"""Expansion of the model's simple node/connector specs into Excalidraw elements."""

from __future__ import annotations

import hashlib
import math
from typing import Any

# Excalidraw element defaults shared by every element (the renderer needs these present;
# Excalidraw's own restore() is tolerant, but emitting them in full keeps the scene OK
# across plugin versions). Per-type fields are layered on top in the builders below.
_EXCALIDRAW_TEXT_FONT_SIZE: int = 20
_EXCALIDRAW_LINE_HEIGHT: float = 1.25
# Padding between a bound label's text box and its container's edge (Excalidraw's own
# default container padding), and the gap a bound arrow leaves between its endpoint and
# the shape edge it snaps to (so the arrowhead does not sit on the border).
_EXCALIDRAW_TEXT_PADDING: float = 5.0
_EXCALIDRAW_BINDING_GAP: float = 8.0


def _build_excalidraw_elements(
    specs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Expand the model's simple node/connector specs into valid Excalidraw elements.

    The model returns only the *structure* (a shape's box + label, a connector's
    endpoints); this turns each spec into a fully-formed Excalidraw element with all the
    properties the renderer expects (issue #68 live-verify: the earlier minimal shapes
    with a ``label`` shorthand rendered as empty boxes). Specifically:

    * A ``rectangle``/``ellipse``/``diamond`` becomes a shape element, and -- when it
      carries a ``text`` label -- a **bound** text element: the label's ``containerId``
      points at the shape and the shape's ``boundElements`` references the label, so the
      text is a *property of the box* (Excalidraw centres, wraps, and moves it with the
      box) rather than a loose overlaid label.
    * A ``text`` spec becomes a free-standing text element.
    * An ``arrow``/``line`` joining two shapes (``from``/``to`` ids) is **bound** to
      them: its endpoints snap to the point on each box's edge facing the other box
      (not the centre) with a small gap, it carries ``startBinding``/``endBinding``, and
      each shape's ``boundElements`` references the connector -- so the arrow tracks the
      boxes and never plunges into their middles. A connector with explicit
      ``x``/``y``/``points`` (no resolvable shapes) is emitted unbound as a fallback.
    * A connector's own ``text`` label is bound to the connector (``containerId`` = the
      arrow), so Excalidraw places it at the line's midpoint over a masked background --
      near the line it labels, never crossing it.

    Unknown/malformed specs are skipped. Returns ``(elements, text_index_rows)`` where
    the rows feed the ``## Text Elements`` section.
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
    """Attach a label to its host (a shape or a connector) as a *bound* text element.

    One place owns the bound-label invariant: the label gets a deterministic 8-char id
    (:func:`_text_block_id`, seeded ``{eid}:label``) used identically for the text
    element's JSON ``id``, the host's ``boundElements`` reference, and the
    ``## Text Elements`` index row appended to ``text_rows``. ``box`` is the host's
    ``(x, y, w, h)`` (a connector passes its zero-size midpoint box).
    """
    label_id = _text_block_id(f"{eid}:label")
    elements.append(_bound_text_element(label_id, label, eid, box))
    _add_bound_element(host, "text", label_id)
    text_rows.append({"id": label_id, "text": label})


def _text_block_id(seed: str) -> str:
    """A deterministic 8-character id for a text element (its ``## Text Elements`` key).

    The Obsidian-Excalidraw plugin re-reads the ``## Text Elements`` markdown block as
    the authoritative text source, parsing it with ``/\\s\\^(.{8})[\\n]+/`` and
    advancing a fixed 12 chars (`` ^12345678\\n\\n``) per entry: the block id must be
    **exactly 8 non-newline chars**. An id of any other length is silently skipped and
    its entry's text bleeds into the next 8-char id (issue #68 live-verify: a 2-char
    free-standing-label id merged into the following arrow label). So every text element
    thoth writes -- box label, connector label, free-standing text -- gets an 8-char id
    derived from a stable seed (the owning element id + role), used identically for the
    element's JSON ``id``, its container's ``boundElements`` ref, and the index row.
    """
    return hashlib.sha256(seed.encode()).hexdigest()[:8]


def _add_bound_element(host: dict[str, Any], etype: str, eid: str) -> None:
    """Append a ``{type, id}`` reference to ``host``'s ``boundElements`` (init to list).

    A shape accrues one entry per bound label and per connector that snaps to it; an
    arrow accrues its bound label. ``_excalidraw_base`` seeds ``boundElements`` to
    ``None`` (Excalidraw's "nothing bound"), so the first binding promotes it to a list.
    """
    bound = host.get("boundElements")
    if not isinstance(bound, list):
        bound = []
        host["boundElements"] = bound
    bound.append({"type": etype, "id": eid})


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


def _bound_text_element(
    eid: str, text: str, container_id: str, box: tuple[float, float, float, float]
) -> dict[str, Any]:
    """A text element *bound* to a container (a shape's box, or a connector's midpoint).

    The label's ``containerId`` points at its host and the host's ``boundElements``
    references it (set by the caller), so Excalidraw treats the text as a property of
    the box/arrow -- centred, wrapped, and moved with it -- not a loose overlaid label.
    ``box`` is the host's ``(x, y, w, h)``; a connector passes a zero-size box at the
    line midpoint (see :func:`_connector_midbox`) so the same centring maths places the
    label there.
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
    """A free-standing (unbound) text element -- a title/loose label at ``x``/``y``."""
    font = _EXCALIDRAW_TEXT_FONT_SIZE
    tw = _estimate_text_width(text)
    th = float(font) * _EXCALIDRAW_LINE_HEIGHT
    element = _excalidraw_base(eid, "text", x, y, tw, th)
    element.update(_text_props(text, container_id=None, align="left"))
    return element


def _text_props(text: str, *, container_id: str | None, align: str) -> dict[str, Any]:
    """The text-specific property set shared by bound + free-standing text elements."""
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
    """Build an arrow/line, snapped to the edges of the shapes named by ``from``/``to``.

    When both endpoint ids resolve to shapes, the connector binds to them: each
    endpoint is the point on that box's edge facing the *other* box (plus a small gap),
    and ``startBinding``/``endBinding`` record the bond so Excalidraw keeps the arrow
    snapped to the boxes' edges -- never their centres. Falls back to the spec's
    explicit ``x``/``y``/``points`` (unbound) when the ids are not resolvable; returns
    ``None`` when neither a routable pair nor explicit points exist (so a dangling
    connector is dropped, not emitted malformed).
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
    """The centre point of an ``(x, y, w, h)`` box."""
    x, y, w, h = box
    return (x + w / 2, y + h / 2)


def _edge_point(
    box: tuple[float, float, float, float], target: tuple[float, float]
) -> tuple[float, float]:
    """The point on ``box``'s edge facing ``target``, pushed out by the binding gap.

    Casts a ray from the box centre toward ``target`` and finds where it crosses the
    box's bounding rectangle, then steps :data:`_EXCALIDRAW_BINDING_GAP` further along
    that ray -- so a bound arrow starts/ends just off the shape's border (its snap
    point) rather than at the centre. A degenerate (coincident) target returns centre.
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
    """An Excalidraw arrow binding to a shape (``focus`` 0 aims at the shape centre)."""
    return {
        "elementId": element_id,
        "focus": 0.0,
        "gap": _EXCALIDRAW_BINDING_GAP,
    }


def _connector_midbox(
    element: dict[str, Any],
) -> tuple[float, float, float, float]:
    """A zero-size box at a built connector's midpoint, for centring its bound label.

    Reuses the connector's absolute origin (``x``/``y``) and its relative end point so
    the label sits at the line's midpoint; the zero width/height make
    :func:`_bound_text_element`'s centring resolve to that exact point.
    """
    points = element["points"]
    mid_x = element["x"] + points[-1][0] / 2
    mid_y = element["y"] + points[-1][1] / 2
    return (mid_x, mid_y, 0.0, 0.0)


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
