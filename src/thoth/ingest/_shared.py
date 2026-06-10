"""Shared types, vocabulary, and the collaborator base of the ingest passes.

The capture/classification/report dataclasses, the errors, the kind vocabulary, and
:class:`_IngestorBase` (the ``__init__`` and small helpers every pass class shares)
live here so the pass submodules of :mod:`thoth.ingest` stay cycle-free. Only the
standard library plus ``thoth.*`` are imported, preserving the package's
import-purity contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, overload

from thoth.analyse import Analyser, Analysis
from thoth.config import Config
from thoth.extract import Extractor, FetchedBinary
from thoth.filetypes import AUDIO_EXTS as _AUDIO_EXTS
from thoth.filetypes import IMAGE_EXTS as _IMAGE_EXTS
from thoth.filetypes import TEXT_EXTS as _TEXT_EXTS
from thoth.git_sync import GitSync
from thoth.hindsight import Hindsight
from thoth.llm import LLM
from thoth.state import MarkerStore
from thoth.vault import FOLDER_TYPE_CONTRACT, Vault

logger = logging.getLogger("thoth.ingest")

# The two intended-curation modes stamped into a hold's ``mode:`` frontmatter (issue
# #95, task E) so a later inbox sweep honours the ORIGINAL intent instead of guessing: a
# capture deferred under ``--as-is`` re-files low-touch, a normal capture re-curates.
# The values are kept here (the single place the hold mode vocabulary is expressed) and
# read back by :mod:`thoth.inbox_drain`.
HOLD_MODE_CURATE: str = "curate"
HOLD_MODE_AS_IS: str = "as-is"
HOLD_MODES: frozenset[str] = frozenset({HOLD_MODE_CURATE, HOLD_MODE_AS_IS})

# The single content folder each page ``type`` is written to (the inverse of the
# folder->types :data:`~thoth.vault.FOLDER_TYPE_CONTRACT`). Each content type maps to
# exactly one folder, so the as-is import path (issue #80, ADR 0010) can route a
# classify-chosen type to its folder without a curate-authored file-plan. Derived from
# the same canonical contract rather than restated, so adding a folder is a one-place
# edit. The ``inbox`` holding type is excluded -- as-is files into a content folder.
_TYPE_FOLDER: dict[str, str] = {
    page_type: folder
    for folder, types in FOLDER_TYPE_CONTRACT.items()
    if folder != "inbox"
    for page_type in types
}

# Head-truncation cap (chars, ~750 tokens) for the extracted URL/transcript body folded
# into the classify AND curate prompts via ``_capture_summary``. The vault is canonical
# and the full text already lives at ``raw/articles/<slug>.md``; the curated page is a
# distilled view, so a lead excerpt carries the gist while capping token cost on large
# articles (issue #75). The SAME bounded excerpt feeds classify so routing is
# content-aware -- a personal URL routes differently from a technical one rather than
# being decided from the link + title alone (issue #123); classify stays on Sonnet (the
# Haiku move is issue #79).
_URL_EXCERPT_CHARS: int = 3000


class IngestError(Exception):
    """Raised when an ingest pass fails validation, extraction, or a vault write."""


class LLMUnavailableError(IngestError):
    """Raised when an LLM *client call* (classify/curate) itself fails.

    A subclass of :class:`IngestError` so existing ``except IngestError`` / test
    ``pytest.raises(IngestError)`` sites are unaffected, but distinguishable so
    :meth:`Ingestor.ingest` can treat a transport/availability failure as a *deferred
    curation* (the inbound item is already persisted durably to ``inbox/`` before any
    LLM call, per issue #14) rather than a lost capture. A *validation* failure (an
    out-of-vocabulary type, a bad slug, an unparseable or schema-invalid plan) stays a
    plain :class:`IngestError` and still aborts -- the validation gate is preserved.
    """


class CaptureKind(StrEnum):
    """The kind of inbound item, which selects the raw-capture strategy."""

    URL = "url"
    PDF = "pdf"
    IMAGE = "image"
    AUDIO = "audio"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Capture:
    """One inbound item to ingest: raw text, a URL, or a server-resolvable path.

    Binary bytes never travel as base64 **as their stored/canonical form** (SPEC section
    6): an image/PDF/audio capture carries a ``path`` the *server* can read (downloaded
    by the Slack/MCP layer to a tmp file) or a ``url`` the server fetches itself, and
    the bytes are saved as a real binary under ``raw/assets/`` (never as base64 into
    the vault). The analyse pass (:mod:`thoth.analyse`, issue #42) may **transiently**
    base64-encode those same bytes to send them to the vision/document API *for
    analysis* -- a deliberate amendment to the storage rule recorded in ADR 0006: the
    base64 lives only inside one request and is never persisted or treated as the source
    of truth.

    A Slack message that attaches **several images at once** is the natural unit of
    intent -- the user meant them as *one* thing (three photos of the same whiteboard, a
    figure plus its caption) -- so it is captured as ONE :class:`Capture`, not N
    independent ones (issue #84). The first image is the primary ``path`` (it drives the
    analyse/classify routing); the rest ride on ``extra_paths`` and are saved as extra
    assets under the *same* slug and embedded in the *same* curated page, so the batch
    gets one shared summary + one tag set with every image inline. ``extra_paths`` is
    only populated for an all-image batch (the homogeneous, embed-in-one-page case); a
    single-file message leaves it empty and is unchanged, and a heterogeneous batch
    (mixed images/PDFs/text) is still ingested per file by the Slack layer.

    Attributes:
        text: Inline text/markdown to capture, if any.
        url: A URL to fetch server-side, if any.
        path: A server-resolvable local file (image/pdf/audio), if any. For a
            multi-image batch this is the *primary* image.
        source: The frontmatter ``source`` value (one of
            :data:`thoth.vault.VALID_SOURCES`).
        filename: The original upload name, used for slug and extension hints.
        extra_paths: Additional server-resolvable image files for a multi-image batch
            (issue #84), saved as extra assets alongside the primary in upload order.
    """

    text: str | None = None
    url: str | None = None
    path: Path | None = None
    source: str = "slack"
    filename: str | None = None
    extra_paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class Classification:
    """Validated output of the cheap classify call (the routing table, SPEC Appendix).

    Attributes:
        page_type: The frontmatter ``type``; validated to be in
            :data:`thoth.vault.VALID_TYPES`.
        slug: The page slug; validated by :meth:`thoth.vault.Vault.validate_slug`.
        title: The human-readable title.
        entities: Named entities mentioned (drive candidate fetch).
        concepts: Named concepts mentioned (drive candidate fetch).
    """

    page_type: str
    slug: str
    title: str
    entities: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RawCaptureResult:
    """What the raw-capture pass did: the path written and its disposition.

    Attributes:
        raw_path: The vault-relative raw page path, or ``None`` when no raw page was
            written (for example a plain-text capture with no raw layer).
        disposition: One of ``'created'``, ``'skipped_unchanged'``,
            ``'updated_drift'``, or ``'none'``.
        asset_paths: Vault-relative asset paths saved during raw capture (images).
    """

    raw_path: str | None
    disposition: str
    asset_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Prefetched:
    """Extracted text captured before classify, reused by :meth:`Ingestor.capture_raw`.

    Attributes:
        body: The extracted raw body text (URL markdown / plain text / transcript).
        source_url: The provenance URL, if any (a web-extracted article carries one).
    """

    body: str
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class _Holding:
    """The durable pre-LLM holding write plus any prefetched extraction to reuse.

    Attributes:
        result: The :class:`RawCaptureResult` for the ``inbox/`` holding page.
        prefetched: The extracted text reused by :meth:`Ingestor.capture_raw` so the
            source is not fetched twice, or ``None`` for a binary capture (no text yet).
    """

    result: RawCaptureResult
    prefetched: _Prefetched | None


@dataclass(frozen=True, slots=True)
class _Analysed:
    """The analyse pass's output plus any URL binary it fetched, for one-fetch reuse.

    Attributes:
        analysis: The :class:`~thoth.analyse.Analysis` (or ``None`` for a non-binary
            kind, or an unparseable analysis filed blind).
        fetched: The :class:`~thoth.extract.FetchedBinary` the analyse pass downloaded
            for a URL image/PDF, threaded into :meth:`Ingestor.capture_raw` so the same
            bytes are reused for the asset write -- no second network download and no
            leaked temp file. ``None`` for a local-``path`` capture (no fetch happened)
            or a non-binary kind.
        excalidraw_md: The reconstructed ``.excalidraw.md`` markdown for a ``diagram``
            -kind image (issue #68), or ``None``. A best-effort *enhancement* -- it is
            saved as an extra asset alongside the original, never replacing it, and
            never defers or loses the capture.
    """

    analysis: Analysis | None
    fetched: FetchedBinary | None = None
    excalidraw_md: str | None = None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Structured outcome the Slack/MCP layer renders (SPEC step 8).

    Attributes:
        page_paths: Curated page paths written/updated.
        raw_paths: Raw source page paths written (may be empty). On a deferred capture
            this is the durable ``inbox/`` holding page.
        asset_paths: Binary asset paths saved (may be empty).
        obsidian_links: ``obsidian://`` deep links built by the harness via
            :meth:`thoth.vault.Vault.obsidian_uri` (one per curated page; unfabricable).
        wikilinks: ``[[slug]]`` handles for the curated pages.
        titles: Human-readable page titles, one per curated page (parallel to
            ``page_paths`` / ``obsidian_links``), so the Slack renderer can label each
            link; falls back to the slug-derived title when frontmatter has none.
        committed: Whether :meth:`thoth.git_sync.GitSync.commit` made a commit.
        conflict: Whether a :class:`~thoth.git_sync.VaultConflictError` was surfaced.
        deferred: ``True`` when the inbound item was persisted durably but the
            classify/curate pass was skipped because the LLM was unavailable; a later
            reindex/sweep re-curates the held raw item (SPEC section 6).
        unchanged: ``True`` when this was a no-op re-run -- the raw source was
            byte-identical to an existing one *and* a curated page already exists, so
            the curate/navigation/retain passes were skipped (issue #95, task D). No
            ``updated:`` date was bumped and no LLM curate call was spent.
        message: A short human-readable status line.
    """

    page_paths: list[str]
    raw_paths: list[str]
    asset_paths: list[str]
    obsidian_links: list[str]
    wikilinks: list[str]
    titles: list[str] = field(default_factory=list)
    committed: bool = False
    conflict: bool = False
    deferred: bool = False
    unchanged: bool = False
    message: str = ""


class _IngestorBase:
    """Holds the injected collaborators plus the helpers every pass shares."""

    def __init__(
        self,
        config: Config,
        vault: Vault,
        llm: LLM,
        extractor: Extractor,
        hindsight: Hindsight,
        git: GitSync,
        *,
        schema_md: str | None = None,
        markers: MarkerStore | None = None,
        analyser: Analyser | None = None,
    ) -> None:
        """Store the injected collaborators.

        Args:
            config: The frozen runtime configuration.
            vault: The path-confined read/write vault facade (the only disk surface).
            llm: The injectable Anthropic wrapper (classify + curate calls).
            extractor: The SSRF-guarded URL/PDF/image/STT extractor.
            hindsight: The subprocess wrapper over the semantic index.
            git: The deterministic git sync wrapper.
            schema_md: Optional SCHEMA.md text passed as ``system_extra`` to the curate
                call so the model files to the live schema.
            markers: Optional liveness :class:`~thoth.state.MarkerStore`; when wired, a
                successful ingest records a ``capture`` marker and a successful push
                records a ``push`` marker so the daily heartbeat can report them
                (issue #15). ``None`` (the default) disables marker recording, so
                existing callers and tests are unaffected.
            analyser: Optional :class:`~thoth.analyse.Analyser` for the vision/PDF
                content-analysis pass (issue #42). When ``None`` (the default) one is
                built lazily from the injected ``llm`` -- so it shares the same daily
                budget guard -- and configured with the ``analyse_model`` /
                ``diagram_model`` knobs (issue #68); a test can inject a fake to drive
                analysis with no real model call.
        """
        self._config = config
        self._vault = vault
        self._llm = llm
        self._extractor = extractor
        self._hindsight = hindsight
        self._git = git
        self._schema_md = schema_md
        self._markers = markers
        self._analyser = (
            analyser
            if analyser is not None
            else Analyser(
                llm,
                model=config.analyse_model,
                diagram_model=config.diagram_model,
            )
        )

    def _record_marker(self, name: str) -> None:
        """Record a liveness marker (best-effort; never lets bookkeeping break ingest).

        A failure to write the disposable marker DB must not fail or abort a capture
        that otherwise succeeded, so any error is swallowed (the heartbeat's job is to
        make *silence* diagnostic, not to gate the pipeline).
        """
        if self._markers is None:
            return
        try:
            self._markers.record(name)
        except Exception:  # noqa: BLE001 - marker bookkeeping is best-effort
            pass

    def _capture_kind(self, capture: Capture) -> CaptureKind:
        """Decide the capture kind from the populated fields and any extension hint.

        A server-resolvable ``path`` is read by its extension: a text upload
        (``.md``/``.txt``/... per :data:`_TEXT_EXTS`, issue #57) is a TEXT capture whose
        bytes are read as the body, audio is transcribed, and anything else -- including
        a path with no recognised extension -- is treated as an image binary (the common
        phone-upload case). A ``url`` is web-extracted unless its own extension or the
        ``filename`` hint marks it as a PDF or image (a direct binary the server
        downloads). Plain ``text`` is the fallback.
        """
        hint = (capture.filename or "").lower()
        if capture.path is not None:
            return _ext_kind(
                hint or capture.path.name.lower(), default=CaptureKind.IMAGE
            )
        if capture.url is not None:
            url_name = capture.url.lower().split("?", 1)[0]
            for candidate in (hint, url_name):
                kind = _ext_kind(candidate, default=None)
                if kind is CaptureKind.PDF or kind is CaptureKind.IMAGE:
                    return kind
            return CaptureKind.URL
        return CaptureKind.TEXT

    @staticmethod
    def _capture_summary(
        capture: Capture,
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
        is_transcript: bool = False,
    ) -> str:
        """Render a compact textual summary of the capture for a prompt.

        For a binary capture the analysis (issue #42) is appended so the model sees the
        asset's OCR'd/extracted content, description, and routing hints -- the load-
        bearing fix: previously a binary reached the model as a bare ``File: name`` line
        and was filed blind. ``extracted_body`` (an audio transcript / URL article body
        extracted before curate) is appended for the same reason.

        For a text-bearing capture the body excerpt is appended only when the capture
        has no inline ``text`` -- which is already shown verbatim -- so a plain text
        capture is never duplicated. The exception is ``is_transcript`` (an audio
        capture, issue #129): a voice memo's spoken content is the *canonical* content,
        but Slack stamps the message with generic fallback text ("Listen to voice note")
        that lands in ``capture.text`` and would otherwise suppress the transcript --
        leaving classify to title/route the note blind off the placeholder. So an audio
        transcript is always folded in, regardless of the (noise) caption, so the note
        is titled and routed by what was actually said.
        """
        parts: list[str] = []
        if capture.url is not None:
            parts.append(f"URL: {capture.url}")
        if capture.path is not None:
            parts.append(f"File: {capture.filename or capture.path.name}")
        if capture.text is not None:
            parts.append(f"Text: {capture.text}")
        summary = "\n".join(parts) or "(empty capture)"
        if analysis is not None and not analysis.is_empty():
            summary += "\n\n" + _analysis_summary(analysis)
        if (
            (capture.text is None or is_transcript)
            and extracted_body
            and extracted_body.strip()
        ):
            label = "Extracted text (transcript / article body)"
            # Head-truncate to a bounded lead excerpt so a large article cannot blow up
            # the curate prompt's token cost (issue #75). The full text stays canonical
            # in raw/articles/<slug>.md; the opening reliably carries the gist.
            excerpt = extracted_body.strip()[:_URL_EXCERPT_CHARS]
            summary += f"\n\n{label}:\n{excerpt}"
        return summary


@overload
def _ext_kind(name: str, *, default: CaptureKind) -> CaptureKind: ...


@overload
def _ext_kind(name: str, *, default: None) -> CaptureKind | None: ...


def _ext_kind(name: str, *, default: CaptureKind | None) -> CaptureKind | None:
    """Classify a filename/URL by its extension into a capture kind.

    Args:
        name: A lowercase filename or URL path.
        default: The kind to return when the extension is unrecognised.

    Returns:
        :attr:`CaptureKind.PDF`/``IMAGE``/``AUDIO``/``TEXT`` for a known extension,
        else ``default``.
    """
    if name.endswith(".pdf"):
        return CaptureKind.PDF
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in _TEXT_EXTS:
        return CaptureKind.TEXT
    if ext in _IMAGE_EXTS:
        return CaptureKind.IMAGE
    if ext in _AUDIO_EXTS:
        return CaptureKind.AUDIO
    return default


def _analysis_summary(analysis: Analysis) -> str:
    """Render a binary's analysis as a prompt block (content + routing hints)."""
    lines: list[str] = ["Content analysis of the attached binary:"]
    if analysis.summary.strip():
        lines.append(f"Summary: {analysis.summary.strip()}")
    if analysis.description.strip():
        lines.append(f"Description: {analysis.description.strip()}")
    if analysis.text.strip():
        lines.append(f"Extracted text:\n{analysis.text.strip()}")
    if analysis.suggested_type:
        lines.append(f"Suggested type: {analysis.suggested_type}")
    if analysis.entities:
        lines.append(f"Entities: {', '.join(analysis.entities)}")
    if analysis.concepts:
        lines.append(f"Concepts: {', '.join(analysis.concepts)}")
    return "\n".join(lines)


def _require(value: Any, field_name: str) -> Any:
    """Return ``value`` or raise :class:`IngestError` naming the missing field."""
    if value is None:
        raise IngestError(f"capture is missing required field {field_name!r}")
    return value
