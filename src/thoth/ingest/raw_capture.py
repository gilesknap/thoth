"""Passes 0b + 2: the durable pre-LLM holding write and idempotent raw capture."""

from __future__ import annotations

import hashlib

from thoth.extract import ExtractError, FetchedBinary
from thoth.vault import Vault, VaultError

from ._shared import (
    HOLD_MODE_AS_IS,
    HOLD_MODE_CURATE,
    Capture,
    CaptureKind,
    Classification,
    IngestError,
    RawCaptureResult,
    _Analysed,
    _Holding,
    _Prefetched,
    _require,
)
from .assets import _AssetStore


class _RawCapturePass(_AssetStore):
    """Pass 0b (persist inbound durably) and pass 2 (idempotent raw capture)."""

    # ---- durable pre-LLM capture (SPEC section 6: persist before classify) -------

    def persist_inbound(self, capture: Capture, *, as_is: bool = False) -> _Holding:
        """Extracts and persists the inbound item durably before any LLM call.

        This is the capture-never-lost guarantee: the text is on disk and committable
        before classify and curate run. The holding page lands at ``inbox/<sha12>.md``,
        whose body is the extracted text or, for a binary with no text yet, a provenance
        stub naming the source. The slug comes from the body SHA-256, so re-persisting
        identical content is idempotent.

        The curation mode and original filename are stamped into the frontmatter (issue
        #95) so a later inbox sweep honours the original intent rather than guessing.

        Extraction is the only network step and happens here, so an extract failure
        still aborts loudly. Nothing is lost, because there was nothing to persist.

        Args:
            capture: The inbound item.
            as_is: Record ``mode: as-is`` so a later sweep re-files low-touch.

        Returns:
            The holding result plus any prefetched extraction, for reuse by
            :meth:`capture_raw` without a second fetch.

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
            # A binary with no extracted text yet, so hold a provenance stub. The
            # capture stays durable and a later sweep can re-fetch and curate it
            body = self._binary_stub_body(capture)
        mode = HOLD_MODE_AS_IS if as_is else HOLD_MODE_CURATE
        try:
            result = self._write_inbox_holding(
                body, capture.source, mode=mode, filename=capture.filename
            )
        except VaultError as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc
        return _Holding(result=result, prefetched=prefetched)

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
        """Extracts the immutable source and writes it under ``raw/``, idempotently.

        Dispatches on the capture kind. A URL is extracted to clean markdown, a PDF or
        image is downloaded as a binary into ``raw/assets/``, audio is transcribed, and
        plain text is filed verbatim. Images never become base64.

        For textual sources the body SHA-256 is compared to any existing page's stored
        digest before writing, so an identical body is skipped and a changed one is
        flagged as drift and rewritten.

        Supplying ``prefetched`` or ``fetched`` reuses work the earlier passes already
        did, so a source is fetched or transcribed exactly once per ingest and a URL
        binary's temp file is never leaked. Calling this with neither re-fetches, which
        is the standalone behaviour.

        Args:
            capture: The inbound item.
            cls: Validated classification supplying the raw slug.
            prefetched: Text extracted before classify, or None to re-extract.
            fetched: A URL binary already downloaded, or None to re-fetch.
            derived: Best-effort enhancement artifacts (issue #68), saved as extra
                assets beside the original. None saves only the original.

        Returns:
            The path and disposition. For an image, ``asset_paths`` lists the original
            first, then any derived assets.

        Raises:
            IngestError: on an extraction failure or a vault write error.
        """
        kind = self._capture_kind(capture)
        try:
            if kind is CaptureKind.IMAGE:
                return self._capture_image(
                    capture, cls, fetched=fetched, derived=derived
                )
            if kind is CaptureKind.PDF:
                return self._capture_pdf(capture, cls, fetched=fetched)
            pre = (
                prefetched
                if prefetched is not None
                else self._extract_text(capture, kind)
            )
            assert pre is not None  # url, audio and text kinds always carry a body
            subdir = "transcripts" if kind is CaptureKind.AUDIO else "articles"
            return self._write_raw_doc(subdir, cls, pre.body, pre.source_url)
        except ExtractError as exc:
            raise IngestError(f"capture failed during extraction: {exc}") from exc
        except VaultError as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc

    # ---- internals: durable pre-LLM holding --------------------------------------

    def _extract_text(self, capture: Capture, kind: CaptureKind) -> _Prefetched | None:
        """Extracts the text body for a text-bearing capture, with no LLM.

        Runs the single network or IO step per kind: web-extract a URL, transcribe
        audio, read an uploaded text file (issue #57), or take inline text verbatim.

        Raises:
            ExtractError: on a web-extract or transcribe failure.
            IngestError: when a text capture has neither inline text nor a readable
                path.
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
        """Returns the body for a text capture: inline text, else the uploaded file.

        An uploaded file carries its body as the file itself (issue #57), so the path is
        read when no inline text is supplied. Decoding replaces bad bytes, so a stray
        non-UTF-8 byte in a log or CSV dump never aborts the capture.

        Raises:
            IngestError: if there is neither inline text nor a readable path.
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
        """Builds the holding body for a binary capture with no extracted text yet.

        The held page records the source URL or filename so a later sweep can re-fetch
        and curate it, and carries no base64 because the bytes are fetched server-side.
        The deferral reason is the unsupported binary content and not LLM availability
        (issue #57). A text upload is read directly and never lands here.
        """
        ref = capture.url or capture.filename or "(binary upload)"
        return (
            f"# Held capture\n\n"
            f"Binary source: `{ref}`\n\n"
            "_Unsupported binary content held at capture time; queued for a later "
            "reindex/sweep to fetch and curate._"
        )

    def _write_inbox_holding(
        self,
        body: str,
        source: str,
        *,
        mode: str = HOLD_MODE_CURATE,
        filename: str | None = None,
    ) -> RawCaptureResult:
        """Writes the durable ``inbox/<sha12>.md`` hold, idempotent on the body SHA.

        The slug is the first 12 hex chars of the body SHA-256, so re-persisting an
        identical body lands on the same path and is skipped. The page records ``type:
        inbox`` so a later sweep can find un-curated holds.

        The source is threaded through so a deferred item is held under its true
        provenance, and :meth:`Vault.write_page` validates it. The digest compare uses
        the same derivation the writer stamps, matching :meth:`_write_raw_doc`.

        Args:
            body: The extracted text, or a binary provenance stub, to hold.
            source: The capture's frontmatter source value.
            mode: Intended curation mode to stamp (issue #95), honoured by the sweep.
            filename: Original upload name, or None to keep the frontmatter minimal.

        Returns:
            The held page path and its disposition.
        """
        slug = f"hold-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]}"
        rel = f"inbox/{slug}.md"
        disposition = self._doc_disposition(rel, body)
        if disposition == "skipped_unchanged":
            return RawCaptureResult(raw_path=rel, disposition=disposition)
        meta: dict[str, object] = {
            "title": "Held capture",
            "type": "inbox",
            "source": source,
            # Stamp the body digest so re-persist is idempotent, mirroring write_raw
            "sha256": Vault.stored_body_sha256(body),
            # Stamp the curation mode (issue #95) so the inbox sweep re-files this hold
            # with the original intent rather than guessing
            "mode": mode,
        }
        if filename:
            meta["filename"] = filename
        self._vault.write_page("inbox", slug, meta, body)
        return RawCaptureResult(raw_path=rel, disposition=disposition)

    # ---- internals: raw capture --------------------------------------------------

    def _write_raw_doc(
        self,
        subdir: str,
        cls: Classification,
        body: str,
        source_url: str | None,
    ) -> RawCaptureResult:
        """Writes, or idempotently skips, a textual raw page after a SHA-256 compare.

        The digest is compared to any existing page's stored one before writing. Equal
        means skip, leaving the page and its mtime untouched, and different means drift
        and a rewrite. A brand-new path is created.

        Args:
            subdir: The ``raw/`` subdir, ``articles`` or ``transcripts``.
            cls: Validated classification supplying the slug.
            body: The raw markdown body.
            source_url: Provenance URL stamped into frontmatter, if any.
        """
        rel = f"raw/{subdir}/{cls.slug}.md"
        disposition = self._doc_disposition(rel, body)
        if disposition == "skipped_unchanged":
            return RawCaptureResult(raw_path=rel, disposition=disposition)
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
        """Keeps a PDF binary and writes a searchable ``raw/papers/<slug>.md`` page.

        The binary is staged into ``raw/assets/`` idempotently on its bytes, and the
        page records the source URL and a pointer to it, so curate and retrieval have a
        text body to surface (SPEC step 2). Full PDF text extraction is deferred to
        Phase 3, so the page is a provenance stub until then.

        Raises:
            IngestError: if the binary differs at an existing asset slug.
        """
        asset_result, source_url = self._obtain_primary_asset(
            capture, cls, fetched, local_ext="pdf"
        )
        return self._write_paper_stub(cls, asset_result, source_url)

    def _write_paper_stub(
        self,
        cls: Classification,
        asset_result: RawCaptureResult,
        source_url: str | None,
    ) -> RawCaptureResult:
        """Writes the ``raw/papers/<slug>.md`` provenance page for a kept PDF.

        The body names the kept binary so retrieval can follow it, and notes the
        deferred text extraction. The asset's paths are carried through so the report
        still lists the saved binary.
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
        """Downloads or stages an image binary into ``raw/assets``, never as base64.

        The original is always saved first and never replaced. Any enhancement artifacts
        the analyse pass derived (issue #68), such as an editable Excalidraw
        reconstruction of a hand-drawn diagram, are saved as extra assets under the same
        slug and merged in after it, so curate sees them and they are all embedded. Each
        derived asset goes through :meth:`_store_asset`, so it keeps the same
        bytes-SHA-256 idempotency as the original.
        """
        name = capture.filename or (
            capture.path.name if capture.path is not None else ""
        )
        ext = name.rsplit(".", 1)[-1].lower()
        original, _ = self._obtain_primary_asset(capture, cls, fetched, local_ext=ext)
        original = self._append_extra_images(capture, cls, original)
        return self._append_derived_assets(cls, original, derived)

    def _append_extra_images(
        self,
        capture: Capture,
        cls: Classification,
        original: RawCaptureResult,
    ) -> RawCaptureResult:
        """Saves a multi-image batch's extra images under the same slug (issue #84).

        A Slack message carrying several images at once is one capture, so each extra is
        saved next to the primary under a numbered slug in upload order and merged in
        after it, so they are all embedded in the one curated page. Each goes through
        :meth:`_store_asset` and keeps the same bytes-SHA-256 idempotency as the
        primary, whose own disposition is preserved because the extras are additive.
        """
        if not capture.extra_paths:
            return original
        asset_paths = list(original.asset_paths)
        for index, extra in enumerate(capture.extra_paths, start=2):
            ext = extra.name.rsplit(".", 1)[-1].lower() if "." in extra.name else "png"
            result = self._save_local_asset_result_named(
                f"{cls.slug}-{index}", extra, ext
            )
            for rel in result.asset_paths:
                if rel not in asset_paths:
                    asset_paths.append(rel)
        return RawCaptureResult(
            raw_path=original.raw_path,
            disposition=original.disposition,
            asset_paths=asset_paths,
        )

    def _append_derived_assets(
        self,
        cls: Classification,
        original: RawCaptureResult,
        derived: _Analysed | None,
    ) -> RawCaptureResult:
        """Saves the derived enhancement assets and merges them after the original.

        Each artifact (issue #68) is routed through :meth:`_store_asset` under the
        classification slug. The original's disposition is preserved, because the
        derived assets are additive and never change whether the original was created,
        skipped or drifted.
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

    def _doc_disposition(self, rel: str, body: str) -> str:
        """Classifies a textual write against any existing stored digest.

        The compare must use the same derivation the writer stamps. Otherwise an
        unchanged body ending in a newline, which is the normal extractor case, never
        matches and is wrongly reported as drift.
        """
        new_sha = Vault.stored_body_sha256(body)
        existing_sha = self._existing_raw_sha(rel)
        if existing_sha is not None and existing_sha == new_sha:
            return "skipped_unchanged"
        return "updated_drift" if existing_sha is not None else "created"

    def _existing_raw_sha(self, rel: str) -> str | None:
        """Returns the stored ``sha256`` of an existing raw page."""
        if not self._vault.page_exists(rel):
            return None
        page = self._vault.read_page(rel)
        stored = page.frontmatter.get("sha256")
        return stored if isinstance(stored, str) else None
