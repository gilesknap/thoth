"""Expansion of the model's simple node/connector specs into Excalidraw elements."""

from __future__ import annotations

import hashlib
import math
from typing import Any

# Element defaults shared by every element. Excalidraw's own restore() is tolerant, but
# emitting them in full keeps the scene valid across plugin versions. Per-type fields
# are layered on top by the builders below
_EXCALIDRAW_TEXT_FONT_SIZE: int = 20
_EXCALIDRAW_LINE_HEIGHT: float = 1.25
# Padding between a bound label and its container edge, matching excalidraw's own
# default, and the gap a bound arrow leaves at the shape edge so the arrowhead does not
# sit on the border
_EXCALIDRAW_TEXT_PADDING: float = 5.0
_EXCALIDRAW_BINDING_GAP: float = 8.0


def _build_excalidraw_elements(
    specs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Expands the model's node and connector specs into valid Excalidraw elements.

    The model returns only the structure, so each spec becomes a fully-formed element
    carrying every property the renderer expects. The issue #68 live-verify showed
    minimal shapes with a label shorthand rendering as empty boxes.

    * A ``rectangle``, ``ellipse`` or ``diamond`` becomes a shape element, and a
      ``text`` label on it becomes a bound text element: the label points at the shape
      and the shape references the label back, so the text is a property of the box that
      Excalidraw centres, wraps and moves with it rather than a loose overlay.
    * A ``text`` spec becomes a free-standing text element.
    * An ``arrow`` or ``line`` joining two shapes binds to them. Its endpoints snap to
      the point on each box's edge facing the other box, not the centre, with a small
      gap, and each shape references the connector back, so the arrow tracks the boxes
      and never plunges into their middles. A connector with explicit points and no
      resolvable shapes is emitted unbound as a fallback.
    * A connector's own ``text`` label binds to the connector, so Excalidraw places it
      at the line's midpoint over a masked background, near the line but never crossing
      it.

    Unknown or malformed specs are skipped. Returns the elements plus the rows that feed
    the ``## Text Elements`` section.
    """
    shapes: dict[str, dict[str, Any]] = {}
    geometry: dict[str, tuple[float, float, float, float]] = {}
    elements: list[dict[str, Any]] = []
    text_rows: list[dict[str, str]] = []
    connectors: list[dict[str, Any]] = []

    for index, spec in enumerate(specs):
        etype = spec.get("type")
        eid = _excalidraw_id(spec, index)
        if etype in ("rectangle", "ellipse", "diamond"):
            x, y, w, h = _spec_geometry(spec, default_w=160.0, default_h=80.0)
            shape = _shape_element(eid, str(etype), x, y, w, h)
            elements.append(shape)
            shapes[eid] = shape
            geometry[eid] = (x, y, w, h)
            label = _spec_label(spec)
            if label:
                _attach_bound_label(
                    shape, eid, label, (x, y, w, h), elements, text_rows
                )
        elif etype == "text":
            label = _spec_label(spec)
            if not label:
                continue
            x, y, w, h = _spec_geometry(
                spec, default_w=_estimate_text_width(label), default_h=25.0
            )
            text_id = _text_block_id(f"{eid}:text")
            elements.append(_free_text_element(text_id, label, x, y))
            text_rows.append({"id": text_id, "text": label})
        elif etype in ("arrow", "line"):
            connectors.append({"id": eid, "spec": spec, "type": etype})

    for connector in connectors:
        eid = connector["id"]
        spec = connector["spec"]
        element = _connector_element(eid, connector["type"], spec, geometry)
        if element is None:
            continue
        elements.append(element)
        for ref in (_as_ref(spec.get("from")), _as_ref(spec.get("to"))):
            if ref in shapes:
                _add_bound_element(shapes[ref], "arrow", eid)
        label = _spec_label(spec)
        if label:
            _attach_bound_label(
                element, eid, label, _connector_midbox(element), elements, text_rows
            )
    return elements, text_rows


def _attach_bound_label(
    host: dict[str, Any],
    eid: str,
    label: str,
    box: tuple[float, float, float, float],
    elements: list[dict[str, Any]],
    text_rows: list[dict[str, str]],
) -> None:
    """Attaches a label to its host shape or connector as a bound text element.

    One place owns the bound-label invariant. The label gets a deterministic 8-character
    id used identically for the element's own id, the host's reference back, and the
    index row.
    """
    label_id = _text_block_id(f"{eid}:label")
    elements.append(_bound_text_element(label_id, label, eid, box))
    _add_bound_element(host, "text", label_id)
    text_rows.append({"id": label_id, "text": label})


def _text_block_id(seed: str) -> str:
    """Builds a deterministic 8-character id for a text element.

    The Obsidian plugin re-reads the text-elements block as the authoritative text
    source, parsing it with a fixed-width pattern and advancing 12 characters per entry,
    so the id must be exactly 8 non-newline characters. An id of any other length is
    silently skipped and its text bleeds into the next entry.

    The issue #68 live-verify saw a 2-character label id merge into the following arrow
    label. So every text element gets an 8-character id from a stable seed, used
    identically for the element id, its container's reference, and the index row.
    """
    return hashlib.sha256(seed.encode()).hexdigest()[:8]


def _add_bound_element(host: dict[str, Any], etype: str, eid: str) -> None:
    """Appends a reference to a host's bound elements, initialising the list.

    A shape accrues one entry per bound label and per connector that snaps to it, and an
    arrow accrues its own label. The base element seeds the field to None, meaning
    nothing bound, so the first binding promotes it to a list.
    """
    bound = host.get("boundElements")
    if not isinstance(bound, list):
        bound = []
        host["boundElements"] = bound
    bound.append({"type": etype, "id": eid})


def _excalidraw_id(spec: dict[str, Any], index: int) -> str:
    """Returns the spec's id, or a stable positional fallback."""
    raw = spec.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"el{index}"


def _spec_label(spec: dict[str, Any]) -> str:
    """Pulls a label string from a spec's text or label field."""
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
    """Reads geometry from a spec with numeric fallbacks."""
    x = _as_float(spec.get("x"), 0.0)
    y = _as_float(spec.get("y"), 0.0)
    w = _as_float(spec.get("width"), default_w)
    h = _as_float(spec.get("height"), default_h)
    return x, y, max(w, 1.0), max(h, 1.0)


def _as_float(value: object, default: float) -> float:
    """Coerces a JSON number to a float."""
    return float(value) if isinstance(value, (int, float)) else default


def _estimate_text_width(text: str) -> float:
    """Estimates a text element's width from its length at the default font size."""
    return max(
        len(text) * _EXCALIDRAW_TEXT_FONT_SIZE * 0.6, float(_EXCALIDRAW_TEXT_FONT_SIZE)
    )


def _excalidraw_seed(eid: str, salt: str) -> int:
    """Builds a deterministic 31-bit seed for an element, so output stays stable."""
    digest = hashlib.sha256(f"{eid}:{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2_000_000_000


def _excalidraw_base(
    eid: str, etype: str, x: float, y: float, w: float, h: float
) -> dict[str, Any]:
    """Builds the styling and bookkeeping every element shares."""
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
    """Builds a closed-shape element, rounding a rectangle's corners."""
    element = _excalidraw_base(eid, etype, x, y, w, h)
    if etype == "rectangle":
        element["roundness"] = {"type": 3}
    return element


def _bound_text_element(
    eid: str, text: str, container_id: str, box: tuple[float, float, float, float]
) -> dict[str, Any]:
    """Builds a text element bound to a container.

    The label points at its host and the host references it back, set by the caller, so
    excalidraw treats the text as a property of the box or arrow rather than a loose
    overlay. A connector passes a zero-size box at the line midpoint, so the same
    centring maths places the label there.
    """
    x, y, w, h = box
    font = _EXCALIDRAW_TEXT_FONT_SIZE
    natural = _estimate_text_width(text)
    # A shape container caps the label at its inner width; a connector's zero-size
    # midpoint box does not (the label takes its natural width, centred on the line).
    if w > 0:
        tw = min(natural, max(w - 2 * _EXCALIDRAW_TEXT_PADDING, float(font)))
    else:
        tw = natural
    th = float(font) * _EXCALIDRAW_LINE_HEIGHT
    tx = x + (w - tw) / 2
    ty = y + (h - th) / 2
    element = _excalidraw_base(eid, "text", tx, ty, tw, th)
    element.update(_text_props(text, container_id=container_id, align="center"))
    return element


def _free_text_element(eid: str, text: str, x: float, y: float) -> dict[str, Any]:
    """Builds a free-standing text element, such as a title or loose label."""
    font = _EXCALIDRAW_TEXT_FONT_SIZE
    tw = _estimate_text_width(text)
    th = float(font) * _EXCALIDRAW_LINE_HEIGHT
    element = _excalidraw_base(eid, "text", x, y, tw, th)
    element.update(_text_props(text, container_id=None, align="left"))
    return element


def _text_props(text: str, *, container_id: str | None, align: str) -> dict[str, Any]:
    """Builds the text-specific properties shared by bound and free text."""
    font = _EXCALIDRAW_TEXT_FONT_SIZE
    return {
        "text": text,
        "rawText": text,
        "originalText": text,
        "fontSize": font,
        "fontFamily": 1,
        "textAlign": align,
        "verticalAlign": "middle",
        "baseline": round(font * 0.85, 2),
        "containerId": container_id,
        "lineHeight": _EXCALIDRAW_LINE_HEIGHT,
        "autoResize": True,
    }


def _connector_element(
    eid: str,
    etype: str,
    spec: dict[str, Any],
    geometry: dict[str, tuple[float, float, float, float]],
) -> dict[str, Any] | None:
    """Builds an arrow or line snapped to the edges of the named shapes.

    When both endpoint ids resolve, the connector binds to them. Each endpoint is the
    point on that box's edge facing the other box, plus a small gap, and the bindings
    record the bond so excalidraw keeps the arrow on the edges rather than the centres.
    It falls back to the spec's explicit points, unbound, when the ids do not resolve.
    """
    from_box = geometry.get(_as_ref(spec.get("from")))
    to_box = geometry.get(_as_ref(spec.get("to")))
    start_binding: dict[str, Any] | None = None
    end_binding: dict[str, Any] | None = None
    if from_box is not None and to_box is not None:
        start = _edge_point(from_box, _box_centre(to_box))
        end = _edge_point(to_box, _box_centre(from_box))
        x, y = start
        points = [[0.0, 0.0], [end[0] - start[0], end[1] - start[1]]]
        start_binding = _binding(_as_ref(spec.get("from")))
        end_binding = _binding(_as_ref(spec.get("to")))
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
            "startBinding": start_binding,
            "endBinding": end_binding,
            "startArrowhead": None,
            "endArrowhead": "arrow" if etype == "arrow" else None,
        }
    )
    return element


def _box_centre(box: tuple[float, float, float, float]) -> tuple[float, float]:
    """Returns the centre point of a box."""
    x, y, w, h = box
    return (x + w / 2, y + h / 2)


def _edge_point(
    box: tuple[float, float, float, float], target: tuple[float, float]
) -> tuple[float, float]:
    """Returns the point on a box's edge facing a target, pushed out by the gap.

    Casts a ray from the centre toward the target, finds where it crosses the bounding
    rectangle, then steps the binding gap further along it, so a bound arrow starts just
    off the border rather than at the centre.
    """
    cx, cy = _box_centre(box)
    _, _, w, h = box
    dx, dy = target[0] - cx, target[1] - cy
    distance = math.hypot(dx, dy)
    if distance == 0:
        return (cx, cy)
    scale_x = (w / 2) / abs(dx) if dx != 0 else math.inf
    scale_y = (h / 2) / abs(dy) if dy != 0 else math.inf
    edge = min(scale_x, scale_y)
    gap = _EXCALIDRAW_BINDING_GAP / distance
    return (cx + dx * (edge + gap), cy + dy * (edge + gap))


def _binding(element_id: str) -> dict[str, Any]:
    """Builds an arrow binding to a shape, aimed at its centre."""
    return {
        "elementId": element_id,
        "focus": 0.0,
        "gap": _EXCALIDRAW_BINDING_GAP,
    }


def _connector_midbox(
    element: dict[str, Any],
) -> tuple[float, float, float, float]:
    """Returns a zero-size box at a connector's midpoint, for centring its label.

    Reuses the connector's origin and relative end point so the label sits at the line
    midpoint, and the zero size makes the centring maths resolve to that exact point.
    """
    points = element["points"]
    mid_x = element["x"] + points[-1][0] / 2
    mid_y = element["y"] + points[-1][1] / 2
    return (mid_x, mid_y, 0.0, 0.0)


def _as_ref(value: object) -> str:
    """Returns a connector endpoint reference as a string."""
    return value.strip() if isinstance(value, str) else ""


def _as_points(value: object) -> list[list[float]] | None:
    """Coerces a model points value into coordinate pairs."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    points: list[list[float]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append([_as_float(item[0], 0.0), _as_float(item[1], 0.0)])
    return points if len(points) >= 2 else None
