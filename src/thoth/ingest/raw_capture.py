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
        """Extract and persist the inbound item durably *before* any LLM call.

        Writes a holding page under ``inbox/<sha12>.md`` whose body is the extracted
        text (a URL article's markdown, plain text, or an audio transcript) -- or, for a
        binary capture (image/PDF, no text yet), a short provenance stub naming the
        source so a later sweep can re-fetch and curate it. The slug is derived from the
        body SHA-256, so re-persisting identical content lands on the same path and is
        idempotent (``skipped_unchanged``). This is the *capture-never-lost* guarantee:
        the text is on disk and committable before classify/curate run.

        The intended curation mode (``--as-is`` low-touch vs the default re-curate) and
        the original ``filename`` are stamped into the hold frontmatter (issue #95, task
        E) so a later inbox sweep (:mod:`thoth.inbox_drain`) honours the ORIGINAL intent
        rather than guessing: a hold deferred under ``--as-is`` is re-filed as-is, a
        normal one is re-curated.

        The extraction itself (the only network step) happens here, so an
        :class:`thoth.extract.ExtractError` still aborts the ingest loudly (nothing is
        lost -- there was nothing to persist). The extracted text is returned on the
        :class:`_Holding` so the later :meth:`capture_raw` reuses it without a second
        fetch.

        Args:
            capture: The inbound item.
            as_is: Whether this capture was requested in low-touch ``--as-is`` mode, so
                the hold records ``mode: as-is`` and a later sweep re-files it low-touch
                (default ``False`` records ``mode: curate``).

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
            if kind is CaptureKind.PDF:
                return self._capture_pdf(capture, cls, fetched=fetched)
            pre = (
                prefetched
                if prefetched is not None
                else self._extract_text(capture, kind)
            )
            assert pre is not None  # URL/AUDIO/TEXT kinds always carry a text body
            subdir = "transcripts" if kind is CaptureKind.AUDIO else "articles"
            return self._write_raw_doc(subdir, cls, pre.body, pre.source_url)
        except ExtractError as exc:
            raise IngestError(f"capture failed during extraction: {exc}") from exc
        except VaultError as exc:
            raise IngestError(f"capture failed during vault write: {exc}") from exc

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

    def _write_inbox_holding(
        self,
        body: str,
        source: str,
        *,
        mode: str = HOLD_MODE_CURATE,
        filename: str | None = None,
    ) -> RawCaptureResult:
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

        The intended curation ``mode`` (``curate``/``as-is``) and the original
        ``filename`` are stamped into the frontmatter (issue #95, task E) so a later
        inbox sweep honours the original intent; ``filename`` is omitted when the
        capture had none (Slack/MCP text), keeping the frontmatter minimal.

        Args:
            body: The extracted inbound text (or a binary provenance stub) to hold.
            source: The capture's frontmatter ``source`` value.
            mode: The intended curation mode to stamp (``curate``/``as-is``).
            filename: The original upload name to stamp, or ``None`` to omit it.

        Returns:
            A :class:`RawCaptureResult` naming the held page and its disposition.
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
            "tags": ["inbox"],
            # Stamp the body digest so re-persist is idempotent (mirrors write_raw).
            "sha256": Vault.stored_body_sha256(body),
            # Stamp the intended curation mode (issue #95, task E) so the inbox sweep
            # re-files this hold with the ORIGINAL intent rather than guessing.
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
        """Save a multi-image batch's extra images as assets under the same slug (#84).

        A Slack message that carried several images at once is ONE capture, so every
        extra image rides on :attr:`Capture.extra_paths` and is saved next to the
        primary under a numbered slug (``<slug>-2.png``, ``<slug>-3.png``, ...), in
        upload order, and merged into the returned ``asset_paths`` after the primary so
        :meth:`_append_embeds` embeds them all in the one curated page. Each goes
        through :meth:`_store_asset`, so it keeps the same bytes-SHA-256
        idempotency/drift behaviour as the primary. The primary's own disposition is
        preserved (the extras are additive). An empty ``extra_paths`` (the single-file
        case) returns the original unchanged.
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

    def _doc_disposition(self, rel: str, body: str) -> str:
        """Classify a textual raw/holding write against any existing stored digest.

        The writer stamps the parse-stable redacted digest
        (:meth:`Vault.stored_body_sha256`), so the idempotency compare MUST use the
        same derivation -- otherwise an unchanged body ending in a newline (the normal
        extractor case) never matches and is wrongly re-reported as drift. Returns
        ``'skipped_unchanged'`` for a byte-identical existing page, ``'updated_drift'``
        for a changed one, and ``'created'`` for a brand-new path.
        """
        new_sha = Vault.stored_body_sha256(body)
        existing_sha = self._existing_raw_sha(rel)
        if existing_sha is not None and existing_sha == new_sha:
            return "skipped_unchanged"
        return "updated_drift" if existing_sha is not None else "created"

    def _existing_raw_sha(self, rel: str) -> str | None:
        """Return the stored ``sha256`` of an existing raw page, or ``None``."""
        if not self._vault.page_exists(rel):
            return None
        page = self._vault.read_page(rel)
        stored = page.frontmatter.get("sha256")
        return stored if isinstance(stored, str) else None
