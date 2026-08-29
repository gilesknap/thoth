"""The prompt strings for the analyse and Excalidraw-reconstruction calls."""

from __future__ import annotations

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
    "size, in pixels) and 'text' for the label that belongs INSIDE the shape. Put any "
    "text that sits inside a box in that box's 'text' field -- do NOT emit it as a "
    "separate free-standing 'text' element.\n"
    "- 'text': 'x','y' and 'text' -- ONLY for a label that is NOT inside a shape (a "
    "title or a free-floating annotation).\n"
    "- connectors ('arrow'/'line'): whenever the connector joins two shapes, give "
    "'from' and 'to' as the ids of those shapes (NOT explicit points) so it attaches "
    "to the boxes; only use explicit 'x','y','points' for a connector that joins no "
    "shape. A connector may also carry a 'text' label for the relationship (e.g. "
    "'depends on') -- it is placed on the line itself.\n"
    "Do NOT try to redraw pictorial/figurative drawings (a stick figure or sketched "
    "person, an icon, a drawn object) as raw lines. Represent each such drawing as a "
    "single 'rectangle' whose 'text' names what it depicts (e.g. a stick person "
    "becomes a box labelled 'User' or 'Me'), and connect it with arrows like any other "
    "box, so its relationships are kept without the messy line-art.\n"
    "Lay the coordinates out (roughly a 600-1000px canvas) to mirror the diagram's "
    "arrangement, with arrows reflecting the real connections and direction. Leave "
    "enough space between boxes that the connectors between them are clearly visible."
)
