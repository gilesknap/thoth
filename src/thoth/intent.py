"""The Slack free-text intent gate (issue #5), routing bare prose to an engine.

The Slack surface routes a message with a deterministic ladder of a capture prefix, a
bare URL or a shared file, and historically defaulted everything else to a query. That
made plain prose like "remind me to call the dentist tomorrow" get searched rather than
filed, so capturing a free thought needed a prefix. This adds the one missing decision:
for a message hitting none of those short-circuits, ask a cheap model which engine the
user meant.

The gate only chooses an engine. It never blends them and never gives the read-only
query path write access (SPEC sections 3 and 12), and the deterministic prefixes stay as
the escape hatch when the model guesses wrong.

Three constraints hold:

* **Slack-only.** MCP already exposes explicit tools, so the calling agent does its own
  dispatch.
* **Total and fail-safe.** Any model, network or parse failure routes to query.
  Answering a misfiled note is harmless, while silently filing a real question is not.
* **Cheap.** One small model call per bare free-text message, reusing the cached persona
  prefix.

Only :mod:`thoth.llm` is imported, so this module is import-safe under pytest collection
and never pulls in the SDK by itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from thoth.llm import LLM, Message, extract_text, parse_json_block

__all__ = [
    "DEFAULT_INTENT_MODEL",
    "INTENT_INSTRUCTIONS",
    "IntentClassifier",
    "IntentDecision",
]

logger = logging.getLogger(__name__)

DEFAULT_INTENT_MODEL: str = "claude-haiku-4-5-20251001"
"""The cheap model the gate uses by default (a dated Haiku id, not a bare alias)."""

_MAX_TOKENS: int = 64
"""The classifier emits a tiny JSON object; cap generation hard to stay cheap/fast."""

# The two engines the gate can route a bare free-text message to
_VALID_INTENTS: frozenset[str] = frozenset({"capture", "query"})
_VALID_CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})

INTENT_INSTRUCTIONS: str = """# Slack intent gate

Classify the user's Slack message into exactly ONE routing intent for their personal
knowledge-management assistant, then return ONLY a JSON object (no prose, no fence):

{"intent": "capture" | "query", "confidence": "high" | "medium" | "low",
 "keywords": ["dog", "Labradoodle", "pet"]}

- "capture": the user is recording something to FILE for later -- a note, idea, fact
  about their life/work, reminder, or TODO/action. Imperatives like "remind me to ...",
  "note that ...", "I need to ...", and bare declarative statements ("the wifi password
  is hunter2", "Jane's birthday is in March") are captures.
- "query": the user is asking to RETRIEVE something from their OWN vault/notes
  ("what are my open todos?", "what did I save about raft?", "show my notes on raft").

When the message is ambiguous between asking and filing, prefer "query" and lower the
confidence -- searching a misfiled note is harmless, but silently filing a real
question is not. Set "confidence" to how sure you are of the chosen intent.

"keywords" seed a lexical search of the user's vault, so give the most useful search
terms drawn from the message. De-pluralise to the singular ("dogs" -> "dog"); drop noise
and stop words ("list", "me", "the", "about", "what", "show"); and expand to obvious
synonyms or named entities the message implies ("my pup" -> also "dog", "pet"). Keep
them lowercase single words or short phrases. An empty list is fine when the message
carries no searchable terms (e.g. a pure reminder).
"""
"""The classifier's task prompt, appended (uncached) after the cached persona prefix."""


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """A routing verdict for one bare free-text Slack message.

    Callers route on :attr:`route` rather than the raw intent, so the
    low-confidence-to-query safety rule is applied in one place.
    """

    intent: str
    confidence: str
    keywords: tuple[str, ...] = ()

    @property
    def route(self) -> str:
        """The engine to route to, collapsing a low-confidence verdict to query.

        Searching a misfiled note is harmless, while silently filing a real question is
        the annoying failure (issue #5), so low confidence routes to query whatever the
        guessed intent was.
        """
        if self.confidence == "low":
            return "query"
        return self.intent


# The safe verdict when the gate cannot get a usable answer, routing to query as the
# least-privilege default
_DEFAULT_DECISION: IntentDecision = IntentDecision(intent="query", confidence="low")


@dataclass
class IntentClassifier:
    """A cheap, total intent gate over an injected LLM seam.

    Consulted only for a bare free-text message that hit none of the deterministic
    short-circuits. One small model call returns a tiny JSON verdict. The LLM is
    injectable, so a test substitutes a fake and needs no real SDK.
    """

    llm: LLM
    model: str = DEFAULT_INTENT_MODEL

    def classify(self, text: str) -> IntentDecision:
        """Returns the routing verdict for a message, and never raises.

        Sends the text as a single turn under the instructions and parses the JSON. Any
        failure returns the safe default rather than propagating, because the gate is a
        routing optimisation and the user's message must still be served.

        Args:
            text: The stripped free-text message body.

        Returns:
            The parsed verdict, or the safe query default on any failure.
        """
        try:
            response = self.llm.complete(
                [Message(role="user", content=text)],
                system_extra=INTENT_INSTRUCTIONS,
                max_tokens=_MAX_TOKENS,
                model=self.model,
            )
            obj = parse_json_block(extract_text(response))
        except Exception:  # noqa: BLE001 - the gate is total; fail safe to query
            decision = _DEFAULT_DECISION
        else:
            decision = _decision_from(obj)
        # Operator-readable line (issue #52) naming the verdict and the engine it
        # routes to, with a grep-friendly prefix, so a misroute is diagnosable
        logger.info(
            "intent routed: %s (confidence=%s) -> %s",
            decision.intent,
            decision.confidence,
            decision.route,
        )
        return decision


def _decision_from(obj: dict[str, object]) -> IntentDecision:
    """Builds a verdict from a parsed object, defaulting on bad shapes.

    An intent outside the two known engines is untrustworthy, so the whole verdict falls
    back to the safe default. An unknown confidence is treated as low, which also routes
    to query, so a model naming a valid intent but botching the confidence still routes
    conservatively.

    Keywords parse leniently (issue #102). A garbled value degrades to empty rather than
    rejecting the verdict, because the keywords only seed the search and the raw query
    is always the fallback.
    """
    intent = obj.get("intent")
    if intent not in _VALID_INTENTS:
        return _DEFAULT_DECISION
    confidence = obj.get("confidence")
    if confidence not in _VALID_CONFIDENCES:
        confidence = "low"
    return IntentDecision(
        intent=str(intent),
        confidence=str(confidence),
        keywords=_keywords_from(obj.get("keywords")),
    )


def _keywords_from(value: object) -> tuple[str, ...]:
    """Coerces a parsed keywords value into a clean tuple of search terms.

    Totality-preserving (issue #102). Only a genuine list of strings yields keywords,
    and any other shape degrades to empty, so a malformed field never raises and the
    caller falls back to grepping the raw query. Entries are stripped and empties
    dropped, while order and duplicates survive as the model emitted them, since the
    grep ranker dedupes downstream.
    """
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )
