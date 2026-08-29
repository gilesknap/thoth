"""The Slack free-text intent gate (issue #5): route bare prose to the right engine.

The Slack surface :mod:`thoth.slack_app` routes a message with a deterministic
``if/elif`` ladder, matching a pending-save affirmative, a ``capture:``, ``note:`` or
``save:`` prefix, a bare URL or a shared file, and historically defaulted *everything
else* to a query. Plain prose like "remind me to call the dentist tomorrow" was
therefore *searched* instead of *filed*, and capturing a free thought needed a prefix.

This module adds the one missing decision. For a message that hits **none** of those
deterministic short-circuits, it asks a cheap model which engine the user meant. The
gate only *chooses* an engine, never blending them and never handing the read-only
query path write access, per least-privilege (SPEC sections 3 and 12). The
deterministic prefixes stay as the explicit escape hatch when the model guesses
wrong.

Design constraints:

* **Slack-only.** MCP already exposes explicit tools such as ``pkm_ingest``,
  ``pkm_search`` and ``pkm_todos``, so the calling agent does its own dispatch and
  needs no gate.
* **Total and fail-safe.** :meth:`IntentClassifier.classify` never raises, and any
  model, network or parse failure routes to query, as :attr:`IntentDecision.route`
  documents.
* **Cheap.** One small model call, a Haiku from :data:`DEFAULT_INTENT_MODEL`, per bare
  free-text message only, since a prefixed, URL or file message skips the gate
  entirely. The call reuses the cached :data:`thoth.llm.PERSONA` prefix, so it is a
  small marginal cost on a busy appliance.

This module imports only :mod:`thoth.llm`, the injectable model seam, so it is
import-safe under pytest collection and never pulls in the ``anthropic`` SDK itself.
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

# The two engines the gate can route a bare free-text message to.
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

    ``intent`` is the model's best guess, ``capture`` or ``query``, and ``confidence``
    is ``high``, ``medium`` or ``low``. A caller routes on :attr:`route` rather than
    :attr:`intent` directly, so one place applies the low-confidence-to-query safety
    rule. ``keywords`` are the de-pluralised, stop-word-stripped, synonym-expanded
    search terms the gate extracted from the message (issue #102). They seed the lexical
    grep on the query read path, and are empty when the model gave none.
    """

    intent: str
    confidence: str
    keywords: tuple[str, ...] = ()

    @property
    def route(self) -> str:
        """The engine to route to, collapsing a low-confidence verdict to ``query``.

        Searching a misfiled note is harmless, while silently filing a real question as
        a note is the annoying failure (issue #5), so a ``low`` confidence routes to the
        safe query whatever the guessed intent.
        """
        if self.confidence == "low":
            return "query"
        return self.intent


# The safe verdict returned whenever the gate cannot get a usable answer: route to query
# (least-privilege default).
_DEFAULT_DECISION: IntentDecision = IntentDecision(intent="query", confidence="low")


@dataclass
class IntentClassifier:
    """A cheap, total intent gate over an injected :class:`~thoth.llm.LLM` seam.

    :class:`thoth.slack_app.Handlers` consults it only for a bare free-text message that
    hit none of the deterministic short-circuits. :meth:`classify` makes one small model
    call, the :data:`DEFAULT_INTENT_MODEL` Haiku unless ``model`` overrides it, and
    parses a ``{"intent", "confidence"}`` object. The LLM is injectable, so a test
    substitutes a fake exposing ``.complete(...)`` with no real SDK.
    """

    llm: LLM
    model: str = DEFAULT_INTENT_MODEL

    def classify(self, text: str) -> IntentDecision:
        """Return the routing verdict for ``text``, never raising and safe to query.

        Sends ``text`` as a single user turn under :data:`INTENT_INSTRUCTIONS` and
        parses the model's JSON. Any failure returns the safe :data:`_DEFAULT_DECISION`,
        routing to query, rather than propagates, whether a model or network error,
        missing or invalid JSON, or an out-of-range ``intent``. The gate is a routing
        optimisation, and the user's message must still be served.

        Args:
            text: The stripped free-text message body.

        Returns:
            The parsed :class:`IntentDecision`, or the safe default on any failure.
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
        # Concise operator-readable line (issue #52): the gate's verdict and the engine
        # it routes to, so a misroute is diagnosable from the log. Grep-friendly prefix.
        logger.info(
            "intent routed: %s (confidence=%s) -> %s",
            decision.intent,
            decision.confidence,
            decision.route,
        )
        return decision


def _decision_from(obj: dict[str, object]) -> IntentDecision:
    """Build an :class:`IntentDecision` from a parsed object, defaulting on bad shapes.

    An ``intent`` outside the two known engines is untrustworthy, so the whole verdict
    falls back to the safe default. A missing or unknown ``confidence`` counts as
    ``low``, which also routes to query, rather than being rejected, so a model that
    names a valid intent but botches the confidence still routes conservatively.
    ``keywords`` is parsed leniently (issue #102): a missing, non-list or garbled value
    degrades to an empty tuple rather than rejects the verdict, because the keywords
    only *seed* the lexical search and the raw query is always the fallback.
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
    """Coerce a parsed ``keywords`` value into a clean tuple of search terms.

    Totality-preserving (issue #102). Only a genuine list of strings yields keywords,
    and any other shape, whether missing, a bare string, a number or nested junk,
    degrades to ``()``, so a malformed field never raises and the caller falls back to
    grepping the raw query. Each entry is stripped, empties are dropped, and order and
    duplicates survive as the model emitted them, since the grep ranker dedupes
    downstream.
    """
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )
