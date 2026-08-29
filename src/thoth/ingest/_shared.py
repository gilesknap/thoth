"""Shared types, vocabulary, and the collaborator base of the ingest passes.

The dataclasses, errors, kind vocabulary and :class:`_IngestorBase` live here so the
pass submodules of :mod:`thoth.ingest` stay cycle-free. Only the standard library and
``thoth.*`` are imported, which preserves the package's import-purity contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
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

# The two curation modes stamped into a hold's mode: frontmatter (issue #95) so a later
# inbox sweep honours the original intent instead of guessing. A capture deferred under
# --as-is re-files low-touch, a normal one re-curates. This is the single place the hold
# mode vocabulary is expressed, and inbox_drain reads it back
HOLD_MODE_CURATE: str = "curate"
HOLD_MODE_AS_IS: str = "as-is"
HOLD_MODES: frozenset[str] = frozenset({HOLD_MODE_CURATE, HOLD_MODE_AS_IS})

# The single content folder each page type is written to, inverting
# FOLDER_TYPE_CONTRACT. Each content type maps to exactly one folder, so the as-is
# import path (issue #80, ADR 0010) can route a classify-chosen type without a
# curate-authored plan. Derived from the canonical contract rather than restated, so
# adding a folder is a one-place edit. Inbox is excluded, as-is files into content
_TYPE_FOLDER: dict[str, str] = {
    page_type: folder
    for folder, types in FOLDER_TYPE_CONTRACT.items()
    if folder != "inbox"
    for page_type in types
}

# Head-truncation cap in chars, about 750 tokens, for the extracted body folded into
# the classify and curate prompts. The full text already lives at raw/articles/<slug>.md
# and the curated page is a distilled view, so a lead excerpt carries the gist while
# capping token cost on large articles (issue #75). The same excerpt feeds classify so
# routing is content-aware: a personal URL routes differently from a technical one
# rather than being decided from the link and title alone (issue #123)
_URL_EXCERPT_CHARS: int = 3000


class IngestError(Exception):
    """Raised when an ingest pass fails validation, extraction, or a vault write."""


class LLMUnavailableError(IngestError):
    """Raised when an LLM client call in classify or curate itself fails.

    A subclass of :class:`IngestError`, so existing handlers are unaffected, but
    distinguishable so :meth:`Ingestor.ingest` can treat a transport failure as a
    deferred curation rather than a lost capture. The item is already persisted durably
    to ``inbox/`` before any LLM call (issue #14). A validation failure stays a plain
    :class:`IngestError` and still aborts, so the validation gate is preserved.
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

    Binary bytes never travel as base64 in their stored form (SPEC section 6). A binary
    capture carries a path the server can read, or a URL it fetches itself, and the
    bytes are saved as a real binary under ``raw/assets/``.

    The analyse pass may transiently base64 those bytes to send them to the vision API,
    which ADR 0006 records as a deliberate amendment. That base64 lives inside one
    request and is never persisted.

    A Slack message attaching several images at once is one unit of intent, so it
    becomes one capture rather than N (issue #84). The first image is the primary path
    and drives routing, and the rest ride on ``extra_paths`` under the same slug and
    page, so the batch gets one summary and one tag set with every image inline.

    ``extra_paths`` is populated only for an all-image batch. A heterogeneous batch is
    still ingested per file by the Slack layer.

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
        today: date | None = None,
    ) -> None:
        """Stores the injected collaborators.

        Args:
            config: The frozen runtime configuration.
            vault: Path-confined vault facade, the only disk surface.
            llm: Injectable Anthropic wrapper for classify and curate.
            extractor: SSRF-guarded URL, PDF, image and speech extractor.
            hindsight: Wrapper over the semantic index.
            git: The deterministic git sync wrapper.
            schema_md: Optional SCHEMA.md text passed to curate as ``system_extra``,
                so the model files to the live schema.
            markers: Optional liveness store. When wired, a successful ingest and a
                successful push each record a marker for the daily heartbeat
                (issue #15). None disables recording.
            analyser: Optional analyser for the vision and PDF pass (issue #42). When
                None one is built from the injected llm, so it shares the same daily
                budget guard, using the ``analyse_model`` and ``diagram_model``
                knobs (issue #68). Tests inject a fake to avoid a real model call.
            today: Optional fixed date anchoring the curate prompt's relative-date
                resolution. None reads the live date in the configured timezone.
        """
        self._config = config
        self._vault = vault
        self._llm = llm
        self._extractor = extractor
        self._hindsight = hindsight
        self._git = git
        self._schema_md = schema_md
        self._markers = markers
        self._today = today
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
        """Records a liveness marker, best-effort, so bookkeeping cannot break ingest.

        A failure writing the disposable marker DB must not abort a capture that
        otherwise succeeded, so any error is swallowed. The heartbeat exists to make
        silence diagnostic, not to gate the pipeline.
        """
        if self._markers is None:
            return
        try:
            self._markers.record(name)
        except Exception:  # noqa: BLE001 - marker bookkeeping is best-effort
            pass

    def _capture_kind(self, capture: Capture) -> CaptureKind:
        """Decides the capture kind from the populated fields and any extension hint.

        A path is read by its extension. A text upload is read as the body (issue #57),
        audio is transcribed, and anything else including an unrecognised extension is
        treated as an image binary, which is the common phone-upload case.

        A URL is web-extracted unless its extension or the filename hint marks it as a
        PDF or image. Plain text is the fallback.
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

    def _today_iso(self) -> str:
        """Returns the date anchoring relative-date resolution, as ``YYYY-MM-DD``.

        The injected date when a test pins one, otherwise the live date in
        ``THOTH_TIMEZONE``. The model is never otherwise told what day it is, which left
        deadlines like "urgent todo monday" filed with no date.
        """
        today = self._today or datetime.now(self._config.timezone).date()
        return today.isoformat()

    @staticmethod
    def _capture_summary(
        capture: Capture,
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
        is_transcript: bool = False,
    ) -> str:
        """Renders a compact textual summary of the capture for a prompt.

        A binary's analysis is appended so the model sees the OCR text, description and
        routing hints (issue #42). Before that a binary reached the model as a bare
        ``File: name`` line and was filed blind.

        A body excerpt is appended only when there is no inline text, which is already
        shown verbatim, so a plain text capture is never duplicated.

        An audio transcript is the exception and is always folded in (issue #129). Slack
        stamps a voice note with generic fallback text that lands in ``capture.text``
        and would otherwise suppress the transcript, leaving classify to route off the
        placeholder rather than what was said.
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
            # Head-truncate so a large article cannot blow up the prompt's token cost
            # (issue #75). The full text stays canonical in raw/articles/<slug>.md and
            # the opening reliably carries the gist
            excerpt = extracted_body.strip()[:_URL_EXCERPT_CHARS]
            summary += f"\n\n{label}:\n{excerpt}"
        return summary


@overload
def _ext_kind(name: str, *, default: CaptureKind) -> CaptureKind: ...


@overload
def _ext_kind(name: str, *, default: None) -> CaptureKind | None: ...


def _ext_kind(name: str, *, default: CaptureKind | None) -> CaptureKind | None:
    """Classifies a filename or URL by its extension into a capture kind.

    Args:
        name: A lowercase filename or URL path.
        default: Kind to return when the extension is unrecognised.

    Returns:
        The matching kind, otherwise ``default``.
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
    """Renders a binary's analysis as a prompt block of content and routing hints."""
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
    """Returns ``value``, raising when it is absent."""
    if value is None:
        raise IngestError(f"capture is missing required field {field_name!r}")
    return value
