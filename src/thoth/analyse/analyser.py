"""The injectable :class:`Analyser` driving the vision/document analyse calls."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from thoth.llm import LLM, LLMError, Message, extract_text, parse_json_block

from .excalidraw import _excalidraw_markdown
from .excalidraw_elements import _build_excalidraw_elements
from .prompts import _EXCALIDRAW_PROMPT, _IMAGE_PROMPT, _PDF_PROMPT
from .result import Analysis, _analysis_from_obj

# Bare image extension to the IANA media type the vision block expects
_IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_DEFAULT_IMAGE_MEDIA_TYPE: str = "image/png"

# Tokens for the analyse call, generous enough to hold OCR text and description. This is
# one heavier call per binary capture (issue #42), charged against the daily guard
_ANALYSE_MAX_TOKENS: int = 2048

# Tokens for the excalidraw reconstruction (issue #68). A scene of geometric elements as
# JSON is larger than the analyse summary, so this second call gets a roomier budget
_EXCALIDRAW_MAX_TOKENS: int = 4096


class AnalyseError(Exception):
    """Raised when the analyse call returns output that cannot be parsed.

    A transport failure, or the budget guard tripping, is deliberately not wrapped here.
    Those propagate unchanged so the ingest pass can defer the capture, since the raw is
    already durable, exactly as the classify and curate calls do.
    """


def image_media_type(ext: str) -> str:
    """Returns the IANA media type for a bare image extension.

    Args:
        ext: A bare lowercase extension such as ``png``.

    Returns:
        The matching media type, defaulting to ``image/png`` for an unrecognised
        extension, which is the common phone-screenshot case.
    """
    return _IMAGE_MEDIA_TYPES.get(ext.lower().lstrip("."), _DEFAULT_IMAGE_MEDIA_TYPE)


def _base64_source_block(
    block_type: str, media_type: str, data: bytes
) -> dict[str, Any]:
    """Builds a vision or document block carrying data as base64 (ADR 0006)."""
    return {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


class Analyser:
    """Vision and document analysis of binary captures behind an injected LLM.

    The LLM is injected and its client is a fake in tests, so the pass is unit-testable
    with no real model call. It also means the analyse call is charged against the same
    daily budget guard the LLM already enforces (issue #16), so a binary capture costs
    one heavier call that defers like the rest when the cap is reached.
    """

    def __init__(
        self,
        llm: LLM,
        *,
        model: str | None = None,
        diagram_model: str | None = None,
    ) -> None:
        """Stores the injected LLM and the optional per-call model overrides.

        Args:
            llm: The injectable Anthropic wrapper, which carries the budget guard.
            model: Model id for the main analyse call, which must be multimodal.
                None uses the configured default, and the owner may drop it to a
                cheaper model for an A/B.
            diagram_model: Model id for the second reconstruction call (issue #68),
                which needs spatial reasoning plus valid JSON and can warrant a
                stronger model. None falls back to the configured default.
        """
        self._llm = llm
        self._model = model
        self._diagram_model = diagram_model

    def analyse_image(self, image_bytes: bytes, *, ext: str) -> Analysis:
        """Analyses one image for OCR text, description and routing hints.

        A single-image wrapper over :meth:`analyse_images`. The bytes are base64-encoded
        transiently (ADR 0006), while the asset itself is still stored as a real binary
        by the caller.

        Args:
            image_bytes: The raw image bytes of the staged asset.
            ext: The bare image extension, selecting the media type.

        Returns:
            The parsed analysis.

        Raises:
            AnalyseError: if the output cannot be parsed into the expected shape.
            thoth.budget.BudgetExceededError: when the daily cap is reached, so the
                ingest pass defers.
        """
        return self.analyse_images([(image_bytes, ext)])

    def analyse_images(self, images: Sequence[tuple[bytes, str]]) -> Analysis:
        """Analyses one or more images in a single vision call (issues #84 and #124).

        A multi-image batch is one unit of intent curated as one page, so every image is
        sent as its own block in one call producing one shared summary, never N calls
        then a merge. Being one call, it counts as exactly one charge against the daily
        guard. The caller caps the count before calling here.

        Each image's bytes are base64-encoded transiently (ADR 0006), while the assets
        themselves are still stored as real binaries by the caller.

        Args:
            images: Bytes and extension pairs in upload order.

        Returns:
            The parsed analysis, one shared summary covering every supplied image.

        Raises:
            AnalyseError: if the output cannot be parsed into the expected shape.
            ValueError: if no images are supplied.
            thoth.budget.BudgetExceededError: when the daily cap is reached, so the
                ingest pass defers.
        """
        if not images:
            raise ValueError("analyse_images requires at least one image")
        blocks: list[dict[str, Any]] = [
            _base64_source_block("image", image_media_type(ext), image_bytes)
            for image_bytes, ext in images
        ]
        return self._run([*blocks, {"type": "text", "text": _IMAGE_PROMPT}])

    def analyse_pdf(self, pdf_bytes: bytes) -> Analysis:
        """Analyses a PDF for extracted text, summary and routing hints.

        The bytes are base64-encoded transiently into a document block the model reads
        natively (ADR 0006), while the PDF itself is still stored as a real binary by
        the caller.

        Args:
            pdf_bytes: The raw PDF bytes of the staged asset.

        Returns:
            The parsed analysis.

        Raises:
            AnalyseError: if the output cannot be parsed into the expected shape.
            thoth.budget.BudgetExceededError: when the daily cap is reached, so the
                ingest pass defers.
        """
        block = _base64_source_block("document", "application/pdf", pdf_bytes)
        return self._run([block, {"type": "text", "text": _PDF_PROMPT}])

    def _run(self, content: list[dict[str, Any]]) -> Analysis:
        """Sends one analyse turn and parses the JSON result.

        A transport failure or a budget trip is not caught here and propagates, so the
        ingest pass can defer the capture. Only an unparseable result becomes an
        :class:`AnalyseError`.
        """
        message = Message(role="user", content=content)
        response = self._llm.complete(
            [message], max_tokens=_ANALYSE_MAX_TOKENS, model=self._model
        )
        text = extract_text(response)
        try:
            obj = parse_json_block(text)
        except LLMError as exc:
            raise AnalyseError(
                f"could not parse analysis from model output: {exc}"
            ) from exc
        return _analysis_from_obj(obj)

    def reconstruct_excalidraw(self, image_bytes: bytes, *, ext: str) -> str | None:
        """Reconstructs a hand-drawn diagram as an editable Excalidraw scene.

        A second, best-effort vision call (issue #68, ADR 0009) made only for a
        diagram-kind image. It asks the model to re-draw the sketch as an idealised
        scene and return only the element list, then assembles the file envelope
        deterministically in code, because the model is never trusted with the wrapper.

        The result is an extra asset saved alongside the original, which is always kept.

        Being a pure enhancement, this never raises and never defers. Any failure
        returns None and the capture proceeds with just the original image.

        Args:
            image_bytes: The raw image bytes, reused rather than re-read.
            ext: The bare image extension, selecting the media type.

        Returns:
            The full markdown scene, or None on any failure.
        """
        block = _base64_source_block("image", image_media_type(ext), image_bytes)
        message = Message(
            role="user",
            content=[block, {"type": "text", "text": _EXCALIDRAW_PROMPT}],
        )
        try:
            response = self._llm.complete(
                [message],
                max_tokens=_EXCALIDRAW_MAX_TOKENS,
                model=self._diagram_model,
            )
            obj = parse_json_block(extract_text(response))
        except Exception:  # noqa: BLE001 -- best-effort enhancement, never propagate
            return None
        raw = obj.get("elements")
        if not isinstance(raw, list):
            return None
        specs = [element for element in raw if isinstance(element, dict)]
        if not specs:
            return None
        elements, text_elements = _build_excalidraw_elements(specs)
        if not elements:
            return None
        return _excalidraw_markdown(elements, text_elements)
