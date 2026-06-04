"""Tests for :mod:`thoth.intent` -- the Slack free-text intent gate (issue #5).

The classifier is exercised with an injected fake :class:`~thoth.llm.LLM` so no real
``anthropic`` client is built and no network is touched. The seam is **total**: every
model / parse failure must fall back to the safe ``query`` route rather than raising.
"""

from __future__ import annotations

from typing import Any

import pytest

from thoth.config import Config, load_config
from thoth.intent import (
    DEFAULT_INTENT_MODEL,
    INTENT_INSTRUCTIONS,
    IntentClassifier,
    IntentDecision,
)
from thoth.llm import LLM


@pytest.fixture
def config() -> Config:
    """A minimal frozen Config (no disk access needed for these tests)."""
    return load_config({"PKM_VAULT": "/x"})


def _response(text: str) -> dict[str, Any]:
    """Shape a fake Anthropic response carrying a single text block."""
    return {"content": [{"type": "text", "text": text}]}


class _FakeMessages:
    """Records ``create`` kwargs and returns a canned response (or raises)."""

    def __init__(self, response: Any, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._error = error

    def create(self, **kwargs: Any) -> Any:
        """Record kwargs; raise the canned error or return the canned response."""
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClient:
    """Structural stand-in for the Anthropic SDK exposing ``.messages.create``."""

    def __init__(self, response: Any, error: Exception | None = None) -> None:
        self.messages = _FakeMessages(response, error)


def _classifier(
    config: Config,
    *,
    text: str | None = None,
    error: Exception | None = None,
    model: str = DEFAULT_INTENT_MODEL,
) -> tuple[IntentClassifier, _FakeClient]:
    """Build an IntentClassifier over a fake client returning ``text`` (or raising)."""
    client = _FakeClient(_response(text or ""), error)
    return IntentClassifier(LLM(config, client=client), model=model), client


# --- IntentDecision.route ----------------------------------------------------


def test_default_intent_model_is_a_dated_haiku() -> None:
    """The cheap default model is a dated Haiku id (not a bare, 404-prone alias)."""
    assert DEFAULT_INTENT_MODEL == "claude-haiku-4-5-20251001"


@pytest.mark.parametrize("intent", ["capture", "query"])
def test_route_passes_through_when_confident(intent: str) -> None:
    """A high/medium-confidence verdict routes to its named intent verbatim."""
    assert IntentDecision(intent=intent, confidence="high").route == intent
    assert IntentDecision(intent=intent, confidence="medium").route == intent


@pytest.mark.parametrize("intent", ["capture", "query"])
def test_low_confidence_always_routes_to_query(intent: str) -> None:
    """A low-confidence verdict collapses to query whatever the guessed intent."""
    assert IntentDecision(intent=intent, confidence="low").route == "query"


# --- classify happy paths ----------------------------------------------------


@pytest.mark.parametrize("intent", ["capture", "query"])
def test_classify_parses_each_intent(config: Config, intent: str) -> None:
    """A well-formed JSON verdict is parsed into the matching decision."""
    classifier, _ = _classifier(
        config, text=f'{{"intent": "{intent}", "confidence": "high"}}'
    )
    decision = classifier.classify("some message")
    assert decision.intent == intent
    assert decision.confidence == "high"


def test_classify_logs_routing_decision(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """classify emits one concise INFO line: intent, confidence + route (issue #52)."""
    classifier, _ = _classifier(
        config, text='{"intent": "capture", "confidence": "high"}'
    )
    with caplog.at_level("INFO", logger="thoth.intent"):
        classifier.classify("remind me to call the dentist")
    records = [r for r in caplog.records if "intent routed:" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "capture" in msg
    assert "high" in msg
    assert "-> capture" in msg


def test_classify_parses_a_fenced_json_block(config: Config) -> None:
    """A ```json fenced verdict is parsed too (parse_json_block strips the fence)."""
    classifier, _ = _classifier(
        config,
        text='```json\n{"intent": "query", "confidence": "medium"}\n```',
    )
    assert classifier.classify("what are my todos").intent == "query"


def test_classify_sends_text_under_instructions_with_haiku(config: Config) -> None:
    """classify sends the message as the user turn, with gate prompt + cheap model."""
    classifier, client = _classifier(
        config, text='{"intent": "capture", "confidence": "high"}'
    )
    classifier.classify("remind me to call the dentist")
    call = client.messages.calls[0]
    assert call["model"] == DEFAULT_INTENT_MODEL
    assert call["messages"] == [
        {"role": "user", "content": "remind me to call the dentist"}
    ]
    # The gate task prompt rides as an uncached system block after the cached persona.
    assert any(block.get("text") == INTENT_INSTRUCTIONS for block in call["system"])


def test_classify_honours_model_override(config: Config) -> None:
    """A custom model id (e.g. from THOTH_INTENT_MODEL) flows to the create call."""
    classifier, client = _classifier(
        config, text='{"intent": "query", "confidence": "high"}', model="custom-model-1"
    )
    classifier.classify("hello")
    assert client.messages.calls[0]["model"] == "custom-model-1"


# --- classify fail-safe paths ------------------------------------------------


def test_classify_falls_back_to_query_on_model_error(config: Config) -> None:
    """A raising model call is swallowed and yields the safe low-confidence query."""
    classifier, _ = _classifier(config, error=RuntimeError("api exploded"))
    decision = classifier.classify("anything")
    assert decision == IntentDecision(intent="query", confidence="low")
    assert decision.route == "query"


def test_classify_falls_back_to_query_on_unparseable_output(config: Config) -> None:
    """Non-JSON model output yields the safe default rather than raising."""
    classifier, _ = _classifier(config, text="I think this is a question, sorry!")
    assert classifier.classify("anything").route == "query"


def test_classify_rejects_unknown_intent(config: Config) -> None:
    """An out-of-range intent is untrustworthy -> the verdict defaults to query."""
    classifier, _ = _classifier(
        config, text='{"intent": "delete-everything", "confidence": "high"}'
    )
    assert classifier.classify("anything").route == "query"


def test_classify_treats_bad_confidence_as_low(config: Config) -> None:
    """A valid intent with missing/garbage confidence routes conservatively to query."""
    classifier, _ = _classifier(config, text='{"intent": "capture"}')
    decision = classifier.classify("anything")
    assert decision.intent == "capture"
    assert decision.confidence == "low"
    # ...so a capture the model wasn't sure about is still queried, not filed.
    assert decision.route == "query"


# --- keywords (issue #102) ---------------------------------------------------


def test_intent_decision_defaults_to_no_keywords() -> None:
    """An IntentDecision built without keywords carries the empty tuple, not None."""
    assert IntentDecision(intent="query", confidence="high").keywords == ()


def test_classify_parses_keywords(config: Config) -> None:
    """A well-formed keywords list is parsed into the decision's tuple (issue #102)."""
    classifier, _ = _classifier(
        config,
        text='{"intent": "query", "confidence": "high", '
        '"keywords": ["dog", "Labradoodle", "pet"]}',
    )
    decision = classifier.classify("list me the docs about dogs")
    assert decision.keywords == ("dog", "Labradoodle", "pet")


def test_classify_keywords_strips_and_drops_empties(config: Config) -> None:
    """Whitespace-only / blank keyword entries are dropped and the rest trimmed."""
    classifier, _ = _classifier(
        config,
        text='{"intent": "query", "confidence": "high", '
        '"keywords": ["  dog  ", "", "   ", "pet"]}',
    )
    assert classifier.classify("anything").keywords == ("dog", "pet")


@pytest.mark.parametrize(
    "blob",
    [
        '{"intent": "query", "confidence": "high"}',  # absent
        '{"intent": "query", "confidence": "high", "keywords": "dog"}',  # bare string
        '{"intent": "query", "confidence": "high", "keywords": 42}',  # number
        '{"intent": "query", "confidence": "high", "keywords": null}',  # null
        '{"intent": "query", "confidence": "high", "keywords": [1, 2, 3]}',  # non-str
    ],
)
def test_classify_malformed_keywords_degrade_to_empty(
    config: Config, blob: str
) -> None:
    """A missing/garbled keywords field degrades to () without raising (totality)."""
    classifier, _ = _classifier(config, text=blob)
    decision = classifier.classify("anything")
    # The verdict still parses (intent/confidence intact); only keywords degrade.
    assert decision.intent == "query"
    assert decision.keywords == ()


def test_classify_model_error_yields_no_keywords(config: Config) -> None:
    """The fail-safe default decision carries no keywords (issue #102 totality)."""
    classifier, _ = _classifier(config, error=RuntimeError("api exploded"))
    assert classifier.classify("anything").keywords == ()
