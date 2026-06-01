"""Content analysis of binary captures (images + PDFs) via a vision/document call.

This module is the **analyse seam** issue #42 adds to the capture pipeline. A binary
capture (an uploaded image or PDF) historically reached the classify/curate passes as a
single ``File: screenshot.png`` line -- the model never saw the file -- so every
attachment was filed blind into ``memories/`` with a boilerplate stub. The analyse pass
fixes that: it sends the *bytes* of the staged asset to a multimodal Claude model (an
image as a base64 ``image`` content block, a PDF as a base64 ``document`` block) and
returns the OCR'd / extracted text, a structured description/summary, and routing hints
(a suggested ``type`` plus named ``entities``/``concepts``) that drive both the classify
*routing* and the curate *body*.

Transient base64 vs SPEC section 6. SPEC section 6 forbids binary bytes ever travelling
*as base64* -- a **storage** rule: the vault never holds base64, and a byte-blob is
never the canonical form. Sending base64 to the vision API to *analyse* an image, while
the asset is still saved as a real binary file under ``raw/assets/`` and embedded with
``![[...]]``, is a deliberate amendment recorded in ADR 0006: the base64 is transient
(it lives only inside one request) and analysis-only (it enriches and routes; it is
never written or treated as the source of truth).

Cost + durability. The analyse call goes through the injected :class:`thoth.llm.LLM`, so
it is charged against the **same daily budget guard** as every other Anthropic call
(issue #16) and a cap-reached day raises :class:`thoth.budget.BudgetExceededError`
*before* the request -- which the ingest pass treats as a *deferral* (the raw asset is
already durable; a later sweep re-analyses it) rather than a lost capture, exactly like
the existing classify/curate deferral.

The :class:`Analyser` is injectable and the LLM client behind it is a fake in tests, so
the whole pass is unit-testable with **no real model call**: a test scripts the vision /
document JSON response (or injects a fake :class:`Analyser` directly).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thoth.llm import LLM, LLMError, Message, extract_text, parse_json_block

__all__ = [
    "Analyser",
    "AnalyseError",
    "Analysis",
    "image_media_type",
]

# Bare image extension -> the IANA media type the Anthropic vision block expects.
_IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_DEFAULT_IMAGE_MEDIA_TYPE: str = "image/png"

# Tokens for the analyse call: generous enough to hold OCR text + description, but this
# is one heavier call per binary capture (issue #42), charged like any other Anthropic
# call against the daily guard (issue #16).
_ANALYSE_MAX_TOKENS: int = 2048

# Tokens for the Excalidraw reconstruction call (issue #68): a scene of geometric
# elements as JSON is larger than the analyse summary, so this best-effort second call
# gets a roomier budget. It is charged against the same daily guard.
_EXCALIDRAW_MAX_TOKENS: int = 4096

# The coarse image kinds the folded analyse call may report (ADR 0009). Anything the
# model returns outside this set is normalised to "" (unknown), so the ingest pass never
# branches on an unexpected value.
_VALID_KINDS: frozenset[str] = frozenset({"diagram", "document", "photo", "screenshot"})


class AnalyseError(Exception):
    """Raised when the analyse call returns output that cannot be parsed.

    A *transport/availability* failure (the client raising, or the budget guard
    tripping) is deliberately **not** wrapped here: those propagate unchanged so the
    ingest pass can treat them as a deferral (raw already durable), like the
    classify/curate calls.
    """


def image_media_type(ext: str) -> str:
    """Return the IANA media type for a bare image extension (no dot).

    Args:
        ext: A bare lowercase extension such as ``"png"`` or ``"jpg"``.

    Returns:
        The matching ``image/*`` media type, defaulting to ``image/png`` for an
        unrecognised extension (the common phone-screenshot case).
    """
    return _IMAGE_MEDIA_TYPES.get(ext.lower().lstrip("."), _DEFAULT_IMAGE_MEDIA_TYPE)


@dataclass(frozen=True, slots=True)
class Analysis:
    """The structured result of analysing one binary capture.

    Attributes:
        text: The OCR'd / extracted text of the asset (an image's legible text, a PDF's
            body text). Empty when the asset carries no text.
        description: A structured natural-language description of the asset's content
            (what the image shows / what the document is about).
        summary: A short one-line summary suitable for a title / log subject.
        suggested_type: A routing hint -- one of the four content types
            (:data:`thoth.vault.TYPE_ENUMERATION`) -- so a whiteboard photo is routed to
            a knowledge folder rather than defaulting to ``memories/``. ``None`` when
            the model offered no usable hint.
        entities: Named entities the model found (feed the candidate fetch).
        concepts: Named concepts the model found (feed the candidate fetch).
        kind: The coarse image kind the model reported -- one of ``"diagram"``,
            ``"document"``, ``"photo"`` or ``"screenshot"``, ``""`` when unknown. This
            single vision call folds the kind detection in (ADR 0009) rather than paying
            a separate pre-call: the ingest pass branches on it to derive best-effort
            artifacts (an Excalidraw reconstruction of a hand-drawn ``diagram``, a
            cleaned scan of a ``document``).
    """

    text: str = ""
    description: str = ""
    summary: str = ""
    suggested_type: str | None = None
    entities: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    kind: str = ""

    def is_empty(self) -> bool:
        """Return ``True`` when the analysis carries no usable extracted content."""
        return not (self.text.strip() or self.description.strip())

    def body_markdown(self) -> str:
        """Render the analysis as a markdown block for the curated page body.

        The block holds the real extracted meaning -- the description followed by the
        verbatim OCR/extracted text under an ``Extracted text`` heading -- so the
        curated page is searchable on the asset's content, not a blind stub. Returns an
        empty string when there is nothing extracted (the caller then keeps its own
        body).
        """
        parts: list[str] = []
        if self.description.strip():
            parts.append(self.description.strip())
        if self.text.strip():
            parts.append("## Extracted text\n\n" + self.text.strip())
        return "\n\n".join(parts)


class Analyser:
    """Vision/document analysis of binary captures behind an injected :class:`LLM`.

    The :class:`~thoth.llm.LLM` is injected (its client is a fake in tests), so the pass
    is unit-testable with no real model call, and -- crucially -- the analyse call is
    charged against the *same* daily budget guard the LLM already enforces (issue #16),
    so a binary capture costs one heavier vision/document call that defers like the rest
    when the cap is reached.
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
            llm: The injectable Anthropic wrapper (carries the budget guard).
            model: Optional model id overriding ``config.anthropic_model`` for the main
                folded analyse/kind/transcription call (a multimodal model); ``None``
                uses the configured default (the Sonnet models are multimodal, so the
                default is fine). The owner may drop this to a cheaper Haiku for a
                document A/B.
            diagram_model: Optional model id for the second
                :meth:`reconstruct_excalidraw` vision call (issue #68). That call needs
                spatial reasoning plus valid JSON, so it can warrant a stronger model
                than the main pass; ``None`` falls back to ``config.anthropic_model``
                via the LLM.
        """
        self._llm = llm
        self._model = model
        self._diagram_model = diagram_model

    def analyse_image(self, image_bytes: bytes, *, ext: str) -> Analysis:
        """Analyse an image: OCR text + description + routing hints (vision block).

        The bytes are base64-encoded **transiently** into a vision ``image`` content
        block (ADR 0006); the asset itself is still stored as a real binary by the
        caller. The call goes through :meth:`thoth.llm.LLM.complete`, so it is charged
        against the daily budget guard.

        Args:
            image_bytes: The raw image bytes of the staged asset.
            ext: The bare image extension (selects the media type).

        Returns:
            The parsed :class:`Analysis`.

        Raises:
            AnalyseError: if the model output cannot be parsed into the expected shape.
            thoth.budget.BudgetExceededError: when the daily cap is reached (propagated
                so the ingest pass defers).
        """
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type(ext),
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        }
        return self._run([block, {"type": "text", "text": _IMAGE_PROMPT}])

    def analyse_pdf(self, pdf_bytes: bytes) -> Analysis:
        """Analyse a PDF: extracted text + summary + routing hints (document block).

        The bytes are base64-encoded **transiently** into a ``document`` content block
        (ADR 0006) that Claude reads natively; the PDF itself is still stored as a real
        binary by the caller. Charged against the daily budget guard via the LLM.

        Args:
            pdf_bytes: The raw PDF bytes of the staged asset.

        Returns:
            The parsed :class:`Analysis`.

        Raises:
            AnalyseError: if the model output cannot be parsed into the expected shape.
            thoth.budget.BudgetExceededError: when the daily cap is reached (propagated
                so the ingest pass defers).
        """
        block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
            },
        }
        return self._run([block, {"type": "text", "text": _PDF_PROMPT}])

    def _run(self, content: list[dict[str, Any]]) -> Analysis:
        """Send one analyse turn and parse the JSON result into an :class:`Analysis`.

        A client/transport failure (or a budget trip) is **not** caught here -- it
        propagates so the ingest pass can defer the capture; only an unparseable result
        becomes an :class:`AnalyseError`.
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

        This is a **second, best-effort** vision call (issue #68 / ADR 0009) made only
        for a ``diagram``-kind image: it asks the model to re-draw the whiteboard /
        sketch as an *idealised* Excalidraw scene and return only the element list, then
        assembles the ``.excalidraw.md`` envelope **deterministically in code** (the
        model is never trusted with the file wrapper). The result is an additional asset
        saved alongside the original -- the original is always kept.

        Because Excalidraw reconstruction is a pure enhancement, this method **never
        raises and never defers**: any failure (an unparseable reply, an empty element
        list, the budget cap, or a transport error) returns ``None`` and the capture
        proceeds with just the original image. The model id is the injected
        ``diagram_model`` (``None`` falls back to ``config.anthropic_model`` via the
        LLM).

        Args:
            image_bytes: The raw image bytes of the staged asset (reused, not re-read).
            ext: The bare image extension (selects the vision media type).

        Returns:
            The full ``.excalidraw.md`` markdown string on success, or ``None`` on any
            failure (graceful degrade).

        Raises:
            Nothing: every failure mode is caught and turned into ``None``.
        """
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type(ext),
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        }
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
        elements = obj.get("elements")
        if not isinstance(elements, list) or not elements:
            return None
        if not all(isinstance(element, dict) for element in elements):
            return None
        return _excalidraw_markdown(elements)


def analyse_image_path(analyser: Analyser, path: Path, *, ext: str) -> Analysis:
    """Read a staged image file and analyse its bytes (convenience for the ingestor)."""
    return analyser.analyse_image(path.read_bytes(), ext=ext)


def analyse_pdf_path(analyser: Analyser, path: Path) -> Analysis:
    """Read a staged PDF file and analyse its bytes (convenience for the ingestor)."""
    return analyser.analyse_pdf(path.read_bytes())


_RESULT_SHAPE = (
    "Return ONLY a single JSON object (no prose) of this exact shape:\n"
    "{\n"
    '  "text": "the legible/extracted text, verbatim (empty string if none)",\n'
    '  "description": "a structured description of the content",\n'
    '  "summary": "a short one-line summary",\n'
    '  "suggested_type": one of ["entity", "note", "memory", "action"],\n'
    '  "entities": ["named people/orgs/products/models"],\n'
    '  "concepts": ["named concepts/topics"],\n'
    '  "kind": one of ["diagram", "document", "screenshot", "photo"]\n'
    "}\n"
    "Kind: 'diagram' = a whiteboard photo OR a hand-drawn sketch / flowchart / mindmap "
    "/ box-and-arrow drawing; 'document' = a scan or photo of a printed or handwritten "
    "page; 'screenshot' = a UI / app capture; 'photo' = a real-world snapshot.\n"
    "Routing: choose 'note' for anything written/diagrammed (a whiteboard, a sketch, a "
    "screenshot of notes, a document); 'action' for a todo/receipt/invoice/ticket; "
    "'entity' for a photo that is primarily a person/product/device; 'memory' only for "
    "a personal snapshot with no extractable knowledge. Prefer a knowledge type when "
    "the asset carries legible content.\n"
    "Text: for a 'document', the 'text' MUST be a FAITHFUL STRUCTURED MARKDOWN "
    "transcription -- preserve headings as markdown headings, bullet/numbered lists as "
    "markdown lists, and tables as markdown tables -- not loose flattened OCR. For "
    "other kinds, transcribe every legible word verbatim."
)

_IMAGE_PROMPT = (
    "Analyse this image for a personal knowledge vault. OCR every legible word, "
    "describe what it shows, and suggest how to file it.\n\n" + _RESULT_SHAPE
)

_PDF_PROMPT = (
    "Analyse this PDF for a personal knowledge vault. Extract its text, summarise it, "
    "and suggest how to file it.\n\n" + _RESULT_SHAPE
)

# The Excalidraw reconstruction prompt (issue #68). The model returns ONLY the element
# list -- thoth assembles the file envelope deterministically (it is never trusted with
# the wrapper), so the prompt asks only for {"elements": [...]}.
_EXCALIDRAW_PROMPT = (
    "This image is a hand-drawn diagram (a whiteboard, sketch, flowchart, mindmap, or "
    "box-and-arrow drawing). Reconstruct it as an idealised, editable Excalidraw "
    "scene: clean up wobbly strokes into proper shapes and connectors while preserving "
    "the structure, labels, and connections.\n"
    "Return ONLY a single JSON object (no prose) of this exact shape:\n"
    '{"elements": [ ... ]}\n'
    "where each element is an Excalidraw element object. Use the Excalidraw element "
    "types 'rectangle', 'ellipse', 'diamond', 'arrow', 'line', 'text' and 'freedraw'. "
    "Every element MUST have at least 'type', 'x' and 'y'; add 'width' and 'height' "
    "for shapes, 'text' for a text element, 'points' for an arrow/line/freedraw, and a "
    "'label' where a shape carries text. Lay the elements out to mirror the diagram's "
    "spatial arrangement."
)


def _analysis_from_obj(obj: dict[str, Any]) -> Analysis:
    """Build an :class:`Analysis` from a parsed JSON object (missing keys tolerated)."""
    suggested = obj.get("suggested_type")
    return Analysis(
        text=_as_str(obj.get("text")),
        description=_as_str(obj.get("description")),
        summary=_as_str(obj.get("summary")),
        suggested_type=suggested if isinstance(suggested, str) and suggested else None,
        entities=_as_str_list(obj.get("entities")),
        concepts=_as_str_list(obj.get("concepts")),
        kind=_as_kind(obj.get("kind")),
    )


def _as_str(value: object) -> str:
    """Coerce a JSON value to a string (empty string for a non-string)."""
    return value if isinstance(value, str) else ""


def _as_str_list(value: object) -> list[str]:
    """Return ``value`` as a list of non-empty strings (empty list otherwise)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _as_kind(value: object) -> str:
    """Normalise a reported image kind to one of the four valid values or ``""``.

    Anything outside :data:`_VALID_KINDS` (a missing key, a typo, an unexpected label)
    collapses to ``""`` so the ingest pass never branches on a surprise value.
    """
    if isinstance(value, str) and value in _VALID_KINDS:
        return value
    return ""


def _excalidraw_markdown(elements: list[dict[str, Any]]) -> str:
    """Assemble the ``.excalidraw.md`` envelope around model-supplied scene elements.

    The model is trusted only for the ``elements`` list; thoth builds the rest of the
    Obsidian-Excalidraw file format deterministically: YAML frontmatter that marks the
    note as a parsed Excalidraw drawing, then a ``# Excalidraw Data`` section whose
    ``## Drawing`` subsection holds the full scene object inside a fenced ``json`` code
    block. The scene wraps the elements with the fixed Excalidraw scaffolding
    (``type``/``version``/``source``/``appState``/``files``).

    Args:
        elements: The non-empty list of Excalidraw element dicts the model returned.

    Returns:
        The complete ``.excalidraw.md`` markdown string.
    """
    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "thoth",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    scene_json = json.dumps(scene, indent=2)
    return (
        "---\n"
        "excalidraw-plugin: parsed\n"
        "tags: [excalidraw]\n"
        "---\n\n"
        "# Excalidraw Data\n\n"
        "## Drawing\n"
        "```json\n"
        f"{scene_json}\n"
        "```\n"
    )
