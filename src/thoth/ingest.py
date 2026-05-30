"""The bounded-pass capture pipeline that files an inbound item into the vault.

This module is the orchestration core of capture (SPEC section 6). It runs a fixed,
ordered sequence of *validated passes* over one :class:`Capture` and never lets the
appliance LLM touch disk or the network directly: every byte that reaches the vault
goes through :class:`thoth.vault.Vault` (so paths are confined and the folder/type/slug
contract is enforced) and every web fetch goes through the SSRF-guarded
:class:`thoth.extract.Extractor`. git is a deterministic collaborator, never an LLM
tool. The nine passes are:

0. **orient** -- :meth:`thoth.git_sync.GitSync.pull` so writes land on current state.
1. **classify** -- one cheap Claude call -> a :class:`Classification` whose ``type`` and
   ``slug`` are validated through :class:`~thoth.vault.Vault` before use.
2. **capture raw** -- :class:`~thoth.extract.Extractor` by kind; the body SHA-256 is
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
5. **navigation** -- :meth:`~thoth.vault.Vault.append_index` for knowledge pages and
   :meth:`~thoth.vault.Vault.append_log` for every file touched.
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

import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, overload

from thoth.config import Config
from thoth.extract import ExtractError, Extractor, FetchedBinary
from thoth.git_sync import GitSync, GitSyncError, VaultConflictError
from thoth.hindsight import Hindsight, HindsightError
from thoth.llm import (
    LLM,
    LLMError,
    Message,
    SchemaValidationError,
    parse_json_block,
    validate_file_plan,
)
from thoth.vault import SchemaError, SlugError, Vault, VaultError, redact_secrets

__all__ = [
    "Capture",
    "CaptureKind",
    "Classification",
    "IngestError",
    "IngestReport",
    "Ingestor",
    "RawCaptureResult",
]

# Folders scanned by the read-only create-vs-update candidate search (curated layer).
_CANDIDATE_DIRS: tuple[str, ...] = (
    "entities",
    "concepts",
    "comparisons",
    "queries",
    "people",
)

# File extensions (no dot) that select a binary/audio capture kind.
_IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
_AUDIO_EXTS: frozenset[str] = frozenset({"mp3", "wav", "m4a", "ogg", "flac"})

# Knowledge ``type`` -> the index.md catalog section append_index targets.
_INDEX_SECTION_BY_TYPE: dict[str, str] = {
    "entity": "Entities",
    "concept": "Concepts",
    "comparison": "Comparisons",
    "query": "Queries",
}


class IngestError(Exception):
    """Raised when an ingest pass fails validation, extraction, or a vault write."""


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

    Binary bytes never travel as base64 (SPEC section 6): an image/PDF/audio capture
    carries a ``path`` the *server* can read (downloaded by the Slack/MCP layer to a tmp
    file) or a ``url`` the server fetches itself.

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
        life_admin: Parsed life-admin fields (``due``/``priority``/...), empty for
            knowledge captures.
    """

    page_type: str
    slug: str
    title: str
    entities: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    life_admin: dict[str, Any] = field(default_factory=dict)


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
class IngestReport:
    """Structured outcome the Slack/MCP layer renders (SPEC step 8).

    Attributes:
        page_paths: Curated page paths written/updated.
        raw_paths: Raw source page paths written (may be empty).
        asset_paths: Binary asset paths saved (may be empty).
        obsidian_links: ``obsidian://`` deep links built by the harness via
            :meth:`thoth.vault.Vault.obsidian_uri` (one per curated page; unfabricable).
        wikilinks: ``[[slug]]`` handles for the curated pages.
        committed: Whether :meth:`thoth.git_sync.GitSync.commit` made a commit.
        conflict: Whether a :class:`~thoth.git_sync.VaultConflictError` was surfaced.
        message: A short human-readable status line.
    """

    page_paths: list[str]
    raw_paths: list[str]
    asset_paths: list[str]
    obsidian_links: list[str]
    wikilinks: list[str]
    committed: bool
    conflict: bool = False
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
        """
        self._config = config
        self._vault = vault
        self._llm = llm
        self._extractor = extractor
        self._hindsight = hindsight
        self._git = git
        self._schema_md = schema_md

    # ---- the full pipeline -------------------------------------------------------

    def ingest(self, capture: Capture) -> IngestReport:
        """Run all nine passes and return a structured report.

        Pulls the vault, classifies, captures the raw source (idempotent on body
        SHA-256), fetches candidate pages, curates and writes the file-plan, updates
        navigation, retains into the index, and commits. A rebase conflict at commit is
        surfaced as ``IngestReport.conflict`` (the content is already filed locally; no
        ``--force``). No curated/navigation write happens until validation passes, so a
        rejected plan leaves nothing beyond a possible raw page on disk.

        Args:
            capture: The inbound item to ingest.

        Returns:
            The :class:`IngestReport` describing every file touched.

        Raises:
            IngestError: on a classification, extraction, validation, or non-conflict
                git failure.
        """
        self._orient()
        classification = self.classify(capture)
        raw = self.capture_raw(capture, classification)
        candidates = self.fetch_candidates(classification)
        plan = self.curate(capture, classification, raw, candidates)

        page_paths = self._written_page_paths(plan)
        self._apply_navigation(plan, page_paths)
        self._retain_pages(page_paths, classification)

        report = self._build_report(capture, classification, raw, page_paths)
        return self._commit(report, classification)

    # ---- pass 0: orient ----------------------------------------------------------

    def _orient(self) -> None:
        """Pull the vault so writes land on current state (SPEC step 0)."""
        try:
            self._git.pull()
        except GitSyncError as exc:
            raise IngestError(f"vault pull failed before ingest: {exc}") from exc

    # ---- pass 1: classify --------------------------------------------------------

    def classify(self, capture: Capture) -> Classification:
        """Run the cheap classify call and validate its routing output.

        One LLM call returns a JSON object with ``type``/``slug``/``title`` plus any
        named entities/concepts and life-admin fields. The ``type`` and ``slug`` are
        validated through :class:`~thoth.vault.Vault` here, so a bad routing decision is
        rejected before any disk is touched.

        Args:
            capture: The inbound item to classify.

        Returns:
            The validated :class:`Classification`.

        Raises:
            IngestError: if the model output is unparseable or names an
                out-of-vocabulary type or an invalid slug.
        """
        prompt = self._classify_prompt(capture)
        try:
            response = self._llm.complete([Message(role="user", content=prompt)])
        except Exception as exc:  # noqa: BLE001 - any client failure aborts classify
            raise IngestError(f"classify LLM call failed: {exc}") from exc
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
        from thoth.vault import VALID_TYPES

        if page_type not in VALID_TYPES:
            raise IngestError(
                f"classification type {page_type!r} is not a valid vault type"
            )

        title = obj.get("title")
        if not isinstance(title, str) or not title.strip():
            title = slug.replace("-", " ").title()
        life_admin = obj.get("life_admin")
        return Classification(
            page_type=page_type,
            slug=slug,
            title=title,
            entities=_str_list(obj.get("entities")),
            concepts=_str_list(obj.get("concepts")),
            life_admin=life_admin if isinstance(life_admin, dict) else {},
        )

    # ---- pass 2: capture raw -----------------------------------------------------

    def capture_raw(self, capture: Capture, cls: Classification) -> RawCaptureResult:
        """Extract the immutable source and write it under ``raw/`` (idempotent).

        Dispatches on the capture kind: a URL is extracted to clean markdown, a PDF or
        image is downloaded as a binary into ``raw/assets/`` via
        :meth:`thoth.extract.Extractor.fetch_binary` + :meth:`Vault.save_asset`, audio
        is transcribed, and plain text is filed verbatim. For text/markdown sources the
        body SHA-256 is compared to any existing raw page's stored digest *before*
        writing: an identical body is skipped (``'skipped_unchanged'``) and a changed
        body is flagged and rewritten (``'updated_drift'``). Images never become base64.

        Args:
            capture: The inbound item.
            cls: Its validated classification (supplies the raw slug).

        Returns:
            A :class:`RawCaptureResult` recording the path and disposition.

        Raises:
            IngestError: on extraction failure (wraps
                :class:`thoth.extract.ExtractError`) or a vault write error.
        """
        kind = self._capture_kind(capture)
        try:
            if kind is CaptureKind.IMAGE:
                return self._capture_image(capture, cls)
            if kind is CaptureKind.URL:
                doc = self._extractor.web_extract(_require(capture.url, "url"))
                return self._write_raw_doc(
                    "articles", cls, doc.markdown, doc.source_url
                )
            if kind is CaptureKind.PDF:
                return self._capture_pdf(capture, cls)
            if kind is CaptureKind.AUDIO:
                transcript = self._extractor.transcribe(_require(capture.path, "path"))
                return self._write_raw_doc("transcripts", cls, transcript, None)
            # TEXT
            return self._write_raw_doc(
                "articles", cls, _require(capture.text, "text"), None
            )
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
    ) -> dict[str, Any]:
        """Run the curate call, validate the file-plan, and write every page.

        A second LLM call returns a file-plan; it is validated by
        :func:`thoth.llm.validate_file_plan` (which reuses the same vault validators)
        then each page is written through :meth:`thoth.vault.Vault.write_page`, which
        re-validates the folder/type/slug contract and confines the path. A plan that
        tries to escape the vault root or violates the contract is rejected and nothing
        is written for the offending page.

        Args:
            capture: The inbound item (for context).
            cls: The validated classification.
            raw: The raw-capture result (its path/embeds are offered to the model).
            candidates: Existing candidate page paths.

        Returns:
            The validated file-plan object (with a private list of written page paths
            attached under ``"_written"``).

        Raises:
            IngestError: if the model output is unparseable, the plan fails validation,
                or a vault write rejects a page.
        """
        prompt = self._curate_prompt(capture, cls, raw, candidates)
        try:
            response = self._llm.complete(
                [Message(role="user", content=prompt)],
                system_extra=self._schema_md,
            )
        except Exception as exc:  # noqa: BLE001 - any client failure aborts curate
            raise IngestError(f"curate LLM call failed: {exc}") from exc
        plan = self._parse_block(response, "file plan")
        try:
            validate_file_plan(plan)
        except SchemaValidationError as exc:
            raise IngestError(f"file plan rejected: {exc}") from exc

        written: list[str] = []
        pages = plan.get("pages")
        assert isinstance(pages, list)  # guaranteed by validate_file_plan
        for page in pages:
            written.append(self._write_planned_page(page, capture.source, raw))
        plan["_written"] = written
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

    # ---- internals: raw capture --------------------------------------------------

    def _capture_kind(self, capture: Capture) -> CaptureKind:
        """Decide the capture kind from the populated fields and any extension hint.

        A server-resolvable ``path`` is always a binary/audio capture (a path with no
        recognised extension is treated as an image, the common phone-upload case). A
        ``url`` is web-extracted unless its own extension or the ``filename`` hint marks
        it as a PDF or image (a direct binary the server downloads). Plain ``text`` is
        the fallback.
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
        # write_raw stores sha256 over the *redacted* body, so the idempotency compare
        # must use the same digest or a re-ingest of secret-bearing text never matches.
        new_sha = Vault.body_sha256(redact_secrets(body))
        existing_sha = self._existing_raw_sha(rel)
        if existing_sha is not None and existing_sha == new_sha:
            return RawCaptureResult(raw_path=rel, disposition="skipped_unchanged")
        disposition = "updated_drift" if existing_sha is not None else "created"
        meta: dict[str, object] = {}
        if source_url is not None:
            meta["source_url"] = source_url
        self._vault.write_raw(subdir, cls.slug, meta, body)
        return RawCaptureResult(raw_path=rel, disposition=disposition)

    def _capture_pdf(self, capture: Capture, cls: Classification) -> RawCaptureResult:
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
            fetched = self._extractor.fetch_binary(capture.url)
            asset_result = self._save_fetched_asset(cls, fetched)
            source_url: str | None = fetched.source_url
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

    def _capture_image(self, capture: Capture, cls: Classification) -> RawCaptureResult:
        """Download/stage an image binary into ``raw/assets`` (never base64)."""
        if capture.url is not None:
            fetched = self._extractor.fetch_binary(capture.url)
            return self._save_fetched_asset(cls, fetched)
        path = _require(capture.path, "path")
        ext = (capture.filename or path.name).rsplit(".", 1)[-1].lower()
        return self._save_local_asset_result(cls, path, ext)

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
        self, page: dict[str, Any], source: str, raw: RawCaptureResult
    ) -> str:
        """Write one validated file-plan page through the confined vault helper.

        ``write_page`` re-validates the folder/type/slug contract and confines the path,
        so a plan that slipped a bad folder or an escaping slug past the schema check is
        still rejected here.
        """
        folder = page["folder"]
        slug = page["slug"]
        frontmatter = dict(page["frontmatter"])
        frontmatter.setdefault("source", source)
        body = page["body"]
        body = self._append_embeds(body, page, raw)
        try:
            return self._vault.write_page(folder, slug, frontmatter, body)
        except (SchemaError, SlugError, VaultError) as exc:
            raise IngestError(
                f"vault rejected planned page {folder}/{slug}: {exc}"
            ) from exc

    @staticmethod
    def _append_embeds(body: str, page: dict[str, Any], raw: RawCaptureResult) -> str:
        """Append Obsidian ``![[asset]]`` embeds for saved assets not already in body.

        Uses the bare asset filename (Obsidian resolves embeds vault-wide), never a
        base64 blob. Embeds already present in the model's body are left as-is.
        """
        embeds: list[str] = []
        for asset_rel in raw.asset_paths:
            name = PurePosixPath(asset_rel).name
            embed = f"![[{name}]]"
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

    # ---- pass 5: navigation ------------------------------------------------------

    def _apply_navigation(self, plan: dict[str, Any], page_paths: list[str]) -> None:
        """Append index entries for knowledge pages and a log block for all touches.

        ``index_entries`` from the plan are applied when present; otherwise a default
        entry is derived for each written knowledge page. Life-admin pages are surfaced
        by Bases views and get no index entry (SPEC step 5).
        """
        entries = plan.get("index_entries")
        if isinstance(entries, list) and entries:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                section = entry.get("section")
                wikilink = entry.get("wikilink")
                summary = entry.get("summary", "")
                if isinstance(section, str) and isinstance(wikilink, str):
                    try:
                        self._vault.append_index(section, wikilink, str(summary))
                    except VaultError as exc:
                        raise IngestError(f"index update failed: {exc}") from exc
        else:
            self._default_index_entries(plan, page_paths)

        try:
            self._vault.append_log("ingest", self._log_subject(plan), page_paths)
        except VaultError as exc:
            raise IngestError(f"log update failed: {exc}") from exc

    def _default_index_entries(
        self, plan: dict[str, Any], page_paths: list[str]
    ) -> None:
        """Derive one catalog entry per written knowledge page from the plan."""
        pages = plan.get("pages")
        if not isinstance(pages, list):
            return
        for page, rel in zip(pages, page_paths, strict=True):
            if not isinstance(page, dict):
                continue
            folder = PurePosixPath(rel).parent.name
            page_type = page.get("frontmatter", {}).get("type")
            section = _INDEX_SECTION_BY_TYPE.get(str(page_type))
            if section is None and folder == "people":
                section = "People"
            if section is None:
                continue
            slug = PurePosixPath(rel).stem
            title = page.get("frontmatter", {}).get("title", slug)
            try:
                self._vault.append_index(section, slug, str(title))
            except VaultError as exc:
                raise IngestError(f"index update failed: {exc}") from exc

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

    def _commit(self, report: IngestReport, cls: Classification) -> IngestReport:
        """Commit the batch; surface a rebase conflict as a fail-loud report.

        Returns:
            The report with ``committed``/``conflict``/``message`` populated.

        Raises:
            IngestError: on a non-conflict git failure.
        """
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
        return _replace_report(
            report,
            committed=result.committed,
            conflict=False,
            message=f"Filed {len(report.page_paths)} page(s).",
        )

    # ---- pass 8: report ----------------------------------------------------------

    def _build_report(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        page_paths: list[str],
    ) -> IngestReport:
        """Assemble the report with harness-built ``obsidian://`` links and wikilinks.

        Every link is built by :meth:`thoth.vault.Vault.obsidian_uri` from a confined
        path, so the model cannot fabricate a link to a page that does not exist.
        """
        links = [self._vault.obsidian_uri(rel) for rel in page_paths]
        wikilinks = [f"[[{PurePosixPath(rel).stem}]]" for rel in page_paths]
        raw_paths = [raw.raw_path] if raw.raw_path is not None else []
        return IngestReport(
            page_paths=list(page_paths),
            raw_paths=raw_paths,
            asset_paths=list(raw.asset_paths),
            obsidian_links=links,
            wikilinks=wikilinks,
            committed=False,
            conflict=False,
            message="",
        )

    # ---- prompt builders ---------------------------------------------------------

    def _classify_prompt(self, capture: Capture) -> str:
        """Build the cheap classify-call prompt from the capture."""
        what = self._capture_summary(capture)
        return (
            "Classify this captured item for a personal knowledge vault. Return ONLY a "
            "JSON object with keys: type (one of entity, concept, comparison, query, "
            "summary, action, media, memory, inbox), slug (lowercase-hyphen), title, "
            "entities (list of names), concepts (list of names), and life_admin "
            "(object with due/priority/etc, or empty).\n\n"
            f"Captured item:\n{what}"
        )

    def _curate_prompt(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        candidates: list[str],
    ) -> str:
        """Build the curate-call prompt (classification + raw + candidates)."""
        candidate_block = "\n".join(f"- {path}" for path in candidates) or "(none)"
        raw_block = raw.raw_path or "(no raw page)"
        asset_block = ", ".join(raw.asset_paths) or "(none)"
        return (
            "Given the SCHEMA and the captured item, return ONLY a JSON file plan "
            "(see the file-plan schema): a 'pages' list of create/update pages with "
            "full frontmatter, body, and >=2 wikilinks each, optional 'index_entries', "
            "and a 'log' block.\n\n"
            f"Classification: type={cls.page_type} slug={cls.slug} title={cls.title}\n"
            f"Raw source page: {raw_block}\n"
            f"Saved assets (embed with ![[name]]): {asset_block}\n"
            f"Existing candidate pages to maybe update:\n{candidate_block}\n\n"
            f"Captured item:\n{self._capture_summary(capture)}"
        )

    @staticmethod
    def _capture_summary(capture: Capture) -> str:
        """Render a compact textual summary of the capture for a prompt."""
        parts: list[str] = []
        if capture.url is not None:
            parts.append(f"URL: {capture.url}")
        if capture.path is not None:
            parts.append(f"File: {capture.filename or capture.path.name}")
        if capture.text is not None:
            parts.append(f"Text: {capture.text}")
        return "\n".join(parts) or "(empty capture)"

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
        :attr:`CaptureKind.PDF`/``IMAGE``/``AUDIO`` for a known extension, else
        ``default``.
    """
    if name.endswith(".pdf"):
        return CaptureKind.PDF
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
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


def _require(value: Any, field_name: str) -> Any:
    """Return ``value`` or raise :class:`IngestError` naming the missing field."""
    if value is None:
        raise IngestError(f"capture is missing required field {field_name!r}")
    return value


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
        committed=committed,
        conflict=conflict,
        message=message,
    )
