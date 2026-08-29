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

# Bare image extension to the IANA media type the Anthropic vision block expects.
_IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_DEFAULT_IMAGE_MEDIA_TYPE: str = "image/png"

# Tokens for the analyse call, generous enough to hold OCR text and a description. It
# is one heavier call per binary capture (issue #42), charged against the daily guard
# like any other Anthropic call (issue #16).
_ANALYSE_MAX_TOKENS: int = 2048

# Tokens for the Excalidraw reconstruction call (issue #68). A scene of geometric
# elements as JSON is larger than the analyse summary, so this best-effort second call
# gets a roomier budget. The same daily guard charges it.
_EXCALIDRAW_MAX_TOKENS: int = 4096


class AnalyseError(Exception):
    """Raised when the analyse call returns output that cannot be parsed.

    A *transport or availability* failure is deliberately **not** wrapped here, whether
    the client raised or the budget guard tripped. It propagates unchanged, so the
    ingest pass treats it as a deferral with the raw capture already durable, exactly as
    the classify and curate calls do.
    """


def image_media_type(ext: str) -> str:
    """Return the IANA media type for a bare image extension, with no dot.

    Args:
        ext: A bare lowercase extension such as ``"png"`` or ``"jpg"``.

    Returns:
        The matching ``image/*`` media type. An unrecognised extension defaults to
        ``image/png``, the common phone-screenshot case.
    """
    return _IMAGE_MEDIA_TYPES.get(ext.lower().lstrip("."), _DEFAULT_IMAGE_MEDIA_TYPE)


def _base64_source_block(
    block_type: str, media_type: str, data: bytes
) -> dict[str, Any]:
    """A vision or document block carrying ``data`` as transient base64 (ADR 0006)."""
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

    The :class:`~thoth.llm.LLM` is injected, and a test fakes its client, so the pass is
    unit-testable with no real model call. Crucially, the *same* daily budget guard the
    LLM already enforces charges the analyse call (issue #16). A binary capture
    therefore costs one heavier vision call, which defers like the rest at the cap.
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
            model: Optional multimodal model id overriding ``config.anthropic_model``
                for the main folded analyse, kind and transcription call. ``None`` uses
                the configured default, and the Sonnet models are multimodal, so that
                default is fine. The owner may drop this to a cheaper Haiku for a
                document A/B.
            diagram_model: Optional model id for the second
                :meth:`reconstruct_excalidraw` vision call (issue #68). That call needs
                spatial reasoning and valid JSON, so it can warrant a stronger model
                than the main pass. ``None`` falls back to ``config.anthropic_model``
                through the LLM.
        """
        self._llm = llm
        self._model = model
        self._diagram_model = diagram_model

    def analyse_image(self, image_bytes: bytes, *, ext: str) -> Analysis:
        """Analyse one image for OCR text, a description and routing hints.

        A single-image convenience wrapper over :meth:`analyse_images`, with the same
        transient base64 encoding (ADR 0006), charging and failure modes.

        Args:
            image_bytes: The raw image bytes of the staged asset.
            ext: The bare image extension, which selects the media type.

        Returns:
            The parsed :class:`Analysis`.

        Raises:
            AnalyseError: when the model output does not parse into the expected shape.
            thoth.budget.BudgetExceededError: at the daily cap, propagated so the ingest
                pass defers.
        """
        return self.analyse_images([(image_bytes, ext)])

    def analyse_images(self, images: Sequence[tuple[bytes, str]]) -> Analysis:
        """Analyse one OR MORE images in a SINGLE vision call (issues #84 and #124).

        A multi-image Slack batch is one unit of intent curated as one page, so every
        image travels as its own ``image`` block in **one** call that produces one
        shared summary and tag set, never N calls and a merge. Being one
        :meth:`thoth.llm.LLM.complete` call, it counts as exactly ONE charge against the
        daily budget guard, the same as a single-image analyse. The caller,
        :meth:`thoth.ingest.Ingestor.analyse`, caps the count through
        ``THOTH_MAX_ANALYSE_IMAGES`` before calling here.

        Each image's bytes are base64-encoded **transiently** (ADR 0006), and the caller
        still stores the assets themselves as real binaries.

        Args:
            images: One or more ``(image_bytes, ext)`` pairs in upload order, where each
                ``ext`` is the bare image extension that selects the media type.

        Returns:
            The parsed :class:`Analysis`, one shared summary, description and tag set
            covering every supplied image.

        Raises:
            AnalyseError: when the model output does not parse into the expected shape.
            ValueError: when ``images`` is empty.
            thoth.budget.BudgetExceededError: at the daily cap, propagated so the ingest
                pass defers.
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

        The bytes are base64-encoded **transiently** into a ``document`` content block
        (ADR 0006) that Claude reads natively, and the caller still stores the PDF
        itself as a real binary. The daily budget guard charges it through the LLM.

        Args:
            pdf_bytes: The raw PDF bytes of the staged asset.

        Returns:
            The parsed :class:`Analysis`.

        Raises:
            AnalyseError: when the model output does not parse into the expected shape.
            thoth.budget.BudgetExceededError: at the daily cap, propagated so the ingest
                pass defers.
        """
        block = _base64_source_block("document", "application/pdf", pdf_bytes)
        return self._run([block, {"type": "text", "text": _PDF_PROMPT}])

    def _run(self, content: list[dict[str, Any]]) -> Analysis:
        """Send one analyse turn and parse the JSON result into an :class:`Analysis`.

        Only an unparseable result becomes an :class:`AnalyseError`, as that class
        documents.
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

        This is a **second, best-effort** vision call (issue #68, ADR 0009) made only
        for a ``diagram``-kind image. It asks the model to re-draw the whiteboard or
        sketch as an *idealised* Excalidraw scene and return only the element list, then
        assembles the ``.excalidraw.md`` envelope **deterministically in code**, never
        trusting the model with the file wrapper. The result is an additional asset
        saved alongside the original, and the original is always kept.

        Excalidraw reconstruction is a pure enhancement, so this method **never raises
        and never defers**. Any failure returns ``None`` and the capture proceeds with
        just the original image, whether an unparseable reply, an empty element list,
        the budget cap or a transport error. The model id is the injected
        ``diagram_model``, and ``None`` falls back to ``config.anthropic_model`` through
        the LLM.

        Args:
            image_bytes: The raw image bytes of the staged asset, reused rather than
                re-read.
            ext: The bare image extension, which selects the vision media type.

        Returns:
            The full ``.excalidraw.md`` markdown string on success, or ``None`` on any
            failure, a graceful degrade.

        Raises:
            Nothing: every failure mode is caught and turned into ``None``.
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
