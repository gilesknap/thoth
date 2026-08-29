"""The injectable :class:`Analyser` behind the vision and document analyse calls."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from thoth.llm import LLM, LLMError, Message, extract_text, parse_json_block

from .excalidraw import _excalidraw_markdown
from .excalidraw_elements import _build_excalidraw_elements
from .prompts import _EXCALIDRAW_PROMPT, _IMAGE_PROMPT, _PDF_PROMPT
from .result import Analysis, _analysis_from_obj

# Bare image extension -> the IANA media type the Anthropic vision block expects.
_IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_DEFAULT_IMAGE_MEDIA_TYPE: str = "image/png"

# Tokens for the analyse call. The limit is generous enough to hold the OCR text and the
# description. This is one heavier call per binary capture (issue #42), charged against
# the daily guard like any other Anthropic call (issue #16).
_ANALYSE_MAX_TOKENS: int = 2048

# Tokens for the Excalidraw reconstruction call (issue #68). A scene of geometric
# elements as JSON is larger than the analyse summary, so this best-effort second call
# gets a roomier budget. The same daily guard charges it.
_EXCALIDRAW_MAX_TOKENS: int = 4096


class AnalyseError(Exception):
    """Raised when the analyse call returns output that cannot be parsed.

    A *transport or availability* failure is deliberately **not** wrapped here. A
    raising client and a tripped budget guard both propagate unchanged, so the ingest
    pass can treat them as a deferral, exactly as it does for the classify and curate
    calls. The raw capture is already durable.
    """


def image_media_type(ext: str) -> str:
    """Return the IANA media type for a bare image extension, with no dot.

    Args:
        ext: A bare lowercase extension such as ``"png"`` or ``"jpg"``.

    Returns:
        The matching ``image/*`` media type. An unrecognised extension defaults to
        ``image/png``, which is the common phone-screenshot case.
    """
    return _IMAGE_MEDIA_TYPES.get(ext.lower().lstrip("."), _DEFAULT_IMAGE_MEDIA_TYPE)


def _base64_source_block(
    block_type: str, media_type: str, data: bytes
) -> dict[str, Any]:
    """Return a vision or document block carrying ``data`` as transient base64.

    ADR 0006 records why the base64 encoding is allowed here.
    """
    return {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


class Analyser:
    """Vision and document analysis of binary captures behind an injected :class:`LLM`.

    The caller injects the :class:`~thoth.llm.LLM`, and its client is a fake in tests,
    so the pass is unit-testable with no real model call. Crucially, the *same* daily
    budget guard that the LLM already enforces charges the analyse call (issue #16). A
    binary capture therefore costs one heavier vision or document call, which defers
    like every other call when the cap is reached.
    """

    def __init__(
        self,
        llm: LLM,
        *,
        model: str | None = None,
        diagram_model: str | None = None,
    ) -> None:
        """Store the injected LLM and the optional per-call model overrides.

        Args:
            llm: The injectable Anthropic wrapper, which carries the budget guard.
            model: An optional model id that overrides ``config.anthropic_model`` for
                the main folded analyse, kind and transcription call. The model must be
                multimodal. ``None`` uses the configured default, and the Sonnet models
                are multimodal, so the default works. The owner may drop this to a
                cheaper Haiku for a document A/B test.
            diagram_model: An optional model id for the second
                :meth:`reconstruct_excalidraw` vision call (issue #68). That call needs
                spatial reasoning plus valid JSON, so it can warrant a stronger model
                than the main pass. ``None`` falls back to ``config.anthropic_model``
                through the LLM.
        """
        self._llm = llm
        self._model = model
        self._diagram_model = diagram_model

    def analyse_image(self, image_bytes: bytes, *, ext: str) -> Analysis:
        """Analyse one image for OCR text, a description and routing hints.

        This method is a single-image convenience wrapper over :meth:`analyse_images`.
        It base64-encodes the bytes **transiently** into a vision ``image`` content
        block (ADR 0006), and the caller still stores the asset itself as a real binary.
        The call goes through :meth:`thoth.llm.LLM.complete`, so the daily budget guard
        charges it.

        Args:
            image_bytes: The raw image bytes of the staged asset.
            ext: The bare image extension, which selects the media type.

        Returns:
            The parsed :class:`Analysis`.

        Raises:
            AnalyseError: When the model output cannot be parsed into the expected
                shape.
            thoth.budget.BudgetExceededError: When the daily cap is reached. The error
                propagates so that the ingest pass defers.
        """
        return self.analyse_images([(image_bytes, ext)])

    def analyse_images(self, images: Sequence[tuple[bytes, str]]) -> Analysis:
        """Analyse one or more images in a SINGLE vision call (issues #84 and #124).

        A multi-image Slack batch is one unit of intent, curated as one page. The method
        therefore sends every image as its own ``image`` block in **one** call, which
        produces one shared summary and one shared tag set. It never makes N calls and
        merges the results. Because it is one :meth:`thoth.llm.LLM.complete` call, it
        counts as exactly ONE charge against the daily budget guard, the same as a
        single-image analyse. The caller, :meth:`thoth.ingest.Ingestor.analyse`, caps
        the image count with ``THOTH_MAX_ANALYSE_IMAGES`` before it calls here.

        The method base64-encodes each image's bytes **transiently** (ADR 0006). The
        caller still stores the assets themselves as real binaries.

        Args:
            images: One or more ``(image_bytes, ext)`` pairs in upload order. Each
                ``ext`` is the bare image extension, which selects the media type.

        Returns:
            The parsed :class:`Analysis`. One shared summary, description and tag set
            covers all the supplied images.

        Raises:
            AnalyseError: When the model output cannot be parsed into the expected
                shape.
            ValueError: When ``images`` is empty.
            thoth.budget.BudgetExceededError: When the daily cap is reached. The error
                propagates so that the ingest pass defers.
        """
        if not images:
            raise ValueError("analyse_images requires at least one image")
        blocks: list[dict[str, Any]] = [
            _base64_source_block("image", image_media_type(ext), image_bytes)
            for image_bytes, ext in images
        ]
        return self._run([*blocks, {"type": "text", "text": _IMAGE_PROMPT}])

    def analyse_pdf(self, pdf_bytes: bytes) -> Analysis:
        """Analyse a PDF for extracted text, a summary and routing hints.

        The method base64-encodes the bytes **transiently** into a ``document`` content
        block (ADR 0006), which Claude reads natively. The caller still stores the PDF
        itself as a real binary. The daily budget guard charges the call through the
        LLM.

        Args:
            pdf_bytes: The raw PDF bytes of the staged asset.

        Returns:
            The parsed :class:`Analysis`.

        Raises:
            AnalyseError: When the model output cannot be parsed into the expected
                shape.
            thoth.budget.BudgetExceededError: When the daily cap is reached. The error
                propagates so that the ingest pass defers.
        """
        block = _base64_source_block("document", "application/pdf", pdf_bytes)
        return self._run([block, {"type": "text", "text": _PDF_PROMPT}])

    def _run(self, content: list[dict[str, Any]]) -> Analysis:
        """Send one analyse turn and parse the JSON result into an :class:`Analysis`.

        A client or transport failure is **not** caught here, and neither is a budget
        trip. Both propagate, so the ingest pass can defer the capture. Only an
        unparseable result becomes an :class:`AnalyseError`.
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
        """Reconstruct a hand-drawn diagram as an editable Excalidraw markdown scene.

        This is a **second, best-effort** vision call (issue #68 and ADR 0009), made
        only for a ``diagram``-kind image. It asks the model to re-draw the whiteboard
        or sketch as an *idealised* Excalidraw scene and to return only the element
        list. It then assembles the ``.excalidraw.md`` envelope **deterministically in
        code**, because the model is never trusted with the file wrapper. The result is
        an additional asset saved alongside the original, and the original is always
        kept.

        Excalidraw reconstruction is a pure enhancement, so this method **never raises
        and never defers**. Any failure returns ``None`` and the capture proceeds with
        the original image alone. An unparseable reply, an empty element list, the
        budget cap and a transport error are all such failures. The model id is the
        injected ``diagram_model``, and ``None`` falls back to
        ``config.anthropic_model`` through the LLM.

        Args:
            image_bytes: The raw image bytes of the staged asset. The method reuses them
                rather than re-reading the file.
            ext: The bare image extension, which selects the vision media type.

        Returns:
            The full ``.excalidraw.md`` markdown string on success, or ``None`` on any
            failure, which degrades gracefully.

        Raises:
            Nothing: the method catches every failure mode and turns it into ``None``.
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
        except Exception:  # noqa: BLE001 - a best-effort enhancement never propagates
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
