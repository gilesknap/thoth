"""Tests for :mod:`thoth.analyse` -- the vision/PDF content-analysis seam (issue #42).

The :class:`thoth.analyse.Analyser` wraps a real :class:`thoth.llm.LLM` driven by a fake
Anthropic client, so the vision/document content blocks the SDK would receive are
asserted directly with NO real model call. The bytes are sent as a *transient* base64
content block (ADR 0006); these tests prove the block shape, the parse, and that a
budget-cap trip propagates (so the ingest pass can defer) rather than being swallowed.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest

from thoth.analyse import (
    _EXCALIDRAW_MAX_TOKENS,
    AnalyseError,
    Analyser,
    Analysis,
    _text_block_id,
    image_media_type,
)
from thoth.budget import BudgetExceededError
from thoth.config import load_config
from thoth.llm import LLM


def _text_response(text: str) -> dict[str, Any]:
    """Shape a fake Anthropic response as ``extract_text`` reads it."""
    return {"content": [{"type": "text", "text": text}]}


class _CapturingMessages:
    """A fake ``client.messages`` recording kwargs, returning a canned response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


class _CapturingClient:
    """A fake Anthropic client exposing :class:`_CapturingMessages`."""

    def __init__(self, response_text: str) -> None:
        self.messages = _CapturingMessages(_text_response(response_text))


class _RaisingMessages:
    """A fake ``messages`` whose ``create`` raises the configured error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def create(self, **kwargs: Any) -> Any:
        raise self._error


class _RaisingClient:
    """A fake client whose ``messages.create`` always raises."""

    def __init__(self, error: Exception) -> None:
        self.messages = _RaisingMessages(error)


def _analysis_json() -> str:
    """A canned analysis result the model would return."""
    return json.dumps(
        {
            "text": "Sprint goals: ship vision pass",
            "description": "A whiteboard photo.",
            "summary": "Sprint whiteboard",
            "suggested_type": "note",
            "entities": ["Giles"],
            "concepts": ["sprint-planning"],
        }
    )


def _config():
    """A minimal config (no real keys needed; the client is injected)."""
    return load_config({"PKM_VAULT": "/x", "ANTHROPIC_API_KEY": "test-key"})


def test_image_media_type_maps_known_and_defaults() -> None:
    """Known image extensions map to their media type; unknown defaults to png."""
    assert image_media_type("png") == "image/png"
    assert image_media_type("jpg") == "image/jpeg"
    assert image_media_type("jpeg") == "image/jpeg"
    assert image_media_type(".GIF") == "image/gif"
    assert image_media_type("heic") == "image/png"  # unknown -> default


def test_analyse_image_sends_base64_vision_block_and_parses() -> None:
    """analyse_image sends a base64 image block + prompt and parses the JSON result."""
    client = _CapturingClient(_analysis_json())
    analyser = Analyser(LLM(_config(), client=client))
    image_bytes = b"\x89PNG\r\n\x1a\nfake"

    result = analyser.analyse_image(image_bytes, ext="png")

    assert result.text == "Sprint goals: ship vision pass"
    assert result.suggested_type == "note"
    assert result.entities == ["Giles"]
    assert result.concepts == ["sprint-planning"]

    # The vision content block carried the bytes as TRANSIENT base64 (ADR 0006).
    create_kwargs = client.messages.calls[-1]
    content = create_kwargs["messages"][0]["content"]
    image_block = content[0]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == base64.standard_b64encode(
        image_bytes
    ).decode("ascii")
    # A text instruction block follows the image.
    assert content[1]["type"] == "text"


def test_analyse_images_sends_one_block_per_image_in_a_single_call() -> None:
    """analyse_images sends N image blocks + one prompt in ONE call (issue #124).

    A multi-image batch is one shared-summary call: each image gets its own base64
    ``image`` block (in order), the instruction text follows last, and there is exactly
    ONE model call -- one charge against the budget guard.
    """
    client = _CapturingClient(_analysis_json())
    analyser = Analyser(LLM(_config(), client=client))
    img_a = b"\x89PNG-A"
    img_b = b"\xff\xd8\xff-B"

    analyser.analyse_images([(img_a, "png"), (img_b, "jpg")])

    assert len(client.messages.calls) == 1  # ONE call, not two
    content = client.messages.calls[-1]["messages"][0]["content"]
    # Two image blocks (in order) then the trailing text instruction.
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[0]["source"]["data"] == base64.standard_b64encode(img_a).decode(
        "ascii"
    )
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"] == base64.standard_b64encode(img_b).decode(
        "ascii"
    )
    assert content[2]["type"] == "text"


def test_analyse_images_rejects_empty() -> None:
    """analyse_images with no images is a programming error (ValueError)."""
    analyser = Analyser(LLM(_config(), client=_CapturingClient(_analysis_json())))
    with pytest.raises(ValueError, match="at least one image"):
        analyser.analyse_images([])


def test_analyse_pdf_sends_base64_document_block_and_parses() -> None:
    """analyse_pdf sends a base64 document block + prompt and parses the result."""
    client = _CapturingClient(_analysis_json())
    analyser = Analyser(LLM(_config(), client=client))
    pdf_bytes = b"%PDF-1.7\nbody"

    result = analyser.analyse_pdf(pdf_bytes)

    assert result.suggested_type == "note"
    create_kwargs = client.messages.calls[-1]
    doc_block = create_kwargs["messages"][0]["content"][0]
    assert doc_block["type"] == "document"
    assert doc_block["source"]["media_type"] == "application/pdf"
    assert doc_block["source"]["data"] == base64.standard_b64encode(pdf_bytes).decode(
        "ascii"
    )


def test_unparseable_result_raises_analyse_error() -> None:
    """A non-JSON model reply becomes an AnalyseError (the ingest pass files blind)."""
    client = _CapturingClient("sorry, I can't read that image")
    analyser = Analyser(LLM(_config(), client=client))
    with pytest.raises(AnalyseError):
        analyser.analyse_image(b"bytes", ext="png")


def test_budget_trip_propagates_for_deferral() -> None:
    """A BudgetExceededError from the LLM propagates unchanged (so ingest defers)."""
    analyser = Analyser(
        LLM(_config(), client=_RaisingClient(BudgetExceededError("cap reached")))
    )
    with pytest.raises(BudgetExceededError):
        analyser.analyse_image(b"bytes", ext="png")


def test_transport_failure_propagates_for_deferral() -> None:
    """A transport failure propagates (it is NOT wrapped as AnalyseError)."""
    analyser = Analyser(LLM(_config(), client=_RaisingClient(RuntimeError("down"))))
    with pytest.raises(RuntimeError):
        analyser.analyse_pdf(b"bytes")


def test_analysis_body_markdown_renders_description_and_text() -> None:
    """body_markdown composes the description + an Extracted text section."""
    analysis = Analysis(text="line one\nline two", description="A diagram.")
    rendered = analysis.body_markdown()
    assert "A diagram." in rendered
    assert "## Extracted text" in rendered
    assert "line one" in rendered


def test_empty_analysis_is_empty() -> None:
    """An analysis with no text/description reports empty (ingest keeps the stub)."""
    assert Analysis().is_empty() is True
    assert Analysis(description="x").is_empty() is False


def test_module_import_is_light() -> None:
    """Importing thoth.analyse pulls in no heavy/absent third-party client."""
    import sys

    import thoth.analyse  # noqa: F401

    for heavy in ("anthropic", "firecrawl", "slack_bolt", "whisper"):
        assert heavy not in sys.modules


# --- kind detection folded into the analyse call (issue #68 / ADR 0009) ---------------


def _analysis_json_with_kind(kind: str) -> str:
    """The canned analysis result plus a reported image ``kind``."""
    return json.dumps(
        {
            "text": "Sprint goals: ship vision pass",
            "description": "A whiteboard photo.",
            "summary": "Sprint whiteboard",
            "suggested_type": "note",
            "entities": ["Giles"],
            "concepts": ["sprint-planning"],
            "kind": kind,
        }
    )


def test_analyse_parses_valid_kind() -> None:
    """A valid 'kind' is parsed straight onto the Analysis."""
    client = _CapturingClient(_analysis_json_with_kind("diagram"))
    analyser = Analyser(LLM(_config(), client=client))

    result = analyser.analyse_image(b"bytes", ext="png")

    assert result.kind == "diagram"


def test_analyse_normalises_unknown_kind_to_empty() -> None:
    """An unrecognised 'kind' label collapses to '' (ingest derives nothing)."""
    client = _CapturingClient(_analysis_json_with_kind("blueprint"))
    analyser = Analyser(LLM(_config(), client=client))

    result = analyser.analyse_image(b"bytes", ext="png")

    assert result.kind == ""


def test_analyse_missing_kind_defaults_to_empty() -> None:
    """A reply with no 'kind' key yields '' (the four-value default)."""
    client = _CapturingClient(_analysis_json())
    analyser = Analyser(LLM(_config(), client=client))

    result = analyser.analyse_image(b"bytes", ext="png")

    assert result.kind == ""


def test_result_shape_mentions_faithful_markdown_for_documents() -> None:
    """The combined prompt asks for faithful structured markdown on a document."""
    from thoth.analyse import _RESULT_SHAPE

    lowered = _RESULT_SHAPE.lower()
    assert "faithful" in lowered
    assert "markdown" in lowered
    # The four kinds are defined in the prompt.
    for kind in ("diagram", "document", "screenshot", "photo"):
        assert kind in lowered


# --- Excalidraw reconstruction (issue #68) --------------------------------------------


def _excalidraw_specs() -> list[dict[str, Any]]:
    """The simple node/connector specs the model is asked to return (issue #68).

    Two labelled boxes joined by an arrow plus a free-standing title -- the exact shape
    thoth must expand into valid Excalidraw elements (the live-verify case).
    """
    return [
        {
            "id": "n1",
            "type": "rectangle",
            "x": 100,
            "y": 100,
            "width": 160,
            "height": 80,
            "text": "Shared IOC",
        },
        {
            "id": "n2",
            "type": "rectangle",
            "x": 100,
            "y": 300,
            "width": 160,
            "height": 80,
            "text": "Device",
        },
        {"id": "a1", "type": "arrow", "from": "n1", "to": "n2"},
        {"id": "t1", "type": "text", "x": 100, "y": 40, "text": "Boot architecture"},
    ]


def _excalidraw_response() -> str:
    """A canned model reply carrying only the simple specs."""
    return json.dumps({"elements": _excalidraw_specs()})


def _parse_excalidraw_scene(markdown: str) -> dict[str, Any]:
    """Extract and parse the JSON scene out of the .excalidraw.md fenced block."""
    fence = "```json\n"
    start = markdown.index(fence) + len(fence)
    end = markdown.index("\n```", start)
    return json.loads(markdown[start:end])


def test_reconstruct_excalidraw_builds_envelope() -> None:
    """A diagram reconstruction returns a valid .excalidraw.md envelope with real
    elements expanded from the model's simple node/connector specs (issue #68)."""
    client = _CapturingClient(_excalidraw_response())
    analyser = Analyser(LLM(_config(), client=client))

    markdown = analyser.reconstruct_excalidraw(b"bytes", ext="png")

    assert markdown is not None
    # Frontmatter + the plugin's switch-to-Excalidraw banner + the indexed text section.
    assert "excalidraw-plugin: parsed" in markdown
    assert "tags: [excalidraw]" in markdown
    assert "Switch to EXCALIDRAW VIEW" in markdown
    assert "# Excalidraw Data" in markdown
    assert "## Text Elements" in markdown
    assert "## Drawing" in markdown
    # The labels are indexed in the ## Text Elements section for Obsidian search.
    assert "Shared IOC ^" in markdown
    assert "Boot architecture ^" in markdown

    scene = _parse_excalidraw_scene(markdown)
    assert scene["type"] == "excalidraw"
    assert scene["version"] == 2
    assert scene["source"] == "thoth"
    assert scene["appState"]["viewBackgroundColor"] == "#ffffff"
    assert scene["files"] == {}

    elements = scene["elements"]
    by_id = {element["id"]: element for element in elements}
    by_type: dict[str, list[dict[str, Any]]] = {}
    for element in elements:
        by_type.setdefault(element["type"], []).append(element)
        # Every element is fully formed (the empty-box failure was missing props/ids).
        assert element["id"]
        assert element["strokeColor"] == "#1e1e1e"
        assert element["isDeleted"] is False
    # Two boxes, three text elements (two box labels + the title), one arrow.
    assert len(by_type["rectangle"]) == 2
    assert len(by_type["text"]) == 3
    assert {t["text"] for t in by_type["text"]} == {
        "Shared IOC",
        "Device",
        "Boot architecture",
    }
    # A box label is BOUND to its box: containerId -> the box, the box's boundElements
    # -> the label, so the text is a property of the box (not a loose overlaid label).
    # Text-element ids are deterministic 8-char block ids (the plugin's parser needs
    # exactly 8 chars), keyed off the owner id + role.
    n1_label_id = _text_block_id("n1:label")
    n1_label = by_id[n1_label_id]
    assert n1_label["text"] == "Shared IOC"
    assert n1_label["containerId"] == "n1"
    assert n1_label["textAlign"] == "center"
    assert len(n1_label_id) == 8
    assert {"type": "text", "id": n1_label_id} in by_id["n1"]["boundElements"]
    # The free-standing title is unbound and also carries an 8-char block id.
    title = by_id[_text_block_id("t1:text")]
    assert title["text"] == "Boot architecture"
    assert title["containerId"] is None

    arrow = by_type["arrow"][0]
    assert arrow["endArrowhead"] == "arrow"
    # The arrow SNAPS to the boxes' edges (not their centres): n1 spans y=100..180 and
    # n2 spans y=300..380, both centred on x=180. The arrow leaves n1's bottom edge and
    # reaches n2's top edge, each offset by the 8px binding gap.
    assert arrow["x"] == 180.0
    assert arrow["y"] == 188.0  # n1 bottom edge (180) + 8px gap
    assert arrow["points"][0] == [0.0, 0.0]
    assert arrow["points"][1] == [0.0, 104.0]  # down to n2 top edge (300) - 8px gap
    # The bond is recorded both ways: the arrow binds to each box, and each box's
    # boundElements references the arrow.
    assert arrow["startBinding"]["elementId"] == "n1"
    assert arrow["endBinding"]["elementId"] == "n2"
    assert arrow["startBinding"]["gap"] == 8.0
    assert {"type": "arrow", "id": "a1"} in by_id["n1"]["boundElements"]
    assert {"type": "arrow", "id": "a1"} in by_id["n2"]["boundElements"]


def test_reconstruct_excalidraw_dangling_connector_is_dropped() -> None:
    """An arrow whose endpoints resolve to nothing (no from/to, no points) is dropped
    rather than emitted malformed; the rest of the scene still builds."""
    specs = [
        {
            "id": "n1",
            "type": "rectangle",
            "x": 0,
            "y": 0,
            "width": 80,
            "height": 40,
            "text": "A",
        },
        {"id": "a1", "type": "arrow", "from": "missing", "to": "alsogone"},
    ]
    client = _CapturingClient(json.dumps({"elements": specs}))
    analyser = Analyser(LLM(_config(), client=client))

    markdown = analyser.reconstruct_excalidraw(b"bytes", ext="png")

    assert markdown is not None
    scene = _parse_excalidraw_scene(markdown)
    assert not [e for e in scene["elements"] if e["type"] == "arrow"]
    assert [e for e in scene["elements"] if e["type"] == "rectangle"]


def test_reconstruct_excalidraw_connector_label_is_bound_to_the_line() -> None:
    """A connector that carries a 'text' label gets a text element bound to the arrow
    (containerId -> the arrow, the arrow's boundElements -> the label), placed at the
    line's midpoint -- so the label sits on the line it labels, not crossing it."""
    specs = [
        {
            "id": "n1",
            "type": "rectangle",
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 60,
            "text": "A",
        },
        {
            "id": "n2",
            "type": "rectangle",
            "x": 0,
            "y": 300,
            "width": 100,
            "height": 60,
            "text": "B",
        },
        {"id": "a1", "type": "arrow", "from": "n1", "to": "n2", "text": "depends on"},
    ]
    client = _CapturingClient(json.dumps({"elements": specs}))
    analyser = Analyser(LLM(_config(), client=client))

    markdown = analyser.reconstruct_excalidraw(b"bytes", ext="png")

    assert markdown is not None
    scene = _parse_excalidraw_scene(markdown)
    by_id = {e["id"]: e for e in scene["elements"]}
    label_id = _text_block_id("a1:label")
    label = by_id[label_id]
    assert label["text"] == "depends on"
    assert label["containerId"] == "a1"
    assert len(label_id) == 8
    assert {"type": "text", "id": label_id} in by_id["a1"]["boundElements"]
    # The label is centred on the line's midpoint (between n1's bottom + n2's top edge).
    arrow = by_id["a1"]
    mid_y = arrow["y"] + arrow["points"][-1][1] / 2
    assert label["y"] < mid_y < label["y"] + label["height"] + 1
    # The connector label is also indexed for Obsidian search, with its 8-char id.
    assert f"depends on ^{label_id}" in markdown


def test_reconstruct_excalidraw_text_block_ids_are_all_eight_chars() -> None:
    """Every ## Text Elements block id is EXACTLY 8 chars and ends ` ^xxxxxxxx\\n\\n`.

    The Obsidian-Excalidraw parser (``/\\s\\^(.{8})[\\n]+/``, advancing a fixed 12 chars
    per entry) only recognises 8-char ids; a shorter one (a model's ``t1``) is skipped
    and its text bleeds into the next entry. This pins the fix: a free-standing label
    with a 2-char model id still gets a parseable 8-char anchor, so it cannot merge."""
    specs = [
        {"id": "t1", "type": "text", "x": 0, "y": 0, "text": "Obsidian Vault"},
        {
            "id": "n1",
            "type": "rectangle",
            "x": 0,
            "y": 100,
            "width": 80,
            "height": 40,
            "text": "thoth",
        },
        {"id": "a1", "type": "arrow", "from": "n1", "to": "n1", "text": "ingest"},
    ]
    client = _CapturingClient(json.dumps({"elements": specs}))
    analyser = Analyser(LLM(_config(), client=client))

    markdown = analyser.reconstruct_excalidraw(b"bytes", ext="png")

    assert markdown is not None
    section = markdown[
        markdown.index("## Text Elements") : markdown.index("## Drawing")
    ]
    # Each non-empty entry matches the plugin's strict ` ^<8 chars>\n\n` shape.
    entries = re.findall(r" \^(.+?)\n\n", section)
    assert entries, "no text-element anchors found"
    assert all(len(block_id) == 8 for block_id in entries)
    # The free-standing 'Obsidian Vault' label is its own parseable entry (8-char anchor
    # + blank line), so it cannot bleed into the following 'ingest' label.
    assert re.search(r"Obsidian Vault \^\w{8}\n\n", section)


def test_reconstruct_excalidraw_passes_diagram_model() -> None:
    """The injected diagram_model is threaded through to the client kwargs."""
    client = _CapturingClient(_excalidraw_response())
    analyser = Analyser(LLM(_config(), client=client), diagram_model="claude-opus-4-8")

    analyser.reconstruct_excalidraw(b"bytes", ext="png")

    create_kwargs = client.messages.calls[-1]
    assert create_kwargs["model"] == "claude-opus-4-8"
    # The reconstruction call gets the roomier dedicated token budget, not the analyse
    # one (a valid Excalidraw scene needs more headroom than OCR + a description).
    assert create_kwargs["max_tokens"] == _EXCALIDRAW_MAX_TOKENS
    # The image is carried as a transient base64 vision block.
    content = create_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "text"


def test_reconstruct_excalidraw_unparseable_returns_none() -> None:
    """An unparseable model reply degrades to None (never raises)."""
    client = _CapturingClient("sorry, can't draw that")
    analyser = Analyser(LLM(_config(), client=client))

    assert analyser.reconstruct_excalidraw(b"bytes", ext="png") is None


def test_reconstruct_excalidraw_empty_elements_returns_none() -> None:
    """An empty (or missing) element list degrades to None."""
    client = _CapturingClient(json.dumps({"elements": []}))
    analyser = Analyser(LLM(_config(), client=client))

    assert analyser.reconstruct_excalidraw(b"bytes", ext="png") is None


def test_reconstruct_excalidraw_non_dict_elements_returns_none() -> None:
    """Elements that are not all dicts degrade to None."""
    client = _CapturingClient(json.dumps({"elements": ["not-a-dict"]}))
    analyser = Analyser(LLM(_config(), client=client))

    assert analyser.reconstruct_excalidraw(b"bytes", ext="png") is None


def test_reconstruct_excalidraw_client_error_returns_none() -> None:
    """A transport failure on the reconstruction call degrades to None (no defer)."""
    analyser = Analyser(LLM(_config(), client=_RaisingClient(RuntimeError("down"))))

    assert analyser.reconstruct_excalidraw(b"bytes", ext="png") is None


def test_reconstruct_excalidraw_budget_trip_returns_none() -> None:
    """Even a budget trip degrades to None: the enhancement never defers the capture."""
    analyser = Analyser(
        LLM(_config(), client=_RaisingClient(BudgetExceededError("cap reached")))
    )

    assert analyser.reconstruct_excalidraw(b"bytes", ext="png") is None
