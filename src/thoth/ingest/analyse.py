"""Pass 0c: vision/PDF content analysis of a binary capture (issue #42)."""

from __future__ import annotations

from pathlib import Path

from thoth.analyse import AnalyseError, Analysis
from thoth.budget import BudgetExceededError
from thoth.extract import ExtractError, FetchedBinary
from thoth.images import downscale_if_oversized

from ._shared import (
    Capture,
    CaptureKind,
    IngestError,
    LLMUnavailableError,
    _Analysed,
    _IngestorBase,
    _require,
    logger,
)

# The binary kinds whose bytes the analyse pass OCRs / extracts to enrich the body and
# route by content (issue #42). Text/URL/audio already carry extracted text, so they are
# never analysed -- their existing paths are unchanged.
_ANALYSE_KINDS: frozenset[CaptureKind] = frozenset({CaptureKind.IMAGE, CaptureKind.PDF})


class _AnalysePass(_IngestorBase):
    """The analyse pass: OCR/vision/PDF enrichment that feeds classify and curate."""

    # ---- pass 0c: analyse (vision/PDF content extraction, issue #42) -------------

    def analyse(self, capture: Capture) -> _Analysed:
        """OCR/vision/PDF-analyse a binary capture so it is routed + curated by content.

        For an image or PDF capture the bytes are sent to a multimodal model (a vision
        ``image`` block or a ``document`` block) and the returned OCR/extracted text,
        description, and routing hints feed :meth:`classify` (so a whiteboard photo is
        routed to ``notes/`` by its content, not the ``memories/`` default) and
        :meth:`curate` (so the page body holds the real meaning). The asset is still
        saved as a real binary and embedded with ``![[...]]`` -- analysis only enriches
        and routes (ADR 0006).

        A **multi-image batch** (issue #84) is one unit of intent curated as one page,
        so EVERY image -- the primary plus the extras on :attr:`Capture.extra_paths` --
        is sent as a block in a **single** vision call producing one shared summary/tags
        (issue #124), not just the first image. Because it is one call it counts as
        exactly ONE charge against the daily budget guard; a safety cap
        (``THOTH_MAX_ANALYSE_IMAGES``) bounds the payload (extras beyond the cap are
        logged-and-skipped from the analyse call but still saved + embedded). A PDF
        is always single-file and stays on its own document/extraction path -- it is
        never bundled with image blocks. Non-binary kinds (text/URL/audio already carry
        extracted text) return ``None`` and their paths are unchanged.

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
            # A multi-image batch (issue #84) is one unit of intent curated as one page,
            # so EVERY image must reach the vision model -- not just the primary -- in
            # ONE call producing one shared summary/tags (issue #124). Read the extras
            # (already-downloaded local paths; a batch is never a URL fetch), capped,
            # reusing the primary bytes already in hand. A PDF never carries extras.
            extra_images = (
                self._extra_analyse_images(capture) if kind is CaptureKind.IMAGE else []
            )
        except (ExtractError, OSError) as exc:
            raise IngestError(f"analyse failed reading binary: {exc}") from exc
        try:
            if kind is CaptureKind.PDF:
                analysis: Analysis | None = self._analyser.analyse_pdf(image_bytes)
            else:
                # One vision call with N image blocks (primary first, then the capped
                # extras) -> one Analysis, one charge against the daily budget guard.
                analysis = self._analyser.analyse_images(
                    [(image_bytes, ext), *extra_images]
                )
        except AnalyseError:
            # An unparseable analysis must not lose the capture: file the binary blind
            # (the prior behaviour) rather than abort. The fetched binary is still
            # threaded forward so capture_raw reuses (and cleans up) it.
            logger.debug("analyse: unparseable result; filing binary blind")
            analysis = None
        except BudgetExceededError as exc:
            # The capture defers, so capture_raw will not consume the fetched binary --
            # clean it up here rather than leak it.
            _cleanup_fetched(fetched)
            logger.debug("analyse defer: budget cap reached (transient): %s", exc)
            raise LLMUnavailableError(f"analyse deferred (budget cap): {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - classify a client failure (raw durable)
            logger.debug(
                "analyse failure: status=%s permanent=%s",
                getattr(exc, "status_code", None),
                _is_permanent_vision_rejection(exc),
            )
            if _is_permanent_vision_rejection(exc):
                # A PERMANENT vision/document 400 (too-large / unsupported payload) is
                # NOT a deferrable outage (issue #70): the bytes never change between
                # attempts, so deferring would re-send the same rejected payload to a
                # held inbox file forever, burning one budget-guarded call each sweep.
                # File the binary blind (like an unparseable analysis) so the capture
                # still lands -- it just goes un-enriched, never into an infinite hold.
                logger.warning(
                    "analyse permanently rejected (vision 400); filing blind: %s", exc
                )
                analysis = None
            else:
                # A transient transport/availability failure DOES defer (raw durable).
                _cleanup_fetched(fetched)
                raise LLMUnavailableError(f"analyse LLM call failed: {exc}") from exc
        image_count = 1 if kind is CaptureKind.PDF else 1 + len(extra_images)
        logger.debug(
            "analyse done: kind=%s images=%d bytes_sent=%d text_len=%d "
            "suggested_type=%s model=%s",
            kind.value,
            image_count,
            len(image_bytes),
            len(analysis.text) if analysis is not None else 0,
            analysis.suggested_type if analysis is not None else None,
            self._config.analyse_model or self._config.anthropic_model,
        )
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

        An over-threshold **image** is downscaled here (issue #108): the reduced bytes
        are returned for the vision call *and* the staged source file is rewritten in
        place, so the same reduced bytes are what :meth:`capture_raw` later hashes and
        commits to ``raw/assets/`` (a single resize covers both storage and analysis).
        PDFs are not resized (downscaling a PDF is out of scope).
        """
        if capture.path is not None:
            name = capture.filename or capture.path.name
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            data = self._maybe_downscale(
                capture.path, capture.path.read_bytes(), ext, kind
            )
            return data, ext, None
        fetched = self._extractor.fetch_binary(_require(capture.url, "url"))
        data = self._maybe_downscale(
            fetched.tmp_path, fetched.tmp_path.read_bytes(), fetched.suggested_ext, kind
        )
        return data, fetched.suggested_ext, fetched

    def _extra_analyse_images(self, capture: Capture) -> list[tuple[bytes, str]]:
        """Read the extra images of a multi-image batch for one analyse call (#84/#124).

        A multi-image Slack batch carries its non-primary images on
        :attr:`Capture.extra_paths` (all local, already-downloaded paths -- a batch is
        never a URL fetch). Every image must reach the vision model so the one shared
        summary/tags genuinely cover the WHOLE batch, not just the primary (issue #84,
        criterion 3). They are sent as extra blocks in the SAME analyse call as the
        primary, so the batch still costs exactly ONE budget-guarded call.

        A safety cap (``THOTH_MAX_ANALYSE_IMAGES``, default 6) bounds the payload: the
        primary already consumes one slot, so at most ``cap - 1`` extras are read;
        any beyond that are logged-and-skipped from the analyse call (they are still
        saved and embedded by :meth:`_append_extra_images`). A non-positive cap disables
        the limit (analyse every image). Each extra is downscaled in place exactly like
        the primary (issue #108), so the bytes analysed match the bytes later stored.
        """
        if not capture.extra_paths:
            return []
        cap = self._config.max_analyse_images
        extras = list(capture.extra_paths)
        if cap > 0:
            # The primary already occupies one slot of the per-call budget.
            allowed = max(cap - 1, 0)
            if len(extras) > allowed:
                logger.debug(
                    "analyse cap: %d image(s) skipped from the vision call "
                    "(cap=%d, batch=%d incl. primary); still saved + embedded",
                    len(extras) - allowed,
                    cap,
                    len(extras) + 1,
                )
                extras = extras[:allowed]
        blocks: list[tuple[bytes, str]] = []
        for path in extras:
            ext = path.name.rsplit(".", 1)[-1].lower() if "." in path.name else "png"
            data = self._maybe_downscale(
                path, path.read_bytes(), ext, CaptureKind.IMAGE
            )
            blocks.append((data, ext))
        return blocks

    def _maybe_downscale(
        self, staged: Path, data: bytes, ext: str, kind: CaptureKind
    ) -> bytes:
        """Downscale an over-threshold image and rewrite its staged file (issue #108).

        Only a :class:`CaptureKind.IMAGE` is resized (a PDF is left untouched). When the
        reduced bytes differ from the input the staged source file (the Slack/MCP
        download or the fetched tmp) is rewritten so the *same* reduced bytes flow on to
        :meth:`capture_raw` -- the asset committed to ``raw/assets/`` is the downscaled
        one and the bytes-SHA-256 idempotency keys on the reduced content. Resize is
        best-effort (see :func:`thoth.images.downscale_if_oversized`): a missing Pillow
        or an undecodable image returns the original bytes and writes nothing.
        """
        # TODO(#108): the PDF analogue (re-rendering an over-limit PDF below Anthropic's
        # 32 MB / 100-page document limit) is out of scope here -- only images resize.
        # A permanent over-limit PDF 400 is still handled (it files blind, not defers --
        # see _is_permanent_vision_rejection); shrinking the PDF itself is future work.
        if kind is not CaptureKind.IMAGE:
            return data
        reduced = downscale_if_oversized(
            data, ext=ext, threshold_bytes=self._config.image_resize_threshold_bytes
        )
        if reduced is not data and len(reduced) != len(data):
            staged.write_bytes(reduced)
            logger.debug(
                "downscale fired (%s): %d -> %d bytes",
                ext or "?",
                len(data),
                len(reduced),
            )
        else:
            logger.debug(
                "downscale: no resize for %s (%d bytes, threshold=%d)",
                ext or "?",
                len(data),
                self._config.image_resize_threshold_bytes,
            )
        return reduced


def _is_permanent_vision_rejection(exc: Exception) -> bool:
    """Is ``exc`` a PERMANENT vision/document rejection (not a transient outage)? (#70)

    Anthropic's vision/document API rejects an over-limit or unsupported payload (an
    image over the 5 MB / pixel limit, a PDF over the 32 MB / 100-page document limit)
    with a permanent HTTP ``400`` ``invalid_request_error`` -- or, when the request
    body itself exceeds the size limit, a ``413`` ``RequestTooLargeError`` -- the
    *same* bytes will be rejected on every retry, so treating it as a deferrable outage
    holds the raw in ``inbox/`` and re-sends the identical payload forever, burning a
    budget-guarded call each sweep. A ``400`` / ``413`` / ``422`` is therefore
    classified as permanent; every other status (``429`` rate-limit, ``5xx`` outage)
    and any non-HTTP transport error stays transient and defers.

    Classified by duck-typing the SDK exception's ``status_code`` (the ``anthropic``
    ``APIStatusError`` surface) so :mod:`thoth.ingest` never imports the runtime-only
    ``anthropic`` package; an exception with no ``status_code`` is treated as transient.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in (400, 413, 422)


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
