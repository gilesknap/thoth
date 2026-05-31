"""The Slack free-text intent gate (issue #5): route bare prose to the right engine.

The Slack surface (:mod:`thoth.slack_app`) routes a message with a deterministic
``if/elif`` ladder -- a pending-save affirmative, a ``capture:``/``note:``/``save:``
prefix, a bare URL, a shared file -- and historically defaulted *everything else* to the
blended ask. That made plain prose like "remind me to call the dentist tomorrow" get
*answered* instead of *filed*; to capture a free thought you had to prefix it.

This module adds the one missing decision: for a message that hits **none** of those
deterministic short-circuits, ask a cheap model which engine the user meant. The gate
only *chooses* an engine -- it never blends them and never hands the read-only ask path
write access (least-privilege, SPEC sections 3 and 12). The deterministic prefixes stay
as the explicit escape hatch when the model guesses wrong.

Design constraints:

* **Slack-only.** MCP already exposes explicit tools (``pkm_ingest`` / ``pkm_ask`` /
  ``pkm_todos`` ...), so the calling agent does its own dispatch; no gate is needed
  there.
* **Total / fail-safe.** :meth:`IntentClassifier.classify` never raises: any model,
  network, or parse failure returns the safe default (route to ask). Silently filing a
  real question as a note is the annoying failure mode; *answering* a misfiled note is
  harmless, so the gate defaults to ask whenever it is unsure -- including on a
  low-confidence verdict (see :attr:`IntentDecision.route`).
* **Cheap.** One small model call (a Haiku, :data:`DEFAULT_INTENT_MODEL`) per bare
  free-text message only -- prefixed / URL / file messages skip the gate entirely. The
  call reuses the cached :data:`thoth.llm.PERSONA` prefix, so it is a small marginal
  cost on a busy appliance.

Only :mod:`thoth.llm` (the injectable model seam) is imported, so this module is
import-safe under pytest collection and never pulls in the ``anthropic`` SDK by itself.
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

# The three engines the gate can route a bare free-text message to.
_VALID_INTENTS: frozenset[str] = frozenset({"capture", "ask", "query"})
_VALID_CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})

INTENT_INSTRUCTIONS: str = """# Slack intent gate

Classify the user's Slack message into exactly ONE routing intent for their personal
knowledge-management assistant, then return ONLY a JSON object (no prose, no fence):

{"intent": "capture" | "ask" | "query", "confidence": "high" | "medium" | "low"}

- "capture": the user is recording something to FILE for later -- a note, idea, fact
  about their life/work, reminder, or TODO/action. Imperatives like "remind me to ...",
  "note that ...", "I need to ...", and bare declarative statements ("the wifi password
  is hunter2", "Jane's birthday is in March") are captures.
- "query": the user is asking to RETRIEVE something from their OWN vault/notes
  ("what are my open todos?", "what did I save about exa?", "show my notes on raft").
- "ask": the user is asking a question that may need outside/world knowledge or
  research ("what's the difference between raft and paxos?", "who won the 2022 final?").

When the message is ambiguous between asking and filing, prefer "ask" and lower the
confidence -- answering a misfiled note is harmless, but silently filing a real
question is not. Set "confidence" to how sure you are of the chosen intent.
"""
"""The classifier's task prompt, appended (uncached) after the cached persona prefix."""


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """A routing verdict for one bare free-text Slack message.

    ``intent`` is the model's best guess (one of ``capture`` / ``ask`` / ``query``) and
    ``confidence`` is ``high`` / ``medium`` / ``low``. Callers route on :attr:`route`,
    not :attr:`intent` directly, so the low-confidence-to-ask safety rule is applied in
    one place.
    """

    intent: str
    confidence: str

    @property
    def route(self) -> str:
        """The engine to route to, collapsing a low-confidence verdict to ``ask``.

        Answering a misfiled note is harmless; silently filing a real question as a
        note is the annoying failure (issue #5), so a ``low`` confidence -- whatever the
        guessed intent -- routes to the safe blended ask.
        """
        if self.confidence == "low":
            return "ask"
        return self.intent


# The safe verdict returned whenever the gate cannot get a usable answer: route to ask.
_DEFAULT_DECISION: IntentDecision = IntentDecision(intent="ask", confidence="low")


@dataclass
class IntentClassifier:
    """A cheap, total intent gate over an injected :class:`~thoth.llm.LLM` seam.

    Consulted by :class:`thoth.slack_app.Handlers` only for a bare free-text message
    that hit none of the deterministic short-circuits. :meth:`classify` makes one small
    model call (the :data:`DEFAULT_INTENT_MODEL` Haiku unless ``model`` overrides it)
    and parses a ``{"intent", "confidence"}`` object. The LLM is injectable, so a test
    substitutes a fake exposing ``.complete(...)`` with no real SDK.
    """

    llm: LLM
    model: str = DEFAULT_INTENT_MODEL

    def classify(self, text: str) -> IntentDecision:
        """Return the routing verdict for ``text`` -- never raises (fail-safe to ask).

        Sends ``text`` as a single user turn under :data:`INTENT_INSTRUCTIONS` and
        parses the model's JSON. Any failure -- a model/network error, missing or
        invalid JSON, or an out-of-range ``intent`` -- returns the safe
        :data:`_DEFAULT_DECISION` (route to ask) rather than propagating, because the
        gate is a routing optimisation and the user's message must still be served.

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
        except Exception:  # noqa: BLE001 - the gate is total; fail safe to ask
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

    An ``intent`` outside the three known engines is untrustworthy, so the whole verdict
    falls back to the safe default. A missing or unknown ``confidence`` is treated as
    ``low`` (which also routes to ask) rather than rejected, so a model that names a
    valid intent but botches the confidence still routes conservatively.
    """
    intent = obj.get("intent")
    if intent not in _VALID_INTENTS:
        return _DEFAULT_DECISION
    confidence = obj.get("confidence")
    if confidence not in _VALID_CONFIDENCES:
        confidence = "low"
    return IntentDecision(intent=str(intent), confidence=str(confidence))
