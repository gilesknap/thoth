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
from typing import Any

import pytest

from thoth.analyse import (
    AnalyseError,
    Analyser,
    Analysis,
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

    for heavy in ("anthropic", "exa_py", "firecrawl", "slack_bolt", "whisper"):
        assert heavy not in sys.modules
