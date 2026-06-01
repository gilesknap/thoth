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
import hashlib
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
        raw = obj.get("elements")
        if not isinstance(raw, list) or not raw:
            return None
        specs = [element for element in raw if isinstance(element, dict)]
        if not specs:
            return None
        elements, text_elements = _build_excalidraw_elements(specs)
        if not elements:
            return None
        return _excalidraw_markdown(elements, text_elements)


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
    "where each element is a SIMPLE node/connector spec (thoth expands it into a valid "
    "Excalidraw element, so do NOT include styling/ids you are unsure of). Fields:\n"
    "- 'id': a short unique string for the element (e.g. 'n1', 'n2', 'a1').\n"
    "- 'type': one of 'rectangle', 'ellipse', 'diamond', 'text', 'arrow', 'line'.\n"
    "- shapes ('rectangle'/'ellipse'/'diamond'): 'x','y','width','height' (top-left + "
    "size, in pixels) and 'text' for the label drawn inside the shape.\n"
    "- 'text': 'x','y' and 'text' (a free-standing label/title).\n"
    "- connectors ('arrow'/'line'): 'from' and 'to' set to the ids of the shapes they "
    "join (preferred); or give explicit 'x','y' and 'points'.\n"
    "Lay the coordinates out (roughly a 600-1000px canvas) to mirror the diagram's "
    "arrangement, with arrows reflecting the real connections and direction."
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


# The banner Obsidian-Excalidraw writes at the top of a parsed drawing; reproduced
# verbatim so a thoth-authored file is byte-shaped like a plugin-authored one.
_EXCALIDRAW_BANNER = (
    "==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ⚠== "
    "You can decompress Drawing data with the command palette: 'Decompress current "
    "Excalidraw file'. For more info check in plugin settings under 'Saving'"
)

# Excalidraw element defaults shared by every element (the renderer needs these present;
# Excalidraw's own restore() is tolerant, but emitting them in full keeps the scene OK
# across plugin versions). Per-type fields are layered on top in the builders below.
_EXCALIDRAW_TEXT_FONT_SIZE: int = 20
_EXCALIDRAW_LINE_HEIGHT: float = 1.25


def _excalidraw_markdown(
    elements: list[dict[str, Any]], text_elements: list[dict[str, str]]
) -> str:
    """Assemble the ``.excalidraw.md`` envelope around the built scene elements.

    thoth builds the entire Obsidian-Excalidraw file format deterministically (the model
    is trusted only for the node/connector *structure*, expanded by
    :func:`_build_excalidraw_elements`): the YAML frontmatter that marks the note as a
    parsed Excalidraw drawing, the plugin's switch-to-Excalidraw banner, a
    ``## Text Elements`` index (each label's text plus its ``^id`` anchor, for Obsidian
    search), and a ``%%``-commented ``# Excalidraw Data`` / ``## Drawing`` section that
    holds the full scene object in a fenced ``json`` block. The scene is stored
    **uncompressed** (plain ``json``, not ``compressed-json``): the plugin reads both,
    and plain JSON keeps the vault canonical-as-plain-text (a compressed blob does not).

    Args:
        elements: The fully-formed Excalidraw element dicts (from
            :func:`_build_excalidraw_elements`).
        text_elements: ``{"id", "text"}`` rows for the ``## Text Elements`` index.

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
    text_index = "".join(
        f"{row['text']} ^{row['id']}\n\n"
        for row in text_elements
        if row["text"].strip()
    )
    return (
        "---\n"
        "excalidraw-plugin: parsed\n"
        "tags: [excalidraw]\n"
        "---\n\n"
        f"{_EXCALIDRAW_BANNER}\n\n\n"
        "# Excalidraw Data\n\n"
        "## Text Elements\n"
        f"{text_index}"
        "%%\n"
        "## Drawing\n"
        "```json\n"
        f"{scene_json}\n"
        "```\n"
        "%%\n"
    )


def _build_excalidraw_elements(
    specs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Expand the model's simple node/connector specs into valid Excalidraw elements.

    The model returns only the *structure* (a shape's box + label, a connector's
    endpoints); this turns each spec into a fully-formed Excalidraw element with all the
    properties the renderer expects (issue #68 live-verify: the earlier minimal shapes
    with a ``label`` shorthand rendered as empty boxes). Specifically:

    * A ``rectangle``/``ellipse``/``diamond`` becomes a shape element plus -- when it
      carries a ``text`` label -- a separate ``text`` element centred over it (a plain
      overlaid label, not a bound-container, which keeps the scene simple + valid while
      still showing the text).
    * A ``text`` spec becomes a free-standing text element.
    * An ``arrow``/``line`` is routed between the centres of the shapes named by its
      ``from``/``to`` ids (or explicit ``x``/``y``/``points`` as a fallback), so links
      mirror the real diagram.

    Unknown/!malformed specs are skipped. Returns ``(elements, text_index_rows)`` where
    the rows feed the ``## Text Elements`` section.
    """
    centres: dict[str, tuple[float, float]] = {}
    elements: list[dict[str, Any]] = []
    text_rows: list[dict[str, str]] = []
    connectors: list[dict[str, Any]] = []

    for index, spec in enumerate(specs):
        etype = spec.get("type")
        eid = _excalidraw_id(spec, index)
        if etype in ("rectangle", "ellipse", "diamond"):
            x, y, w, h = _spec_geometry(spec, default_w=160.0, default_h=80.0)
            elements.append(_shape_element(eid, str(etype), x, y, w, h))
            centres[eid] = (x + w / 2, y + h / 2)
            label = _spec_label(spec)
            if label:
                label_id = f"{eid}-label"
                elements.append(_text_element(label_id, label, x, y, w, h))
                text_rows.append({"id": label_id, "text": label})
        elif etype == "text":
            label = _spec_label(spec)
            if not label:
                continue
            x, y, w, h = _spec_geometry(
                spec, default_w=_estimate_text_width(label), default_h=25.0
            )
            elements.append(_text_element(eid, label, x, y, w, h, centred=False))
            text_rows.append({"id": eid, "text": label})
        elif etype in ("arrow", "line"):
            connectors.append({"id": eid, "spec": spec, "type": etype})

    for connector in connectors:
        element = _connector_element(
            connector["id"], connector["type"], connector["spec"], centres
        )
        if element is not None:
            elements.append(element)
    return elements, text_rows


def _excalidraw_id(spec: dict[str, Any], index: int) -> str:
    """Return the spec's ``id`` (when a non-empty string) or a stable ``el{index}``."""
    raw = spec.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"el{index}"


def _spec_label(spec: dict[str, Any]) -> str:
    """Pull a label string from a spec's ``text`` (or a ``label``/``label.text``)."""
    for key in ("text", "label"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _spec_geometry(
    spec: dict[str, Any], *, default_w: float, default_h: float
) -> tuple[float, float, float, float]:
    """Read ``x``/``y``/``width``/``height`` from a spec with sane numeric fallbacks."""
    x = _as_float(spec.get("x"), 0.0)
    y = _as_float(spec.get("y"), 0.0)
    w = _as_float(spec.get("width"), default_w)
    h = _as_float(spec.get("height"), default_h)
    return x, y, max(w, 1.0), max(h, 1.0)


def _as_float(value: object, default: float) -> float:
    """Coerce a JSON number to ``float`` (the default for a non-number)."""
    return float(value) if isinstance(value, (int, float)) else default


def _estimate_text_width(text: str) -> float:
    """Estimate a text element's width from its length at the default font size."""
    return max(
        len(text) * _EXCALIDRAW_TEXT_FONT_SIZE * 0.6, float(_EXCALIDRAW_TEXT_FONT_SIZE)
    )


def _excalidraw_seed(eid: str, salt: str) -> int:
    """A deterministic 31-bit seed/nonce for an element (no RNG; stable output)."""
    digest = hashlib.sha256(f"{eid}:{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2_000_000_000


def _excalidraw_base(
    eid: str, etype: str, x: float, y: float, w: float, h: float
) -> dict[str, Any]:
    """The property set every Excalidraw element shares (styling + bookkeeping)."""
    return {
        "id": eid,
        "type": etype,
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(w, 2),
        "height": round(h, 2),
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": _excalidraw_seed(eid, "seed"),
        "version": 1,
        "versionNonce": _excalidraw_seed(eid, "nonce"),
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
    }


def _shape_element(
    eid: str, etype: str, x: float, y: float, w: float, h: float
) -> dict[str, Any]:
    """A closed-shape element (rectangle/ellipse/diamond) with rounded corners."""
    element = _excalidraw_base(eid, etype, x, y, w, h)
    if etype == "rectangle":
        element["roundness"] = {"type": 3}
    return element


def _text_element(
    eid: str,
    text: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    centred: bool = True,
) -> dict[str, Any]:
    """A text element, centred over a host shape's box (``centred``) or free-standing.

    A label is drawn as a plain overlaid text element rather than a bound-container
    child -- simpler and valid, and it makes the text visible (the empty-box failure was
    a ``label`` shorthand that Excalidraw does not render).
    """
    font = _EXCALIDRAW_TEXT_FONT_SIZE
    tw = _estimate_text_width(text)
    th = float(font) * _EXCALIDRAW_LINE_HEIGHT
    if centred:
        tx = x + (w - tw) / 2
        ty = y + (h - th) / 2
    else:
        tx, ty = x, y
    element = _excalidraw_base(eid, "text", tx, ty, tw, th)
    element.update(
        {
            "text": text,
            "rawText": text,
            "originalText": text,
            "fontSize": font,
            "fontFamily": 1,
            "textAlign": "center" if centred else "left",
            "verticalAlign": "middle",
            "baseline": round(font * 0.85, 2),
            "containerId": None,
            "lineHeight": _EXCALIDRAW_LINE_HEIGHT,
            "autoResize": True,
        }
    )
    return element


def _connector_element(
    eid: str, etype: str, spec: dict[str, Any], centres: dict[str, tuple[float, float]]
) -> dict[str, Any] | None:
    """Build an arrow/line, routed between the centres named by ``from``/``to``.

    Falls back to the spec's explicit ``x``/``y``/``points`` when the endpoint ids are
    not resolvable; returns ``None`` when neither a routable pair nor explicit points
    are available (so a dangling connector is dropped, not emitted malformed).
    """
    start = centres.get(_as_ref(spec.get("from")))
    end = centres.get(_as_ref(spec.get("to")))
    if start is not None and end is not None:
        x, y = start
        points = [[0.0, 0.0], [end[0] - start[0], end[1] - start[1]]]
    else:
        points = _as_points(spec.get("points"))
        if points is None:
            return None
        x = _as_float(spec.get("x"), 0.0)
        y = _as_float(spec.get("y"), 0.0)
    xs = [px for px, _ in points]
    ys = [py for _, py in points]
    element = _excalidraw_base(eid, etype, x, y, max(xs) - min(xs), max(ys) - min(ys))
    element.update(
        {
            "points": [[round(px, 2), round(py, 2)] for px, py in points],
            "lastCommittedPoint": None,
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": "arrow" if etype == "arrow" else None,
        }
    )
    return element


def _as_ref(value: object) -> str:
    """Return a connector endpoint reference id as a string (``""`` when absent)."""
    return value.strip() if isinstance(value, str) else ""


def _as_points(value: object) -> list[list[float]] | None:
    """Coerce a model ``points`` value to ``[[x, y], ...]`` or ``None`` if unusable."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    points: list[list[float]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append([_as_float(item[0], 0.0), _as_float(item[1], 0.0)])
    return points if len(points) >= 2 else None
