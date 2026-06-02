"""The bounded-pass capture pipeline that files an inbound item into the vault.

This module is the orchestration core of capture (SPEC section 6). It runs a fixed,
ordered sequence of *validated passes* over one :class:`Capture` and never lets the
appliance LLM touch disk or the network directly: every byte that reaches the vault
goes through :class:`thoth.vault.Vault` (so paths are confined and the folder/type/slug
contract is enforced) and every web fetch goes through the SSRF-guarded
:class:`thoth.extract.Extractor`. git is a deterministic collaborator, never an LLM
tool. The passes are:

0. **orient** -- :meth:`thoth.git_sync.GitSync.pull` so writes land on current state.
0b. **persist inbound (durable hold)** -- :meth:`Ingestor.persist_inbound` extracts the
   inbound text/bytes (the only network step) and writes a durable ``inbox/`` holding
   page keyed on the body SHA-256 *before any LLM call*, so an Anthropic outage can
   never lose a capture (per issue #14 -- capture durability decoupled from the
   classify call; SPEC section 6 "pass 0b").
   If the later classify/curate cannot run because the LLM is unavailable, the held raw
   is committed and a *deferred-curation* report is returned for a later reindex/sweep;
   on success the now-superseded holding page is removed.
1. **classify** -- one cheap Claude call -> a :class:`Classification` whose ``type`` and
   ``slug`` are validated through :class:`~thoth.vault.Vault` before use.
2. **capture raw** -- :class:`~thoth.extract.Extractor` by kind (reusing the text
   already extracted in pass 0b, so the source is fetched once); the body SHA-256 is
   compared to any existing raw page's stored digest *before* writing, so an identical
   re-ingest is skipped and a changed body is flagged as drift (the idempotency rule).
   A binary (image/PDF) capture applies the same rule over the *bytes* SHA-256: an
   already-present asset with matching bytes is skipped, and a byte mismatch at the
   same slug is surfaced as drift rather than overwriting (SPEC step 2 'Skip if sha256
   exists'). A PDF additionally lands a ``raw/papers/<slug>.md`` page so the curate
   pass and retrieval have a searchable text body; full PDF text extraction is deferred
   to Phase 3, so the page records the provenance plus a pointer to the kept binary.
3. **fetch candidates** -- a read-only lexical scan for each named entity/concept.
4. **curate** -- a second Claude call returning a file-plan that is validated by
   :func:`thoth.llm.validate_file_plan` *and* re-validated through the
   :class:`~thoth.vault.Vault` write helpers, then written.
5. **navigation** -- :meth:`~thoth.vault.Vault.append_log` for every file touched (a
   reference page's one-line gloss rides in its own ``summary`` frontmatter, so there is
   no separate ``index.md`` catalog pass; ADR 0008).
6. **retain** -- :meth:`thoth.hindsight.Hindsight.retain` per curated page, then a
   ``probe`` that the page came back.
7. **commit** -- :meth:`~thoth.git_sync.GitSync.commit`; a rebase conflict is surfaced
   loudly (never ``--force``).
8. **report** -- a structured :class:`IngestReport` carrying the touched paths plus
   ``obsidian://`` links built by the *harness* (via
   :meth:`~thoth.vault.Vault.obsidian_uri`) so they cannot be fabricated by the model.

All collaborators (``vault``, ``llm``, ``extractor``, ``hindsight``, ``git``) are
injected, so a test substitutes fakes for every external boundary and a real
:class:`~thoth.vault.Vault` over a temporary vault. Only the standard library plus
``thoth.*`` are imported at module top level, so importing this module at pytest
collection is always safe (the heavy clients live behind the injected seams).
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, overload

from thoth.analyse import AnalyseError, Analyser, Analysis
from thoth.budget import BudgetExceededError
from thoth.config import Config
from thoth.extract import ExtractError, Extractor, FetchedBinary
from thoth.git_sync import GitSync, GitSyncError, VaultConflictError
from thoth.hindsight import Hindsight, HindsightError
from thoth.llm import (
    LLM,
    LLMError,
    Message,
    SchemaValidationError,
    _block_id,
    _block_name,
    _tool_use_blocks,
    assistant_blocks_message,
    extract_tool_use,
    file_plan_contract_text,
    parse_json_block,
    tool_result_block,
    validate_file_plan,
)
from thoth.state import MARKER_CAPTURE, MARKER_PUSH, MarkerStore
from thoth.vault import (
    FOLDER_TYPE_CONTRACT,
    SUMMARY_TYPES,
    TYPE_ENUMERATION,
    VALID_TYPES,
    SchemaError,
    SlugError,
    Vault,
    VaultError,
)

__all__ = [
    "Capture",
    "CaptureKind",
    "Classification",
    "IngestError",
    "IngestReport",
    "Ingestor",
    "LLMUnavailableError",
    "RawCaptureResult",
]

logger = logging.getLogger(__name__)

# Folders scanned by the read-only create-vs-update candidate search (reference layer).
_CANDIDATE_DIRS: tuple[str, ...] = ("entities", "notes", "memories")

# File extensions (no dot) that select a binary/audio/text capture kind.
_IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
_AUDIO_EXTS: frozenset[str] = frozenset({"mp3", "wav", "m4a", "ogg", "flac"})
# Plain-text uploads (markdown/notes/data dumps) whose bytes ARE the text body: read
# the file rather than misclassifying it as an image binary and dropping its text
# (issue #57). Checked before the image default in :func:`_ext_kind`.
_TEXT_EXTS: frozenset[str] = frozenset(
    {"md", "txt", "csv", "json", "org", "yaml", "yml", "log", "rst", "tsv"}
)

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

# How many curate LLM attempts before giving up: one initial call plus one corrective
# retry that feeds the validation errors back to the model. A model that returns a
# slightly malformed plan (the failure mode that left the vault empty) is recovered
# rather than aborting the whole capture; a persistently invalid plan still raises so
# the validation gate is preserved.
_CURATE_ATTEMPTS: int = 2

# The forced tool the curate pass uses to return its file plan. Making the model emit
# the plan as a structured ``tool_use.input`` dict (rather than hand-serialised JSON
# free text) means the SDK/transport handles all escaping -- a body with raw newlines,
# tabs, **bold** or U+00A0 non-breaking spaces can never break JSON parsing (issue
# #110, where ~55 of ~140 holds aborted on "Unterminated string" raw_decode failures).
# The schema is deliberately PERMISSIVE (only ``pages`` required): tool-use guarantees
# valid JSON, NOT a valid plan, so :func:`validate_file_plan` stays the real gate and
# the repair loop still feeds validation problems back. The shape mirrors
# :func:`thoth.llm.file_plan_contract_text` and ``_check_page``.
_SUBMIT_FILE_PLAN_TOOL: dict[str, Any] = {
    "name": "submit_file_plan",
    "description": "Submit the file plan for the captured item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "description": "One or more pages to create or update.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "update"],
                        },
                        "folder": {"type": "string"},
                        "slug": {
                            "type": "string",
                            "description": "lowercase-hyphenated",
                        },
                        "frontmatter": {
                            "type": "object",
                            "description": (
                                "title, type, created, updated, source, tags"
                            ),
                            "additionalProperties": True,
                        },
                        "body": {
                            "type": "string",
                            "description": "markdown body with >= 2 [[wikilinks]]",
                        },
                        "summary": {
                            "type": "string",
                            "description": "one-line gloss (reference pages only)",
                        },
                        "wikilinks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                        },
                    },
                    "required": ["action", "folder", "slug", "frontmatter", "body"],
                },
            },
            "log": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "subject": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["pages"],
    },
}

# The forced-tool directive so curate ALWAYS returns its plan via the tool.
_SUBMIT_FILE_PLAN_CHOICE: dict[str, Any] = {
    "type": "tool",
    "name": "submit_file_plan",
}


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


# The binary kinds whose bytes the analyse pass OCRs / extracts to enrich the body and
# route by content (issue #42). Text/URL/audio already carry extracted text, so they are
# never analysed -- their existing paths are unchanged.
_ANALYSE_KINDS: frozenset[CaptureKind] = frozenset({CaptureKind.IMAGE, CaptureKind.PDF})


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

    Attributes:
        text: Inline text/markdown to capture, if any.
        url: A URL to fetch server-side, if any.
        path: A server-resolvable local file (image/pdf/audio), if any.
        source: The frontmatter ``source`` value (one of
            :data:`thoth.vault.VALID_SOURCES`).
        filename: The original upload name, used for slug and extension hints.
    """

    text: str | None = None
    url: str | None = None
    path: Path | None = None
    source: str = "slack"
    filename: str | None = None


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


class Ingestor:
    """Orchestrates the bounded-pass ingest with all collaborators injected."""

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

    # ---- the full pipeline -------------------------------------------------------

    def ingest(
        self, capture: Capture, *, commit: bool = True, as_is: bool = False
    ) -> IngestReport:
        """Run the bounded passes and return a structured report.

        ``commit`` and ``as_is`` are the two seams the ``thoth capture`` backfill (issue
        #80) drives; both default to the Slack/MCP behaviour, so existing callers are
        unaffected:

        * ``commit=False`` defers the git work to the caller. The per-call orient
          (:meth:`_orient` pull) is skipped -- the batch caller pulls **once** up front
          -- and the commit pass writes/logs to disk but does **not** call
          :meth:`thoth.git_sync.GitSync.commit`, so staged changes accumulate in the
          working tree for one batched commit. The returned report has
          ``committed=False``. The deferred (LLM-unavailable) path honours it too.
        * ``as_is=True`` is the low-touch import mode (ADR 0010): the cheap classify
          call still runs (for routing only), but the expensive **curate** is SKIPPED.
          The page is filed ONCE with the original body verbatim and a minimal derived
          frontmatter into the classify-chosen folder, then indexed through the SAME
          retain pass. No file-plan, no reshaping, no wikilink/dedup-merge, no summary
          synthesis -- "files + indexes, skips curate" literally.

        Capture durability is **decoupled from the classify LLM call** (per issue #14):
        the inbound item is extracted and persisted to a durable ``inbox/`` holding page
        (idempotent on the body SHA-256) *before* any LLM call, so an Anthropic outage
        can never lose a capture. Classify/curate then run as a best-effort second
        stage; if the LLM is unavailable (a :class:`LLMUnavailableError`) the held raw
        is already safe, the holding page is committed, and a *deferred-curation* report
        is returned for a later reindex/sweep to re-curate. On success the (now
        superseded) holding page is removed and the curated/raw/navigation/retain passes
        run as before.

        The **validation gate is preserved**: a rejected plan (bad type/slug, an
        unparseable or schema-invalid output) still raises :class:`IngestError`; only a
        *transport* failure defers. A rebase conflict at commit is surfaced as
        :attr:`IngestReport.conflict` (content filed locally; no ``--force``).

        Args:
            capture: The inbound item to ingest.

        Returns:
            The :class:`IngestReport` describing every file touched.

        Raises:
            IngestError: on an extraction, validation, or non-conflict git failure (an
                LLM-availability failure is reported as deferred, not raised).
        """
        started = time.monotonic()
        # commit=False means a batch caller (thoth capture) pulled once up front, so
        # skip the per-call orient and let the staged changes accumulate for its commit.
        if commit:
            self._orient()
        holding = self.persist_inbound(capture)
        analysed = _Analysed(analysis=None)
        try:
            analysed = self.analyse(capture)
            analysis = analysed.analysis
            classification = self.classify(capture, analysis=analysis)
            raw = self.capture_raw(
                capture,
                classification,
                prefetched=holding.prefetched,
                fetched=analysed.fetched,
                derived=analysed,
            )
            # Task D (issue #95): true skip-on-unchanged short-circuit. When the raw
            # source was byte-identical to an existing raw page (skipped_unchanged --
            # which, because the raw path embeds the slug, means classify reproduced the
            # prior routing) AND the curated page it produced is already on disk, the
            # classify-routed curate work is pure churn: a re-run would re-spend the
            # curate LLM call and bump the curated page's `updated:` date for no content
            # change. Skip curate (and navigation/retain) entirely so a re-run to finish
            # an interrupted import costs nothing for the parts already done. Applies to
            # both the curate and as-is paths (each files to the same folder/slug).
            curated = self._unchanged_curated(raw, classification)
            if curated is not None:
                return self._skip_unchanged(holding, classification, curated, commit)
            if as_is:
                # Low-touch import (ADR 0010): SKIP curate; file the original body
                # verbatim into the classify-chosen folder. No second LLM call.
                plan = self._file_as_is(
                    capture,
                    classification,
                    raw,
                    extracted_body=(
                        holding.prefetched.body
                        if holding.prefetched is not None
                        else None
                    ),
                )
            else:
                candidates = self.fetch_candidates(classification)
                plan = self.curate(
                    capture,
                    classification,
                    raw,
                    candidates,
                    analysis=analysis,
                    extracted_body=(
                        holding.prefetched.body
                        if holding.prefetched is not None
                        else None
                    ),
                )
        except LLMUnavailableError as exc:
            # classify/curate (or the analyse call itself) deferred: capture_raw never
            # consumed the analyse-pass binary, so clean up its temp file here rather
            # than leak it (the inbound item is already durable in inbox/).
            _cleanup_fetched(analysed.fetched)
            # Concise operator-readable line (issue #52): a deferral is a partial
            # success (the raw item is durable in inbox/), so say so clearly rather than
            # leaving the degraded path silent. Grep-friendly prefix.
            held = holding.result.raw_path or "inbox"
            logger.info(
                "ingest deferred: held %s (LLM unavailable: %s) in %.0fms",
                held,
                exc,
                (time.monotonic() - started) * 1000,
            )
            return self._commit_deferred(holding, exc, do_commit=commit)

        # Curation succeeded: the holding page is superseded by the curated/raw pages.
        if holding.result.raw_path is not None:
            self._vault.remove_page(holding.result.raw_path)

        page_paths = self._written_page_paths(plan)
        self._apply_navigation(plan, page_paths)
        self._retain_pages(page_paths, classification)

        report = self._build_report(capture, classification, raw, page_paths, plan)
        committed = self._commit(report, classification, do_commit=commit)
        # Record the capture liveness marker only on a clean (non-conflict) ingest, so a
        # wedged sync leaves BOTH the capture and push markers stale -- silence is then
        # the heartbeat's diagnostic (issue #15). The push marker is recorded inside
        # _commit on an actual push.
        if not committed.conflict:
            self._record_marker(MARKER_CAPTURE)
        # Concise operator-readable success line (issue #52): one terse, grep-friendly
        # "ingest filed:" naming the curated path(s), the routed page_type, the page
        # tags, and the wall-clock duration, so a successful capture is no longer silent
        # on the happy path. A conflict is already surfaced via the report.
        logger.info(
            "ingest filed: %s type=%s tags=%s in %.0fms%s",
            ", ".join(page_paths) or "(no curated page)",
            classification.page_type,
            self._page_tags(page_paths),
            (time.monotonic() - started) * 1000,
            " [CONFLICT: unpushed]" if committed.conflict else "",
        )
        return committed

    def _unchanged_curated(
        self, raw: RawCaptureResult, cls: Classification
    ) -> str | None:
        """Return the existing curated path when this is a no-op re-run, else ``None``.

        The skip-on-unchanged short-circuit (issue #95, task D) is only taken when BOTH
        conditions hold, so it never skips genuine work:

        * the raw-capture pass reported ``skipped_unchanged`` -- the source body was
          byte-identical to an existing raw page. Because that raw path embeds the slug
          (``raw/<subdir>/<slug>.md``), a match guarantees classify reproduced the prior
          run's slug; a drifted slug would have created a fresh raw page instead.
        * a curated page already exists at the classify-routed ``<folder>/<slug>.md``.

        A type with no content folder (only ``inbox`` is excluded from
        :data:`_TYPE_FOLDER`) or a missing curated page returns ``None`` so the caller
        falls through to the normal curate/as-is pass -- the short-circuit is purely an
        optimisation and is conservative by construction (a false negative just re-runs
        curate; it never wrongly skips an absent page).
        """
        if raw.disposition != "skipped_unchanged":
            return None
        folder = _TYPE_FOLDER.get(cls.page_type)
        if folder is None:
            return None
        rel = f"{folder}/{cls.slug}.md"
        return rel if self._vault.page_exists(rel) else None

    def _skip_unchanged(
        self,
        holding: _Holding,
        cls: Classification,
        curated_path: str,
        do_commit: bool,
    ) -> IngestReport:
        """Terminal path for a no-op re-run: unchanged content already curated (#95 D).

        Removes the (now superseded) holding page written this run by
        :meth:`persist_inbound` -- exactly like the success path -- then returns an
        ``unchanged`` report WITHOUT running the curate, navigation-log, or Hindsight
        retain passes. So neither the curated page's ``updated:`` date nor the
        ``log.md`` is churned and no LLM/index budget is re-spent for content already on
        disk; the page stays searchable from its original retain. The holding-removal
        deletion is committed (or, for the ``commit=False`` batch path, staged for the
        caller's batched commit) just like a normal success, and the capture liveness
        marker is recorded on a clean (non-conflict) run since the pipeline ran
        healthily.
        """
        if holding.result.raw_path is not None:
            self._vault.remove_page(holding.result.raw_path)
        report = IngestReport(
            page_paths=[],
            raw_paths=[],
            asset_paths=[],
            obsidian_links=[],
            wikilinks=[],
            committed=False,
            conflict=False,
            unchanged=True,
            message=f"Unchanged; already curated at {curated_path} (skipped).",
        )
        committed = self._commit_unchanged(report, cls, do_commit=do_commit)
        if not committed.conflict:
            self._record_marker(MARKER_CAPTURE)
        return committed

    def _commit_unchanged(
        self, report: IngestReport, cls: Classification, *, do_commit: bool
    ) -> IngestReport:
        """Commit the holding-removal for a skip-on-unchanged run (issue #95, task D).

        Mirrors :meth:`_commit_deferred`'s git handling but preserves the ``unchanged``
        report's message instead of synthesising a "Filed N page(s)" line: a conflict is
        surfaced on the report (the removal is local; never a ``--force``), a benign
        "nothing to commit" leaves ``committed=False``, and a real push records the push
        marker. ``do_commit=False`` defers the commit to the batch caller.
        """
        if not do_commit:
            return report
        try:
            result = self._git.commit(cls.title or "unchanged capture")
        except VaultConflictError as conflict:
            return _replace_report(
                report,
                committed=False,
                conflict=True,
                message=(
                    f"{report.message} (holding removal not pushed -- vault conflict; "
                    f"resolve in Obsidian: {conflict})"
                ),
            )
        except GitSyncError as exc:
            raise IngestError(f"commit failed: {exc}") from exc
        if result.committed:
            self._record_marker(MARKER_PUSH)
        return _replace_report(
            report,
            committed=result.committed,
            conflict=False,
            message=report.message,
        )

    # ---- durable pre-LLM capture (SPEC section 6: persist before classify) -------

    def persist_inbound(self, capture: Capture) -> _Holding:
        """Extract and persist the inbound item durably *before* any LLM call.

        Writes a holding page under ``inbox/<sha12>.md`` whose body is the extracted
        text (a URL article's markdown, plain text, or an audio transcript) -- or, for a
        binary capture (image/PDF, no text yet), a short provenance stub naming the
        source so a later sweep can re-fetch and curate it. The slug is derived from the
        body SHA-256, so re-persisting identical content lands on the same path and is
        idempotent (``skipped_unchanged``). This is the *capture-never-lost* guarantee:
        the text is on disk and committable before classify/curate run.

        The extraction itself (the only network step) happens here, so an
        :class:`thoth.extract.ExtractError` still aborts the ingest loudly (nothing is
        lost -- there was nothing to persist). The extracted text is returned on the
        :class:`_Holding` so the later :meth:`capture_raw` reuses it without a second
        fetch.

        Args:
            capture: The inbound item.

        Returns:
            A :class:`_Holding` carrying the holding :class:`RawCaptureResult` and the
            prefetched extraction (if any) for reuse by :meth:`capture_raw`.

        Raises:
            IngestError: on an extraction failure or a vault write error.
        """
        kind = self._capture_kind(capture)
        try:
            prefetched = self._extract_text(capture, kind)
        except ExtractError as exc:
            raise IngestError(f"capture failed during extraction: {exc}") from exc
        body = prefetched.body if prefetched is not None else None
        if body is None:
            # A binary with no extracted text yet: hold a provenance stub so the capture
            # is durable and a later sweep can re-fetch + curate the source.
            body = self._binary_stub_body(capture)
        try:
            result = self._write_inbox_holding(body, capture.source)
        except VaultError as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc
        return _Holding(result=result, prefetched=prefetched)

    # ---- pass 0: orient ----------------------------------------------------------

    def _orient(self) -> None:
        """Pull the vault so writes land on current state (SPEC step 0)."""
        try:
            self._git.pull()
        except GitSyncError as exc:
            raise IngestError(f"vault pull failed before ingest: {exc}") from exc

    # ---- pass 0c: analyse (vision/PDF content extraction, issue #42) -------------

    def analyse(self, capture: Capture) -> _Analysed:
        """OCR/vision/PDF-analyse a binary capture so it is routed + curated by content.

        For an image or PDF capture the bytes are sent to a multimodal model (a vision
        ``image`` block or a ``document`` block) and the returned OCR/extracted text,
        description, and routing hints feed :meth:`classify` (so a whiteboard photo is
        routed to ``notes/`` by its content, not the ``memories/`` default) and
        :meth:`curate` (so the page body holds the real meaning). The asset is still
        saved as a real binary and embedded with ``![[...]]`` -- analysis only enriches
        and routes (ADR 0006). Non-binary kinds (text/URL/audio already carry extracted
        text) return ``None`` and their paths are unchanged.

        The call goes through the injected :class:`~thoth.llm.LLM`, so it is charged
        against the **same daily budget guard** as classify/curate (issue #16). Reusing
        the decoupled-durability pattern, a *transport/availability* failure or a
        budget-cap trip raises :class:`LLMUnavailableError` so the already-durable raw
        asset is **deferred** (re-analysed on a later sweep) rather than lost -- exactly
        like the classify/curate deferral. An *unparseable* analysis (a
        :class:`~thoth.analyse.AnalyseError`) is non-fatal: the binary is filed without
        enrichment (``None``) rather than aborting the capture.

        Args:
            capture: The inbound item.

        Returns:
            An :class:`_Analysed` carrying the :class:`~thoth.analyse.Analysis` for a
            binary capture (``None`` for a non-binary kind, or when the analysis was
            unparseable) plus -- for a URL binary -- the single
            :class:`~thoth.extract.FetchedBinary` it downloaded, so :meth:`capture_raw`
            reuses the same bytes for the asset write instead of fetching a second time.

        Raises:
            LLMUnavailableError: if the analyse model call is unavailable or the daily
                budget cap is reached (treated as a deferral by :meth:`ingest`).
            IngestError: on a failure to read the binary bytes.
        """
        kind = self._capture_kind(capture)
        if kind not in _ANALYSE_KINDS:
            return _Analysed(analysis=None)
        try:
            image_bytes, ext, fetched = self._analyse_bytes(capture, kind)
        except (ExtractError, OSError) as exc:
            raise IngestError(f"analyse failed reading binary: {exc}") from exc
        try:
            if kind is CaptureKind.PDF:
                analysis: Analysis | None = self._analyser.analyse_pdf(image_bytes)
            else:
                analysis = self._analyser.analyse_image(image_bytes, ext=ext)
        except AnalyseError:
            # An unparseable analysis must not lose the capture: file the binary blind
            # (the prior behaviour) rather than abort. The fetched binary is still
            # threaded forward so capture_raw reuses (and cleans up) it.
            analysis = None
        except BudgetExceededError as exc:
            # The capture defers, so capture_raw will not consume the fetched binary --
            # clean it up here rather than leak it.
            _cleanup_fetched(fetched)
            raise LLMUnavailableError(f"analyse deferred (budget cap): {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - any client failure defers (raw durable)
            _cleanup_fetched(fetched)
            raise LLMUnavailableError(f"analyse LLM call failed: {exc}") from exc
        # The PRIMARY analysis succeeded (or filed blind) -- the capture is already
        # safe. Now derive the best-effort enhancement artifacts (issue #68) from the
        # reported image kind, reusing the SAME bytes already in hand (no second
        # read/fetch). Each is purely additive: any failure leaves the original asset
        # filed cleanly and NEVER defers or loses the capture.
        excalidraw_md = self._derive_artifacts(kind, analysis, image_bytes, ext)
        return _Analysed(
            analysis=analysis,
            fetched=fetched,
            excalidraw_md=excalidraw_md,
        )

    def _derive_artifacts(
        self,
        kind: CaptureKind,
        analysis: Analysis | None,
        image_bytes: bytes,
        ext: str,
    ) -> str | None:
        """Best-effort derive the per-kind enhancement artifact (issue #68, ADR 0009).

        For an IMAGE capture only (a PDF gets no derivation), a ``diagram``-kind image
        is reconstructed as an editable Excalidraw scene via
        :meth:`thoth.analyse.Analyser.reconstruct_excalidraw` (a second vision call).

        This is a pure *enhancement* saved alongside the kept original, so every failure
        mode -- ``None``, a raised exception, or a budget trip -- is swallowed and
        turned into ``None`` here: the primary capture is already durable, never
        deferred or lost by a best-effort artifact (the second vision call already
        returns ``None`` on its own failures, but any surprise is guarded too). Returns
        ``excalidraw_md``.
        """
        if kind is not CaptureKind.IMAGE or analysis is None:
            return None
        if analysis.kind == "diagram":
            try:
                return self._analyser.reconstruct_excalidraw(image_bytes, ext=ext)
            except Exception:  # noqa: BLE001 - enhancement only, never lose the capture
                return None
        return None

    def _analyse_bytes(
        self, capture: Capture, kind: CaptureKind
    ) -> tuple[bytes, str, FetchedBinary | None]:
        """Return the inbound binary's bytes, bare extension, and any fetched binary.

        Reads a server-resolvable ``path`` directly (the common Slack/MCP upload case,
        which returns ``fetched=None``) or fetches a ``url`` binary server-side
        **once**; the returned :class:`~thoth.extract.FetchedBinary` is threaded forward
        so :meth:`capture_raw` reuses the same staged bytes for the asset write -- no
        second network download and no leaked temp file (the staged tmp is consumed and
        cleaned by the asset store).
        """
        if capture.path is not None:
            name = capture.filename or capture.path.name
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            return capture.path.read_bytes(), ext, None
        fetched = self._extractor.fetch_binary(_require(capture.url, "url"))
        return fetched.tmp_path.read_bytes(), fetched.suggested_ext, fetched

    # ---- pass 1: classify --------------------------------------------------------

    def classify(
        self, capture: Capture, *, analysis: Analysis | None = None
    ) -> Classification:
        """Run the cheap classify call and validate its routing output.

        One LLM call returns a JSON object with ``type``/``slug``/``title`` plus any
        named entities/concepts. The ``type`` and ``slug`` are validated through
        :class:`~thoth.vault.Vault` here, so a bad routing decision is rejected before
        any disk is touched.

        When ``analysis`` is supplied (a binary capture the analyse pass enriched, issue
        #42), the OCR'd/extracted content is folded into the prompt **and** the model's
        named entities/concepts are unioned with the analysis hints, so the item is
        routed *by its content* -- a whiteboard photo lands in ``notes/``, not the
        ``memories/`` default -- and the candidate fetch sees the analysed terms.

        Args:
            capture: The inbound item to classify.
            analysis: Optional content analysis of a binary capture (image/PDF).

        Returns:
            The validated :class:`Classification`.

        Raises:
            IngestError: if the model output is unparseable or names an
                out-of-vocabulary type or an invalid slug.
        """
        prompt = self._classify_prompt(capture, analysis=analysis)
        try:
            response = self._llm.complete([Message(role="user", content=prompt)])
        except Exception as exc:  # noqa: BLE001 - any client failure aborts classify
            # A transport/availability failure -> deferrable (raw is already durable);
            # validation failures below stay a plain IngestError (abort, gate kept).
            raise LLMUnavailableError(f"classify LLM call failed: {exc}") from exc
        obj = self._parse_block(response, "classification")

        page_type = obj.get("type")
        if not isinstance(page_type, str):
            raise IngestError("classification 'type' must be a string")
        slug = obj.get("slug")
        if not isinstance(slug, str):
            raise IngestError("classification 'slug' must be a string")
        try:
            Vault.validate_slug(slug)
        except SlugError as exc:
            raise IngestError(f"classification slug rejected: {exc}") from exc
        if page_type not in VALID_TYPES:
            raise IngestError(
                f"classification type {page_type!r} is not a valid vault type"
            )

        title = obj.get("title")
        if not isinstance(title, str) or not title.strip():
            title = slug.replace("-", " ").title()

        page_type = self._route_by_analysis(page_type, analysis)
        entities = _str_list(obj.get("entities"))
        concepts = _str_list(obj.get("concepts"))
        if analysis is not None:
            entities = _merge_terms(entities, analysis.entities)
            concepts = _merge_terms(concepts, analysis.concepts)
        return Classification(
            page_type=page_type,
            slug=slug,
            title=title,
            entities=entities,
            concepts=concepts,
        )

    @staticmethod
    def _route_by_analysis(page_type: str, analysis: Analysis | None) -> str:
        """Promote a generic ``memory`` routing to the analysed content type.

        The blind classifier defaults a binary capture to ``memory`` (the only thing it
        can guess from a filename). When the analyse pass extracted real content and
        suggested a knowledge type (``entity``/``note``/``action``), honour that hint so
        the capture is routed by its content rather than landing in ``memories/`` by
        default (issue #42). A model that already chose a non-``memory`` type is
        trusted; an analysis suggesting ``memory`` (a personal snapshot) never overrides
        a more specific model choice.
        """
        if analysis is None:
            return page_type
        suggested = analysis.suggested_type
        if (
            page_type == "memory"
            and suggested is not None
            and suggested in VALID_TYPES
            and suggested != "memory"
        ):
            return suggested
        return page_type

    # ---- pass 2: capture raw -----------------------------------------------------

    def capture_raw(
        self,
        capture: Capture,
        cls: Classification,
        *,
        prefetched: _Prefetched | None = None,
        fetched: FetchedBinary | None = None,
        derived: _Analysed | None = None,
    ) -> RawCaptureResult:
        """Extract the immutable source and write it under ``raw/`` (idempotent).

        Dispatches on the capture kind: a URL is extracted to clean markdown, a PDF or
        image is downloaded as a binary into ``raw/assets/`` via
        :meth:`thoth.extract.Extractor.fetch_binary` + :meth:`Vault.save_asset`, audio
        is transcribed, and plain text is filed verbatim. For text/markdown sources the
        body SHA-256 is compared to any existing raw page's stored digest *before*
        writing: an identical body is skipped (``'skipped_unchanged'``) and a changed
        body is flagged and rewritten (``'updated_drift'``). Images never become base64.

        When ``prefetched`` is supplied (the text extracted by :meth:`persist_inbound`
        before classify), the text-bearing kinds reuse it instead of re-fetching, so a
        URL/audio source is fetched/transcribed exactly once per ingest. When
        ``fetched`` is supplied (a URL image/PDF the analyse pass already downloaded),
        the binary kinds reuse those staged bytes instead of fetching a second time, so
        a URL binary is downloaded exactly once per ingest and its temp file is never
        leaked. Calling this directly with neither re-extracts/re-fetches, the
        standalone behaviour.

        Args:
            capture: The inbound item.
            cls: Its validated classification (supplies the raw slug).
            prefetched: Text already extracted before classify, reused to avoid a second
                fetch; ``None`` re-extracts.
            fetched: A URL binary the analyse pass already downloaded, reused to avoid a
                second download (and the temp-file leak); ``None`` re-fetches.
            derived: The :class:`_Analysed` carrying the best-effort enhancement
                artifacts (issue #68) -- an Excalidraw reconstruction of a ``diagram``
                and a cleaned scan of a ``document`` -- saved as *extra* assets next to
                the original image (the original is always kept and listed first).
                ``None`` saves only the original.

        Returns:
            A :class:`RawCaptureResult` recording the path and disposition. For an image
            capture its ``asset_paths`` lists the original first, then derived assets.

        Raises:
            IngestError: on extraction failure (wraps
                :class:`thoth.extract.ExtractError`) or a vault write error.
        """
        kind = self._capture_kind(capture)
        try:
            if kind is CaptureKind.IMAGE:
                return self._capture_image(
                    capture, cls, fetched=fetched, derived=derived
                )
            if kind is CaptureKind.URL:
                if prefetched is not None:
                    return self._write_raw_doc(
                        "articles", cls, prefetched.body, prefetched.source_url
                    )
                doc = self._extractor.web_extract(_require(capture.url, "url"))
                return self._write_raw_doc(
                    "articles", cls, doc.markdown, doc.source_url
                )
            if kind is CaptureKind.PDF:
                return self._capture_pdf(capture, cls, fetched=fetched)
            if kind is CaptureKind.AUDIO:
                if prefetched is not None:
                    return self._write_raw_doc(
                        "transcripts", cls, prefetched.body, None
                    )
                transcript = self._extractor.transcribe(_require(capture.path, "path"))
                return self._write_raw_doc("transcripts", cls, transcript, None)
            # TEXT
            text = (
                prefetched.body if prefetched is not None else self._text_body(capture)
            )
            return self._write_raw_doc("articles", cls, text, None)
        except ExtractError as exc:
            raise IngestError(f"capture failed during extraction: {exc}") from exc
        except VaultError as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc

    # ---- pass 3: fetch candidates ------------------------------------------------

    def fetch_candidates(self, cls: Classification) -> list[str]:
        """Find existing pages that the curate pass may update (read-only).

        Runs :meth:`search_vault` for each named entity and concept and returns the
        de-duplicated, order-preserving list of candidate vault paths.

        Args:
            cls: The validated classification carrying the named terms.

        Returns:
            Vault-relative paths of existing curated pages that match a named term.
        """
        seen: list[str] = []
        for term in (*cls.entities, *cls.concepts, cls.title):
            for path in self.search_vault(term):
                if path not in seen:
                    seen.append(path)
        return seen

    # ---- pass 4: curate ----------------------------------------------------------

    def curate(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        candidates: list[str],
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> dict[str, Any]:
        """Run the curate call, validate the file-plan, and write every page.

        A second LLM call returns a file-plan; it is validated by
        :func:`thoth.llm.validate_file_plan` (which reuses the same vault validators)
        then each page is written through :meth:`thoth.vault.Vault.write_page`, which
        re-validates the folder/type/slug contract and confines the path. A plan that
        tries to escape the vault root or violates the contract is rejected and nothing
        is written for the offending page.

        When ``analysis`` is supplied (a binary capture, issue #42), the OCR'd/extracted
        text + description are given to the model so the curated page **body holds the
        real meaning** of the asset (and cross-links it), instead of a blind stub around
        the ``![[asset]]`` embed.

        ``extracted_body`` plays the same role for a *text-bearing* capture whose body
        was extracted before classify -- an audio transcript or a URL article's markdown
        (it lives in ``raw/`` but the model cannot read files, only the prompt). Without
        it an audio capture reached the model as a bare ``File: clip.m4a`` line and got
        filed as a content-free "no transcript yet" stub, even though whisper had
        transcribed it. Inlined only when the capture has no inline ``text`` (which is
        already shown), so a plain-text capture is never duplicated.

        Args:
            capture: The inbound item (for context).
            cls: The validated classification.
            raw: The raw-capture result (its path/embeds are offered to the model).
            candidates: Existing candidate page paths.
            analysis: Optional content analysis of a binary capture (image/PDF).
            extracted_body: Optional pre-extracted text body (audio transcript / URL
                article markdown) to inline so curate sees the real content.

        Returns:
            The validated file-plan object (with a private list of written page paths
            attached under ``"_written"``).

        Raises:
            IngestError: if the model output is unparseable, the plan fails validation,
                or a vault write rejects a page.
        """
        prompt = self._curate_prompt(
            capture,
            cls,
            raw,
            candidates,
            analysis=analysis,
            extracted_body=extracted_body,
        )
        messages: list[Message] = [Message(role="user", content=prompt)]
        problems = ""
        for attempt in range(_CURATE_ATTEMPTS):
            try:
                response = self._llm.complete(
                    messages,
                    system_extra=self._schema_md,
                    tools=[_SUBMIT_FILE_PLAN_TOOL],
                    tool_choice=_SUBMIT_FILE_PLAN_CHOICE,
                )
            except Exception as exc:  # noqa: BLE001 - any client failure aborts curate
                # Transport/availability failure -> deferrable (raw is already durable).
                raise LLMUnavailableError(f"curate LLM call failed: {exc}") from exc
            try:
                plan = self._parse_and_validate_plan(response)
            except IngestError as exc:
                # A parse/validation failure is recoverable: feed the exact problems
                # back to the model once before giving up. The last attempt re-raises,
                # so a persistently invalid plan still aborts (validation gate kept).
                problems = str(exc)
                if attempt + 1 >= _CURATE_ATTEMPTS:
                    raise
                messages = [
                    Message(role="user", content=prompt),
                    assistant_blocks_message(response),
                    _curate_repair_turn(response, problems),
                ]
                continue

            written: list[str] = []
            pages = plan.get("pages")
            assert isinstance(pages, list)  # guaranteed by validate_file_plan
            for page in pages:
                written.append(
                    self._write_planned_page(
                        page, capture.source, raw, analysis=analysis
                    )
                )
            plan["_written"] = written
            return plan
        # Unreachable: the loop either returns a written plan or re-raises on the last
        # attempt, but keep a definite terminator for the type checker.
        raise IngestError(f"file plan rejected after retries: {problems}")

    # ---- pass 4 (alternative): file as-is, no curate (issue #80, ADR 0010) -------

    def _file_as_is(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        *,
        extracted_body: str | None = None,
    ) -> dict[str, Any]:
        """File one page with the original body verbatim, skipping the curate LLM call.

        The low-touch import mode (ADR 0010): the cheap classify call has already chosen
        the routing (``type``/``slug``/``title``), so this writes ONE page into that
        type's content folder with the **original body verbatim** and a minimal derived
        frontmatter (``title``/``type``/``source``/``tags``) -- no second (curate) LLM
        call, no reshaping, no wikilink/dedup-merge, no summary synthesis. Any saved
        asset is embedded and any analysed OCR text appended (the same enrichment the
        curated path applies), so a binary import is still searchable on its content.

        Returns a file-plan-shaped dict (with ``_written`` and a single ``pages`` entry)
        so the shared navigation/report tail in :meth:`ingest` treats it like a curate
        plan; the page itself is written here through the confined
        :meth:`thoth.vault.Vault.write_page`, which re-validates the folder/type/slug
        contract.

        Args:
            capture: The inbound item (its ``text``/``source`` are the body/provenance).
            cls: The validated classification (supplies folder routing, slug, title).
            raw: The raw-capture result (its asset embeds are appended).
            extracted_body: A pre-extracted text body (URL article / audio transcript)
                used as the page body when the capture has no inline ``text``.

        Returns:
            A file-plan-shaped dict whose ``_written`` lists the single filed page path.

        Raises:
            IngestError: if the classification routes to an unknown type/folder or the
                vault rejects the write.
        """
        folder = _TYPE_FOLDER.get(cls.page_type)
        if folder is None:
            raise IngestError(
                f"as-is import: classification type {cls.page_type!r} has no content "
                "folder"
            )
        body = self._as_is_body(capture, raw, extracted_body)
        frontmatter: dict[str, Any] = {
            "title": cls.title,
            "type": cls.page_type,
            "source": capture.source,
            "tags": [],
        }
        try:
            rel = self._vault.write_page(folder, cls.slug, frontmatter, body)
        except (SchemaError, SlugError, VaultError) as exc:
            raise IngestError(
                f"as-is import rejected page {folder}/{cls.slug}: {exc}"
            ) from exc
        return {
            "_written": [rel],
            "pages": [{"frontmatter": dict(frontmatter)}],
            "log": {"subject": cls.title},
        }

    def _as_is_body(
        self,
        capture: Capture,
        raw: RawCaptureResult,
        extracted_body: str | None,
    ) -> str:
        """Build the verbatim page body for an as-is import (no model reshaping).

        Prefers the inline ``text`` (the Markdown/text upload case -- the body IS the
        file), then a pre-extracted body (URL article / audio transcript), then a stub
        naming the kept asset for a binary with no text. The saved-asset embeds are
        appended so the binary renders in Obsidian.
        """
        if capture.text is not None:
            body = capture.text
        elif extracted_body and extracted_body.strip():
            body = extracted_body
        elif raw.asset_paths:
            body = ""
        else:
            body = "_Imported with no extractable text._"
        return self._append_embeds(body, {}, raw)

    def _parse_and_validate_plan(self, response: Any) -> dict[str, Any]:
        """Read the curate tool-use plan and validate it against the file-plan contract.

        The curate pass FORCES the ``submit_file_plan`` tool, so the plan arrives as a
        structured ``tool_use.input`` dict (escaping handled by the SDK -- no more
        invalid-JSON aborts, issue #110). A response with no such tool call is treated
        like a parse failure: recoverable by the repair loop. ``validate_file_plan``
        stays the authoritative gate (tool-use guarantees valid JSON, not a valid plan).

        Raises:
            IngestError: if the model did not call the tool or the plan fails
                validation; the message names every offending field so :meth:`curate`
                can feed it back to the model on the corrective retry.
        """
        plan = extract_tool_use(response, "submit_file_plan")
        if plan is None:
            raise IngestError("curate did not call submit_file_plan tool")
        try:
            validate_file_plan(plan)
        except SchemaValidationError as exc:
            raise IngestError(f"file plan rejected: {exc}") from exc
        return plan

    # ---- read-only create-vs-update helper --------------------------------------

    def search_vault(self, query: str, *, limit: int = 10) -> list[str]:
        """Scan the curated folders for ``query`` in filenames and bodies (read-only).

        A case-insensitive lexical scan over ``*.md`` in the curated layer
        (:data:`_CANDIDATE_DIRS`). No LLM, no network; pure disk read. Used to decide
        whether a named term already has a page to update.

        Args:
            query: The term to search for.
            limit: The maximum number of paths to return.

        Returns:
            Up to ``limit`` vault-relative paths whose filename or body contains the
            term, order-preserving and de-duplicated.
        """
        needle = query.strip().lower()
        hits: list[str] = []
        if not needle:
            return hits
        for folder in _CANDIDATE_DIRS:
            directory = self._vault.root / folder
            if not directory.is_dir():
                continue
            for md_path in sorted(directory.glob("*.md")):
                rel = f"{folder}/{md_path.name}"
                if rel in hits:
                    continue
                haystack = md_path.name.lower()
                try:
                    haystack += "\n" + md_path.read_text(encoding="utf-8").lower()
                except OSError:
                    pass
                if needle in haystack:
                    hits.append(rel)
                    if len(hits) >= limit:
                        return hits
        return hits

    # ---- internals: durable pre-LLM holding --------------------------------------

    def _extract_text(self, capture: Capture, kind: CaptureKind) -> _Prefetched | None:
        """Extract the text body for a text-bearing capture (no LLM), else ``None``.

        Runs the single network/IO step per kind -- web-extract a URL, transcribe audio,
        read an uploaded text file (issue #57), or take inline text verbatim -- and
        returns the body plus any provenance URL. Binary kinds (image/PDF) have no text
        body yet, so ``None`` is returned and the caller holds a provenance stub
        instead.

        Raises:
            ExtractError: on a web-extract / transcribe failure (raised to the caller).
            IngestError: when a text capture supplies neither inline text nor a readable
                file path.
        """
        if kind is CaptureKind.URL:
            doc = self._extractor.web_extract(_require(capture.url, "url"))
            return _Prefetched(body=doc.markdown, source_url=doc.source_url)
        if kind is CaptureKind.AUDIO:
            transcript = self._extractor.transcribe(_require(capture.path, "path"))
            return _Prefetched(body=transcript, source_url=None)
        if kind is CaptureKind.TEXT:
            return _Prefetched(body=self._text_body(capture), source_url=None)
        return None

    @staticmethod
    def _text_body(capture: Capture) -> str:
        """Return the body for a TEXT capture: inline text, else the uploaded file.

        An uploaded ``.md``/``.txt``/... file (issue #57) carries its body as the file
        itself, so when no inline ``text`` is supplied the server-resolvable ``path`` is
        read. Decoding uses ``errors="replace"`` so a stray non-UTF-8 byte in a log/CSV
        dump never aborts the capture (the text is still filed, with the offending byte
        shown as the replacement char).

        Raises:
            IngestError: if the capture has neither inline text nor a readable path.
        """
        if capture.text is not None:
            return capture.text
        path = _require(capture.path, "text")
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise IngestError(f"capture failed reading text file: {exc}") from exc

    @staticmethod
    def _binary_stub_body(capture: Capture) -> str:
        """Build the holding-page body for a binary capture with no extracted text yet.

        Reached only for a binary upload (image/PDF) whose bytes have not yet been
        analysed/extracted, so the held page records the source URL / filename for a
        later reindex/sweep to re-fetch and curate; it carries no base64 (the bytes are
        fetched server-side when the item is curated). The deferral reason is the
        *unsupported binary content*, not LLM availability (issue #57): a text upload is
        read directly and never lands here.
        """
        ref = capture.url or capture.filename or "(binary upload)"
        return (
            f"# Held capture\n\n"
            f"Binary source: `{ref}`\n\n"
            "_Unsupported binary content held at capture time; queued for a later "
            "reindex/sweep to fetch and curate._"
        )

    def _write_inbox_holding(self, body: str, source: str) -> RawCaptureResult:
        """Write the durable ``inbox/<sha12>.md`` holding page (idempotent on body SHA).

        The slug is the first 12 hex chars of the body SHA-256, so re-persisting an
        identical body lands on the same path and is skipped (``skipped_unchanged``);
        the page records ``type: inbox`` so a later sweep can find un-curated holds. The
        ``source`` is the capture's own origin (``mcp``/``slack``/...), threaded through
        so a deferred item is held under its true provenance for the re-curate sweep; it
        is validated against :data:`~thoth.vault.VALID_SOURCES` by
        :meth:`Vault.write_page`. The durable digest compare uses
        :meth:`Vault.stored_body_sha256` (the same digest the writer stamps), matching
        :meth:`_write_raw_doc`.

        Args:
            body: The extracted inbound text (or a binary provenance stub) to hold.
            source: The capture's frontmatter ``source`` value.

        Returns:
            A :class:`RawCaptureResult` naming the held page and its disposition.
        """
        slug = f"hold-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]}"
        rel = f"inbox/{slug}.md"
        new_sha = Vault.stored_body_sha256(body)
        existing_sha = self._existing_raw_sha(rel)
        if existing_sha is not None and existing_sha == new_sha:
            return RawCaptureResult(raw_path=rel, disposition="skipped_unchanged")
        disposition = "updated_drift" if existing_sha is not None else "created"
        meta: dict[str, object] = {
            "title": "Held capture",
            "type": "inbox",
            "source": source,
            "tags": ["inbox"],
            # Stamp the body digest so re-persist is idempotent (mirrors write_raw).
            "sha256": new_sha,
        }
        self._vault.write_page("inbox", slug, meta, body)
        return RawCaptureResult(raw_path=rel, disposition=disposition)

    # ---- internals: raw capture --------------------------------------------------

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

    def _write_raw_doc(
        self,
        subdir: str,
        cls: Classification,
        body: str,
        source_url: str | None,
    ) -> RawCaptureResult:
        """Write (or idempotently skip) a textual raw page after a SHA-256 compare.

        The body SHA-256 is computed and compared to the stored ``sha256`` of any
        existing raw page at the same path *before* writing: equal means skip (the page
        and its mtime are untouched), different means drift (rewrite). A brand-new path
        is created.

        Args:
            subdir: The ``raw/`` subdir (``articles`` or ``transcripts``).
            cls: The validated classification (supplies the slug).
            body: The raw markdown body.
            source_url: The provenance URL stamped into frontmatter, if any.
        """
        rel = f"raw/{subdir}/{cls.slug}.md"
        # write_raw stamps the parse-stable redacted digest (Vault.stored_body_sha256),
        # so the idempotency compare MUST use the same derivation -- otherwise an
        # unchanged body ending in a newline (the normal extractor case) never matches
        # and is wrongly re-reported as drift.
        new_sha = Vault.stored_body_sha256(body)
        existing_sha = self._existing_raw_sha(rel)
        if existing_sha is not None and existing_sha == new_sha:
            return RawCaptureResult(raw_path=rel, disposition="skipped_unchanged")
        disposition = "updated_drift" if existing_sha is not None else "created"
        meta: dict[str, object] = {}
        if source_url is not None:
            meta["source_url"] = source_url
        self._vault.write_raw(subdir, cls.slug, meta, body)
        return RawCaptureResult(raw_path=rel, disposition=disposition)

    def _capture_pdf(
        self,
        capture: Capture,
        cls: Classification,
        *,
        fetched: FetchedBinary | None = None,
    ) -> RawCaptureResult:
        """Keep a PDF binary and write a searchable ``raw/papers/<slug>.md`` page.

        The binary is staged into ``raw/assets/`` (idempotent on its bytes SHA-256,
        like an image) and a ``raw/papers/<slug>.md`` page is written (idempotent on
        its body SHA-256) recording the source URL and a pointer to the kept binary, so
        the curate pass and :mod:`thoth.query` retrieval have a text body to surface
        (SPEC step 2: ``PDF/arxiv -> raw/papers/<slug>.md + keep <slug>.pdf``). Full PDF
        text extraction is deferred to Phase 3; the page is the provenance stub until
        then. The returned disposition is the raw page's (the searchable artefact);
        ``skipped_unchanged`` is reported only when the page body is also unchanged.

        Raises:
            IngestError: if the binary is genuinely different at an existing asset slug.
        """
        if capture.url is not None:
            # Reuse the analyse pass's single download when present (no second fetch,
            # no leaked temp); fall back to fetching for a standalone capture_raw call.
            binary = (
                fetched
                if fetched is not None
                else self._extractor.fetch_binary(capture.url)
            )
            asset_result = self._save_fetched_asset(cls, binary)
            source_url: str | None = binary.source_url
        else:
            path = _require(capture.path, "path")
            asset_result = self._save_local_asset_result(cls, path, "pdf")
            source_url = None
        return self._write_paper_stub(cls, asset_result, source_url)

    def _write_paper_stub(
        self,
        cls: Classification,
        asset_result: RawCaptureResult,
        source_url: str | None,
    ) -> RawCaptureResult:
        """Write the ``raw/papers/<slug>.md`` provenance page for a kept PDF binary.

        The page body names the kept binary (so retrieval can follow it) and notes the
        deferred text extraction. The asset's own disposition/paths are carried through
        so the report still lists the saved binary; the page write is idempotent on its
        body SHA-256 via :meth:`_write_raw_doc`.
        """
        asset_rel = asset_result.asset_paths[0] if asset_result.asset_paths else None
        asset_note = (
            f"Binary kept at `{asset_rel}`." if asset_rel else "Binary not kept."
        )
        body = (
            f"# {cls.title}\n\n"
            f"{asset_note}\n\n"
            "_PDF text extraction is deferred to Phase 3; this page records the "
            "source so the capture is searchable in the meantime._"
        )
        paper = self._write_raw_doc("papers", cls, body, source_url)
        return RawCaptureResult(
            raw_path=paper.raw_path,
            disposition=paper.disposition,
            asset_paths=list(asset_result.asset_paths),
        )

    def _capture_image(
        self,
        capture: Capture,
        cls: Classification,
        *,
        fetched: FetchedBinary | None = None,
        derived: _Analysed | None = None,
    ) -> RawCaptureResult:
        """Download/stage an image binary into ``raw/assets`` (never base64).

        The original image is always saved first, then any best-effort enhancement
        artifacts the analyse pass derived (issue #68) are saved as *extra* assets under
        the same slug and merged into the returned ``asset_paths`` (original first), so
        :meth:`_append_embeds` embeds all of them and curate sees them:

        * ``<slug>.excalidraw.md`` -- an editable Excalidraw reconstruction of a hand-
          drawn ``diagram`` (the original is kept, never replaced).

        Each derived asset goes through :meth:`_store_asset`, so it keeps the same
        bytes-SHA-256 idempotency/drift behaviour as the original (a byte-identical
        re-ingest skips it).
        """
        if capture.url is not None:
            # Reuse the analyse pass's single download when present (no second fetch,
            # no leaked temp); fall back to fetching for a standalone capture_raw call.
            binary = (
                fetched
                if fetched is not None
                else self._extractor.fetch_binary(capture.url)
            )
            original = self._save_fetched_asset(cls, binary)
        else:
            path = _require(capture.path, "path")
            ext = (capture.filename or path.name).rsplit(".", 1)[-1].lower()
            original = self._save_local_asset_result(cls, path, ext)
        return self._append_derived_assets(cls, original, derived)

    def _append_derived_assets(
        self,
        cls: Classification,
        original: RawCaptureResult,
        derived: _Analysed | None,
    ) -> RawCaptureResult:
        """Save the derived enhancement assets and merge them after the original.

        Writes each derived artifact (issue #68) to a temp file and routes it through
        :meth:`_store_asset` under the classification slug, then returns a
        :class:`RawCaptureResult` whose ``asset_paths`` lists the original first then
        every derived asset saved. The original's own disposition is preserved (the
        derived assets are additive and never change whether the *original* was created,
        skipped, or drifted). ``None`` derived (or no artifacts) returns the original
        unchanged.
        """
        if derived is None:
            return original
        asset_paths = list(original.asset_paths)
        if derived.excalidraw_md is not None:
            rel = self._store_text_asset(
                f"{cls.slug}.excalidraw.md", derived.excalidraw_md
            )
            if rel is not None and rel not in asset_paths:
                asset_paths.append(rel)
        return RawCaptureResult(
            raw_path=original.raw_path,
            disposition=original.disposition,
            asset_paths=asset_paths,
        )

    def _store_text_asset(self, asset_name: str, text: str) -> str | None:
        """Stage a derived *text* artifact and store it under ``raw/assets``.

        Used for the ``<slug>.excalidraw.md`` reconstruction (issue #68). The text is
        written to a fresh tmp file and handed to :meth:`_store_asset` (so the bytes-
        SHA-256 idempotency/drift rule applies and the tmp is never leaked). Returns the
        stored vault-relative path, or ``None`` if the (best-effort) write fails.

        Crucially, a derived artifact is an *enhancement* and must never lose or defer
        the already-durable primary capture: an :class:`IngestError` from
        :meth:`_store_asset` -- most realistically *drift*, because
        :meth:`~thoth.analyse.Analyser.reconstruct_excalidraw` is a non-deterministic
        model call so a byte-identical re-ingest produces a *different*
        ``<slug>.excalidraw.md`` -- is swallowed to ``None`` here (the existing asset
        is left untouched) rather than aborting the capture (ADR 0009).
        """
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(text.encode("utf-8"))
            staged = Path(handle.name)
        try:
            result = self._store_asset(staged, asset_name)
        except IngestError:
            return None
        return result.asset_paths[0] if result.asset_paths else None

    def _save_fetched_asset(
        self, cls: Classification, fetched: FetchedBinary
    ) -> RawCaptureResult:
        """Move a :class:`~thoth.extract.FetchedBinary` tmp file into ``raw/assets``.

        Idempotent on the fetched bytes' SHA-256: if the destination asset already
        holds byte-identical content the move is skipped (``'skipped_unchanged'``) and
        the staged tmp file is cleaned up; a byte mismatch at the same slug is surfaced
        as drift (never an overwrite). On the happy path :meth:`Vault.save_asset` moves
        the tmp file; only the error/skip path must clean it up.
        """
        asset_name = f"{cls.slug}.{fetched.suggested_ext}"
        return self._store_asset(fetched.tmp_path, asset_name)

    def _save_local_asset_result(
        self, cls: Classification, path: Path, ext: str
    ) -> RawCaptureResult:
        """Stage a server-resolvable local file into ``raw/assets`` via the vault.

        The source is copied into a fresh tmp file first so :meth:`Vault.save_asset`'s
        move never consumes the caller's original (the Slack/MCP tmp download). The same
        bytes-SHA-256 idempotency/drift rule as :meth:`_save_fetched_asset` applies, and
        the staged tmp copy is always cleaned up on the skip/error path.
        """
        asset_name = f"{cls.slug}.{ext}"
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(path.read_bytes())
            staged = Path(handle.name)
        return self._store_asset(staged, asset_name)

    def _store_asset(self, tmp_path: Path, asset_name: str) -> RawCaptureResult:
        """Move ``tmp_path`` into ``raw/assets`` idempotently, never leaking the tmp.

        Compares the staged bytes' SHA-256 to any existing asset of the same name
        *before* the move: equal bytes mean an idempotent skip, different bytes mean
        drift (a loud error, never a silent overwrite), and a missing asset means a
        fresh create. The tmp/staged file is unlinked on every path that does not hand
        it to :meth:`Vault.save_asset` (skip and drift), and on a ``save_asset`` failure
        (for example a malformed asset filename), so no ``thoth-*`` temp file is leaked.

        Raises:
            IngestError: if the staged bytes differ from an existing asset's bytes
                (drift), or the vault rejects the write.
        """
        rel = f"raw/assets/{asset_name}"
        try:
            new_sha = Vault.bytes_sha256(tmp_path.read_bytes())
            if self._vault.asset_exists(asset_name):
                existing_sha = self._vault.asset_sha256(asset_name)
                if existing_sha != new_sha:
                    raise IngestError(
                        f"asset drift: {rel!r} already exists with different bytes; "
                        "refusing to overwrite (resolve in Obsidian)"
                    )
                return RawCaptureResult(
                    raw_path=None,
                    disposition="skipped_unchanged",
                    asset_paths=[rel],
                )
            written = self._vault.save_asset(tmp_path, asset_name)
            return RawCaptureResult(
                raw_path=None, disposition="created", asset_paths=[written]
            )
        except (SlugError, VaultError) as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc
        finally:
            # save_asset MOVES the tmp into the vault on success, leaving nothing to
            # clean. On a skip, a drift error, or a save_asset failure the bytes are
            # still staged, so unlink them here -- no thoth-* temp file is ever leaked.
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _existing_raw_sha(self, rel: str) -> str | None:
        """Return the stored ``sha256`` of an existing raw page, or ``None``."""
        if not self._vault.page_exists(rel):
            return None
        page = self._vault.read_page(rel)
        stored = page.frontmatter.get("sha256")
        return stored if isinstance(stored, str) else None

    # ---- internals: curate -------------------------------------------------------

    def _write_planned_page(
        self,
        page: dict[str, Any],
        source: str,
        raw: RawCaptureResult,
        *,
        analysis: Analysis | None = None,
    ) -> str:
        """Write one validated file-plan page through the confined vault helper.

        ``write_page`` re-validates the folder/type/slug contract and confines the path,
        so a plan that slipped a bad folder or an escaping slug past the schema check is
        still rejected here. A reference page's per-plan ``summary`` (issue #72) is
        routed into its frontmatter -- the canonical, rebuildable one-line gloss that
        replaces the old ``index.md`` catalog (ADR 0008) and which :meth:`thoth.query`
        grep then absorbs transparently. For a binary capture the asset's analysed OCR
        text is ensured present in the body (issue #42) so the page is searchable on the
        real content even if the model's body did not transcribe it.
        """
        folder = page["folder"]
        slug = page["slug"]
        frontmatter = dict(page["frontmatter"])
        frontmatter.setdefault("source", source)
        self._apply_summary(frontmatter, page)
        body = page["body"]
        body = self._append_embeds(body, page, raw)
        body = self._ensure_analysis_text(body, raw, analysis)
        try:
            return self._vault.write_page(folder, slug, frontmatter, body)
        except (SchemaError, SlugError, VaultError) as exc:
            raise IngestError(
                f"vault rejected planned page {folder}/{slug}: {exc}"
            ) from exc

    @staticmethod
    def _apply_summary(frontmatter: dict[str, Any], page: dict[str, Any]) -> None:
        """Route a reference page's per-plan ``summary`` into its frontmatter (#72).

        The curate plan carries a one-line ``summary`` per page; for a reference page
        (:data:`~thoth.vault.SUMMARY_TYPES`: ``entity``/``note``/``memory``) it is the
        canonical, rebuildable gloss and is written into frontmatter as ``summary:`` so
        :meth:`thoth.query.QueryEngine.grep` (which scans the whole file including
        frontmatter) finds it -- the page now owns its gloss instead of an ``index.md``
        catalog (ADR 0008). A blank/whitespace summary, an ``action``/``inbox`` page, or
        a page that already carries its own ``summary`` frontmatter is left untouched.
        """
        if "summary" in frontmatter:
            return
        page_type = frontmatter.get("type")
        if not isinstance(page_type, str) or page_type not in SUMMARY_TYPES:
            return
        summary = page.get("summary")
        if isinstance(summary, str) and summary.strip():
            frontmatter["summary"] = summary.strip()

    @staticmethod
    def _ensure_analysis_text(
        body: str, raw: RawCaptureResult, analysis: Analysis | None
    ) -> str:
        """Append the analysed OCR/extracted text to an asset-bearing page if absent.

        Only the page(s) carrying the saved asset get the extracted text, so a
        multi-page plan does not duplicate the transcript onto unrelated pages. The text
        is appended only when the model's body does not already contain it (the model
        may have transcribed it itself), so there is no double-paste.
        """
        if analysis is None or not raw.asset_paths or not analysis.text.strip():
            return body
        ocr = analysis.text.strip()
        if ocr in body:
            return body
        return body.rstrip("\n") + "\n\n## Extracted text\n\n" + ocr

    @staticmethod
    def _append_embeds(body: str, page: dict[str, Any], raw: RawCaptureResult) -> str:
        """Append Obsidian ``![[asset]]`` embeds for saved assets not already in body.

        Uses the asset filename (Obsidian resolves embeds vault-wide), never a base64
        blob. Embeds already present in the model's body are left as-is -- except one
        the curate model wrote by an asset's *on-disk* filename, which is rewritten to
        its render form: for an Excalidraw drawing the model often emits
        ``![[<slug>.excalidraw.md]]`` (which renders as the raw JSON note), so it is
        normalised to ``![[<slug>.excalidraw]]`` (issue #68 live-verify). The rewrite
        runs before the de-dupe, so the harness never appends a second, redundant embed.
        """
        embeds: list[str] = []
        for asset_rel in raw.asset_paths:
            name = PurePosixPath(asset_rel).name
            embed_name = _embed_name(name)
            if embed_name != name:
                body = body.replace(f"![[{name}]]", f"![[{embed_name}]]")
            embed = f"![[{embed_name}]]"
            if embed not in body and embed not in embeds:
                embeds.append(embed)
        if not embeds:
            return body
        suffix = "\n\n" + "\n".join(embeds)
        return body.rstrip("\n") + suffix

    def _written_page_paths(self, plan: dict[str, Any]) -> list[str]:
        """Return the page paths written by :meth:`curate` (the ``_written`` key)."""
        written = plan.get("_written")
        return list(written) if isinstance(written, list) else []

    def _page_tags(self, page_paths: list[str]) -> list[str]:
        """Collect the curated pages' frontmatter tags for the success log (issue #52).

        Reads each curated page's ``tags`` and returns the de-duplicated,
        order-preserving union; an unreadable page or a missing/ill-typed ``tags`` value
        is skipped so the observability line never raises or blocks a good ingest.
        """
        seen: list[str] = []
        for path in page_paths:
            try:
                page = self._vault.read_page(path)
            except VaultError:
                continue
            tags = page.frontmatter.get("tags")
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if isinstance(tag, str) and tag and tag not in seen:
                    seen.append(tag)
        return seen

    # ---- pass 5: navigation ------------------------------------------------------

    def _apply_navigation(self, plan: dict[str, Any], page_paths: list[str]) -> None:
        """Append a ``log.md`` block for every file touched (SPEC step 5).

        A reference page's one-line gloss is its own ``summary:`` frontmatter (routed in
        at write time by :meth:`_write_planned_page`), so there is no separate
        ``index.md`` catalog to maintain here -- ``index.md`` is a static set of Bases
        dashboards (ADR 0008). The only navigation edit left is the append-only
        ``log.md`` entry recording the touched paths.
        """
        try:
            self._vault.append_log("ingest", self._log_subject(plan), page_paths)
        except VaultError as exc:
            raise IngestError(f"log update failed: {exc}") from exc

    @staticmethod
    def _log_subject(plan: dict[str, Any]) -> str:
        """Build the log subject from the plan's log block or its first page title."""
        log = plan.get("log")
        if isinstance(log, dict):
            subject = log.get("subject")
            if isinstance(subject, str) and subject.strip():
                return subject
        pages = plan.get("pages")
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            title = pages[0].get("frontmatter", {}).get("title")
            if isinstance(title, str) and title.strip():
                return title
        return "capture"

    # ---- pass 6: retain ----------------------------------------------------------

    def _retain_pages(self, page_paths: list[str], cls: Classification) -> None:
        """Retain each curated page into Hindsight and probe that it landed.

        The page body is read back from the (already durable) vault file and retained
        keyed by its vault path; a ``probe`` confirms recall returns the path. A
        Hindsight failure is surfaced (the vault write is already durable), so the page
        is never silently lost (SPEC steps 6-7 ordering).

        A daily-budget trip (:class:`~thoth.budget.BudgetExceededError`) during retain
        is **not** an error: the curated page is already on disk and committed, so
        indexing is simply deferred to the next reindex (which re-retains every changed
        page). The remaining pages are left unindexed too and the pass returns cleanly
        -- the capture is filed, never lost, just not yet searchable (issue #16).

        Raises:
            IngestError: if a retain call fails (the page is still on disk).
        """
        for rel in page_paths:
            try:
                page = self._vault.read_page(rel)
            except VaultError:
                continue
            facts = self._retain_facts(page.frontmatter, page.body)
            try:
                self._hindsight.retain(rel, facts, tags=[cls.page_type, rel])
            except BudgetExceededError:
                # Cap reached mid-ingest: the page is durable on disk and will be
                # indexed by the next reindex; stop retaining rather than fail (#16).
                return
            except HindsightError as exc:
                raise IngestError(
                    f"hindsight retain failed for {rel} (page is filed on disk): {exc}"
                ) from exc
            # Best-effort 'did it land?' probe; a False does not abort the ingest.
            try:
                self._hindsight.probe(rel, cls.title)
            except HindsightError:
                pass

    @staticmethod
    def _retain_facts(frontmatter: dict[str, object], body: str) -> str:
        """Compose the fact text retained for a page (title line + body)."""
        title = frontmatter.get("title")
        header = f"{title}\n\n" if isinstance(title, str) and title else ""
        return f"{header}{body}".strip()

    # ---- pass 7: commit ----------------------------------------------------------

    def _commit(
        self, report: IngestReport, cls: Classification, *, do_commit: bool = True
    ) -> IngestReport:
        """Commit the batch; surface a rebase conflict as a fail-loud report.

        ``do_commit=False`` (the ``thoth capture`` batch path, issue #80) defers the git
        work to the caller: the navigation log was already appended, the pages are
        staged in the working tree, and the caller commits+pushes the whole batch via
        :meth:`thoth.git_sync.GitSync.commit`. This method then returns the report
        unchanged (``committed=False``) without touching git.

        Returns:
            The report with ``committed``/``conflict``/``message`` populated.

        Raises:
            IngestError: on a non-conflict git failure.
        """
        if not do_commit:
            return _replace_report(
                report,
                committed=False,
                conflict=False,
                message=(
                    f"Filed {len(report.page_paths)} page(s) (batch commit pending)."
                ),
            )
        subject = cls.title or "capture"
        try:
            result = self._git.commit(subject)
        except VaultConflictError as exc:
            return _replace_report(
                report,
                committed=False,
                conflict=True,
                message=(
                    "VAULT CONFLICT: content is filed locally but the push was "
                    "refused; resolve in Obsidian. Paths: "
                    f"{', '.join(report.page_paths)} ({exc})"
                ),
            )
        except GitSyncError as exc:
            raise IngestError(f"commit failed: {exc}") from exc
        if result.committed:
            # A non-empty vault-commit ran the rebase + push to completion, so the
            # remote is now current -- record the push liveness marker (issue #15).
            self._record_marker(MARKER_PUSH)
        return _replace_report(
            report,
            committed=result.committed,
            conflict=False,
            message=f"Filed {len(report.page_paths)} page(s).",
        )

    def _commit_deferred(
        self, holding: _Holding, exc: LLMUnavailableError, *, do_commit: bool = True
    ) -> IngestReport:
        """Commit the durable holding page; report deferred curation (SPEC section 6).

        The inbound item is already on disk (``inbox/`` holding page); the LLM was
        unavailable, so classify/curate are skipped. The holding page is logged and
        committed (best-effort -- a conflict or git failure is surfaced on the report,
        not raised, since the capture is already durable locally), and a ``deferred``
        report is returned so the Slack/MCP reply can say "saved raw, curation deferred"
        and a later reindex/sweep re-curates the held item.

        ``do_commit=False`` (the ``thoth capture`` batch path, issue #80) keeps the
        navigation log append (the hold is staged in the working tree) but leaves the
        git commit to the caller's batched commit -- exactly like the non-deferred
        :meth:`_commit`, so the deferred path is covered too.

        Args:
            holding: The durable pre-LLM holding write.
            exc: The :class:`LLMUnavailableError` that triggered the deferral.
            do_commit: When ``False``, append the log but defer the git commit to the
                caller's batch commit.

        Returns:
            A ``deferred`` :class:`IngestReport` naming the held raw page.
        """
        rel = holding.result.raw_path
        raw_paths = [rel] if rel is not None else []
        try:
            self._vault.append_log("ingest", "deferred capture", raw_paths)
        except VaultError:
            # Navigation is best-effort here; the durable hold is what matters.
            pass
        report = IngestReport(
            page_paths=[],
            raw_paths=raw_paths,
            asset_paths=list(holding.result.asset_paths),
            obsidian_links=[],
            wikilinks=[],
            committed=False,
            conflict=False,
            deferred=True,
            message=(
                f"Saved raw, curation deferred ({exc}). The item is held durably "
                "in inbox/ but is not re-curated automatically -- re-run the capture "
                "to curate it once capacity is available."
            ),
        )
        if not do_commit:
            # The batch caller (thoth capture) commits the run; the hold is staged.
            return report
        try:
            result = self._git.commit("deferred capture")
        except VaultConflictError as conflict:
            return _replace_report(
                report,
                committed=False,
                conflict=True,
                message=(
                    "Saved raw locally, curation deferred (LLM unavailable), but the "
                    f"push was refused; resolve in Obsidian. ({conflict})"
                ),
            )
        except GitSyncError:
            # The hold is durable locally even if the push failed; do not raise.
            return report
        if result.committed:
            self._record_marker(MARKER_PUSH)
        return _replace_report(
            report,
            committed=result.committed,
            conflict=False,
            message=report.message,
        )

    # ---- pass 8: report ----------------------------------------------------------

    def _build_report(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        page_paths: list[str],
        plan: dict[str, Any],
    ) -> IngestReport:
        """Assemble the report with harness-built ``obsidian://`` links and wikilinks.

        Every link is built by :meth:`thoth.vault.Vault.obsidian_uri` from a confined
        path, so the model cannot fabricate a link to a page that does not exist. The
        per-page ``titles`` run parallel to ``page_paths`` (both are ordered from
        ``plan["pages"]``, whose ``_written`` paths are ``page_paths``): each title is
        the page's ``frontmatter.title``, falling back to the slug-derived title when
        absent or empty, so the Slack renderer can label every link (issue #53).
        """
        links = [self._vault.obsidian_uri(rel) for rel in page_paths]
        wikilinks = [f"[[{PurePosixPath(rel).stem}]]" for rel in page_paths]
        titles = self._page_titles(plan, page_paths)
        raw_paths = [raw.raw_path] if raw.raw_path is not None else []
        return IngestReport(
            page_paths=list(page_paths),
            raw_paths=raw_paths,
            asset_paths=list(raw.asset_paths),
            obsidian_links=links,
            wikilinks=wikilinks,
            titles=titles,
            committed=False,
            conflict=False,
            message="",
        )

    @staticmethod
    def _page_titles(plan: dict[str, Any], page_paths: list[str]) -> list[str]:
        """Build a per-page title list parallel to ``page_paths`` (issue #53).

        Reads ``frontmatter.title`` from each ``plan["pages"]`` entry (ordered the same
        as ``page_paths``), falling back to the slug-derived title from the written path
        when a plan title is missing or blank. Pages without a matching plan entry
        (uneven lengths) still get a slug-derived title so the renderer never indexes
        off the end.
        """
        pages = plan.get("pages")
        plan_pages = pages if isinstance(pages, list) else []
        titles: list[str] = []
        for index, rel in enumerate(page_paths):
            title = ""
            if index < len(plan_pages):
                page = plan_pages[index]
                if isinstance(page, dict):
                    frontmatter = page.get("frontmatter")
                    if isinstance(frontmatter, dict):
                        title = str(frontmatter.get("title") or "").strip()
            if not title:
                title = PurePosixPath(rel).stem.replace("-", " ").title()
            titles.append(title)
        return titles

    # ---- prompt builders ---------------------------------------------------------

    def _classify_prompt(
        self, capture: Capture, *, analysis: Analysis | None = None
    ) -> str:
        """Build the cheap classify-call prompt from the capture.

        The legal ``type`` enumeration is derived from
        :data:`thoth.vault.TYPE_ENUMERATION` (the canonical vocabulary, issue #19),
        not restated here, so a type added to the vault contract is offered to the
        classifier automatically and the two cannot diverge. A binary capture's analysis
        (issue #42) is folded in so the model classifies by the asset's real content.
        """
        what = self._capture_summary(capture, analysis=analysis)
        type_list = ", ".join(TYPE_ENUMERATION)
        return (
            "Classify this captured item for a personal knowledge vault. Return ONLY a "
            f"JSON object with keys: type (one of {type_list}), slug "
            "(lowercase-hyphen), title, entities (list of names), and concepts (list "
            "of names). Use 'note' for anything written (a concept, comparison, or "
            "query, differentiated by a tag); use 'action' for a todo or a to-consume "
            "item (a media item is an action tagged 'media').\n\n"
            f"Captured item:\n{what}"
        )

    def _curate_prompt(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        candidates: list[str],
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> str:
        """Build the curate-call prompt (the file-plan contract + classification + raw).

        The model returns the plan by CALLING the ``submit_file_plan`` tool (forced via
        ``tool_choice``), so the plan is a structured ``tool_use.input`` dict the SDK
        escapes -- it can never break JSON parsing (issue #110). The exact field/enum
        contract is embedded verbatim from :func:`thoth.llm.file_plan_contract_text`
        (rendered from the same constants the validator enforces) so the model knows the
        shape the tool input must satisfy; ``validate_file_plan`` remains the gate. A
        binary capture's analysis (issue #42) is included so the curated body holds the
        asset's real OCR'd/extracted content; ``extracted_body`` does the same for an
        audio transcript / URL article body (which the model cannot read off the raw
        page path).
        """
        candidate_block = "\n".join(f"- {path}" for path in candidates) or "(none)"
        raw_block = raw.raw_path or "(no raw page)"
        asset_block = ", ".join(raw.asset_paths) or "(none)"
        summary = self._capture_summary(
            capture, analysis=analysis, extracted_body=extracted_body
        )
        return (
            "Given the SCHEMA (in the system prompt) and the captured item below, file "
            "it into the vault by CALLING the submit_file_plan tool with the file "
            "plan.\n\n"
            f"{file_plan_contract_text()}\n\n"
            f"Classification: type={cls.page_type} slug={cls.slug} title={cls.title}\n"
            f"Raw source page: {raw_block}\n"
            f"Saved assets (embed with ![[name]]): {asset_block}\n"
            f"Existing candidate pages to maybe update:\n{candidate_block}\n\n"
            f"Captured item:\n{summary}"
        )

    @staticmethod
    def _capture_summary(
        capture: Capture,
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> str:
        """Render a compact textual summary of the capture for a prompt.

        For a binary capture the analysis (issue #42) is appended so the model sees the
        asset's OCR'd/extracted content, description, and routing hints -- the load-
        bearing fix: previously a binary reached the model as a bare ``File: name`` line
        and was filed blind. ``extracted_body`` (an audio transcript / URL article body
        extracted before curate) is appended for the same reason, but only when the
        capture has no inline ``text`` -- which is already shown verbatim -- so a plain
        text capture is never duplicated.
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
        if capture.text is None and extracted_body and extracted_body.strip():
            label = "Extracted text (transcript / article body)"
            summary += f"\n\n{label}:\n{extracted_body.strip()}"
        return summary

    # ---- shared parse helper -----------------------------------------------------

    @staticmethod
    def _parse_block(response: Any, what: str) -> dict[str, Any]:
        """Extract text from a response and parse its first JSON object.

        Raises:
            IngestError: if no parseable JSON object is found.
        """
        from thoth.llm import extract_text

        text = extract_text(response)
        try:
            return parse_json_block(text)
        except LLMError as exc:
            raise IngestError(
                f"could not parse {what} from model output: {exc}"
            ) from exc


# --- small module-level helpers ----------------------------------------------------


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


def _str_list(value: object) -> list[str]:
    """Return ``value`` as a list of non-empty strings (empty list otherwise)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _embed_name(asset_filename: str) -> str:
    """Map an asset filename to the name Obsidian must embed to *render* it (issue #68).

    For an Excalidraw drawing stored as ``<slug>.excalidraw.md``, the trailing ``.md``
    must be dropped: Obsidian's basename for that file is ``<slug>.excalidraw``, and the
    Excalidraw plugin only renders the *drawing* for an ``![[<slug>.excalidraw]]`` embed
    -- ``![[<slug>.excalidraw.md]]`` embeds the markdown note instead, showing the raw
    scene JSON (the issue #68 live-verify failure). Every other asset embeds by its bare
    filename unchanged.
    """
    if asset_filename.endswith(".excalidraw.md"):
        return asset_filename[: -len(".md")]
    return asset_filename


def _merge_terms(primary: list[str], extra: list[str]) -> list[str]:
    """Union two term lists, order-preserving and case-insensitively de-duplicated.

    The model's own classify terms come first (so they drive the candidate fetch order),
    then any analysed entities/concepts not already present (issue #42).
    """
    seen = {term.lower() for term in primary}
    merged = list(primary)
    for term in extra:
        if term.lower() not in seen:
            merged.append(term)
            seen.add(term.lower())
    return merged


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


def _cleanup_fetched(fetched: FetchedBinary | None) -> None:
    """Unlink an analyse-pass URL binary's staged temp file when not consumed.

    On the happy path :meth:`Ingestor.capture_raw` reuses and cleans up the staged tmp
    (via the asset store's move/unlink). This guards the paths where ``capture_raw``
    never runs -- a classify/curate/analyse deferral -- so the ``thoth-fetch-*`` temp
    file is removed rather than leaked. A best-effort unlink: a missing file is fine.
    """
    if fetched is None:
        return
    fetched.tmp_path.unlink(missing_ok=True)


def _curate_repair_prompt(problems: str) -> str:
    """Build the corrective retry prompt that feeds the problems back to the model.

    Sent as the follow-up user turn after a rejected plan (the prior assistant turn
    carries the model's ``submit_file_plan`` tool call), so the model sees exactly what
    failed and fixes it rather than the capture aborting. The problem string may be a
    :func:`validate_file_plan` message OR "curate did not call submit_file_plan tool"
    (the model failed to call the tool at all), so the wording stays generic and
    references the tool either way.
    """
    return (
        "Your previous submit_file_plan call was REJECTED -- the following problems "
        f"were found:\n{problems}\n\n"
        "Call submit_file_plan again with a corrected plan that fixes EVERY problem "
        "above and matches the required shape exactly."
    )


def _curate_repair_turn(response: Any, problems: str) -> Message:
    """Build the user turn that feeds curate problems back for the corrective retry.

    The shape depends on how the prior assistant turn ended (issue #110 reviewer fix):

    * If the assistant CALLED ``submit_file_plan`` (the normal forced-tool path, where
      the plan merely failed :func:`validate_file_plan`), the Messages API requires the
      next user turn to OPEN with a ``tool_result`` block keyed to that ``tool_use``
      block's id -- a plain-text turn after a ``tool_use`` block is a 400
      ("tool_use ids were found without tool_result blocks immediately after"). So the
      turn leads with ``tool_result(tool_use_id, repair_text, is_error=True)``.
    * If the assistant did NOT call the tool (the "did not call submit_file_plan" case),
      its turn is plain text, so a plain-text user follow-up is valid and no
      ``tool_result`` is owed.

    Args:
        response: The rejected curate response (its assistant turn was just echoed).
        problems: The validation/parse problems to feed back.

    Returns:
        A user :class:`Message` whose content is API-valid for the echoed assistant
        turn.
    """
    text = _curate_repair_prompt(problems)
    for block in _tool_use_blocks(response):
        if _block_name(block) == "submit_file_plan":
            return Message(
                role="user",
                content=[tool_result_block(_block_id(block), text, is_error=True)],
            )
    return Message(role="user", content=text)


def _replace_report(
    report: IngestReport, *, committed: bool, conflict: bool, message: str
) -> IngestReport:
    """Return a copy of ``report`` with the commit-outcome fields set."""
    return IngestReport(
        page_paths=report.page_paths,
        raw_paths=report.raw_paths,
        asset_paths=report.asset_paths,
        obsidian_links=report.obsidian_links,
        wikilinks=report.wikilinks,
        titles=report.titles,
        committed=committed,
        conflict=conflict,
        deferred=report.deferred,
        unchanged=report.unchanged,
        message=message,
    )
