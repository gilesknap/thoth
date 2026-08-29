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

# The binary kinds whose bytes the analyse pass OCRs to enrich the body and route by
# content (issue #42). Text, URL and audio already carry extracted text, so they are
# never analysed and their paths are unchanged
_ANALYSE_KINDS: frozenset[CaptureKind] = frozenset({CaptureKind.IMAGE, CaptureKind.PDF})


class _AnalysePass(_IngestorBase):
    """The analyse pass: OCR/vision/PDF enrichment that feeds classify and curate."""

    # ---- pass 0c: analyse (vision/PDF content extraction, issue #42) -------------

    def analyse(self, capture: Capture) -> _Analysed:
        """Analyses a binary capture so it is routed and curated by its content.

        The bytes go to a multimodal model, and the returned text, description and
        routing hints feed classify and curate. So a whiteboard photo routes to
        ``notes/`` by its content rather than the default, and the page body holds the
        real meaning.

        The asset is still saved as a real binary and embedded, because analysis only
        enriches and routes (ADR 0006).

        A multi-image batch is one unit of intent curated as one page (issue #84), so
        every image is sent as a block in a single vision call producing one shared
        summary (issue #124). Being one call, it costs exactly one charge against the
        daily budget guard, and a safety cap bounds the payload. Extras beyond the cap
        are skipped from the call but still saved and embedded.

        A PDF is always single-file and stays on its own document path, never bundled
        with image blocks.

        A transport failure or a budget trip raises so the already-durable asset defers
        and is re-analysed on a later sweep, exactly like the classify deferral. An
        unparseable analysis is not fatal: the binary is filed without enrichment rather
        than aborting the capture.

        Args:
            capture: The inbound item.

        Returns:
            The analysis for a binary capture, None for a non-binary kind or an
            unparseable result, plus any fetched URL binary so :meth:`capture_raw`
            reuses the same bytes rather than fetching twice.

        Raises:
            LLMUnavailableError: if the call is unavailable or the budget cap is
                reached, which :meth:`ingest` treats as a deferral.
            IngestError: on a failure to read the binary bytes.
        """
        kind = self._capture_kind(capture)
        if kind not in _ANALYSE_KINDS:
            return _Analysed(analysis=None)
        try:
            image_bytes, ext, fetched = self._analyse_bytes(capture, kind)
            # A multi-image batch (issue #84) is one unit of intent curated as one page,
            # so every image must reach the model in one call producing one shared
            # summary (issue #124). The extras are already-downloaded local paths, since
            # a batch is never a URL fetch. A PDF never carries extras
            extra_images = (
                self._extra_analyse_images(capture) if kind is CaptureKind.IMAGE else []
            )
        except (ExtractError, OSError) as exc:
            raise IngestError(f"analyse failed reading binary: {exc}") from exc
        try:
            if kind is CaptureKind.PDF:
                analysis: Analysis | None = self._analyser.analyse_pdf(image_bytes)
            else:
                # One vision call with N image blocks, primary first, giving one
                # analysis and one charge against the daily budget guard
                analysis = self._analyser.analyse_images(
                    [(image_bytes, ext), *extra_images]
                )
        except AnalyseError:
            # An unparseable analysis must not lose the capture, so file the binary
            # blind rather than abort. The fetched binary is still threaded forward so
            # capture_raw reuses and cleans it up
            logger.debug("analyse: unparseable result; filing binary blind")
            analysis = None
        except BudgetExceededError as exc:
            # The capture defers, so capture_raw will not consume the fetched binary.
            # Clean it up here rather than leak it
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
                # A permanent 400 is not a deferrable outage (issue #70). The bytes
                # never change between attempts, so deferring would re-send the same
                # rejected payload forever, burning a guarded call each sweep. File the
                # binary blind so the capture still lands, un-enriched but not held
                logger.warning(
                    "analyse permanently rejected (vision 400); filing blind: %s", exc
                )
                analysis = None
            else:
                # A transient transport failure does defer, since the raw is durable
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
        # The primary analysis succeeded or filed blind, so the capture is already safe.
        # Now derive the enhancement artifacts (issue #68) from the reported image kind,
        # reusing the bytes already in hand. Each is purely additive, so any failure
        # leaves the original filed cleanly and never defers or loses the capture
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
        """Derives the per-kind enhancement artifact, best-effort (issue #68).

        For an image only, a diagram is reconstructed as an editable Excalidraw scene by
        a second vision call. A PDF gets no derivation.

        This is a pure enhancement saved alongside the kept original, so every failure
        mode is swallowed here. The primary capture is already durable and must never be
        deferred or lost by a best-effort artifact.
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
        """Returns the binary's bytes, bare extension, and any fetched binary.

        A server-resolvable path is read directly, which is the common upload case, and
        a URL binary is fetched exactly once. The fetched result is threaded forward so
        :meth:`capture_raw` reuses the staged bytes, with no second download and no
        leaked temp file.

        An over-threshold image is downscaled here (issue #108). The reduced bytes are
        returned for the vision call and the staged file is rewritten in place, so one
        resize covers both storage and analysis. PDFs are not resized.
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
        """Reads a batch's extra images for one analyse call (issues #84 and #124).

        A batch carries its non-primary images as local, already-downloaded paths, since
        a batch is never a URL fetch. Every image must reach the model so the one shared
        summary genuinely covers the whole batch rather than just the primary.

        They ride as extra blocks in the same call, so the batch still costs one guarded
        call.

        A safety cap bounds the payload. The primary already occupies one slot, so at
        most one fewer extra is read and the rest are skipped from the call, though
        :meth:`_append_extra_images` still saves and embeds them. A non-positive cap
        disables the limit.

        Each extra is downscaled in place exactly like the primary (issue #108), so the
        bytes analysed match the bytes later stored.
        """
        if not capture.extra_paths:
            return []
        cap = self._config.max_analyse_images
        extras = list(capture.extra_paths)
        if cap > 0:
            # The primary already occupies one slot of the per-call budget
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
        """Downscales an over-threshold image and rewrites its staged file (#108).

        Only an image is resized, and a PDF is left untouched. When the reduced bytes
        differ, the staged source file is rewritten so the same reduced bytes flow on to
        :meth:`capture_raw`.

        The asset committed to ``raw/assets/`` is then the downscaled one and the
        idempotency key is on the reduced content.

        Resize is best-effort. A missing Pillow or an undecodable image returns the
        original bytes and writes nothing.
        """
        # TODO(#108): the PDF analogue, re-rendering an over-limit PDF below the
        # document limit, is out of scope here and only images resize. A permanent
        # over-limit PDF 400 is still handled and files blind rather than deferring, but
        # shrinking the PDF itself is future work
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
    """Reports whether a failure is a permanent vision rejection (issue #70).

    An over-limit or unsupported payload is rejected with a permanent 400, or a 413 when
    the request body itself is too large. The same bytes are rejected on every retry, so
    treating it as a deferrable outage would hold the raw in ``inbox/`` and re-send the
    identical payload forever, burning a guarded call each sweep.

    A 400, 413 or 422 is therefore permanent. Every other status, including rate limits
    and outages, and any transport error stays transient and defers.

    Classification duck-types the SDK exception's status code, so :mod:`thoth.ingest`
    never imports the runtime-only ``anthropic`` package.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in (400, 413, 422)


def _cleanup_fetched(fetched: FetchedBinary | None) -> None:
    """Unlinks a staged temp file that was never consumed.

    On the happy path :meth:`Ingestor.capture_raw` reuses and cleans up the staged temp
    file. This guards the paths where it never runs, such as a deferral, so the temp
    file is removed rather than leaked. Best-effort, so a missing file is fine.
    """
    if fetched is None:
        return
    fetched.tmp_path.unlink(missing_ok=True)
