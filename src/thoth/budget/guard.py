"""The daily call-count circuit-breaker and its notification seams (issue #16)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from thoth._time import LONDON, utc_now

from .store import BudgetStore

_LOG = logging.getLogger("thoth.budget")

KIND_ANTHROPIC: str = "anthropic"
"""Counter name for the appliance's own Anthropic ``messages.create`` calls."""

KIND_HINDSIGHT: str = "hindsight"
"""Counter name for Gemini fact-extraction triggered via a Hindsight ``retain``."""

COUNTED_KINDS: tuple[str, ...] = (KIND_ANTHROPIC, KIND_HINDSIGHT)
"""The counters that contribute to the combined daily budget (display order)."""


class BudgetAlerterLike(Protocol):
    """The one-method slice of the alerter the guard posts through.

    Typing the seam structurally rather than concretely lets a test inject a small
    recorder without building a real alerter, and the real one satisfies it.
    """

    def alert_budget_exceeded(
        self, *, day: str, limit: int, breakdown: dict[str, int]
    ) -> bool:
        """Posts the one-per-day cap-reached alert and reports whether it landed."""
        ...


class BudgetGuardLike(Protocol):
    """The one-method slice of the guard the model chokepoints depend on.

    Both chokepoints take an optional guard typed by this protocol and charge it before
    spending, so a test can inject a small fake, or None to disable, without building a
    real store.
    """

    def charge(self, kind: str) -> None:
        """Accounts one call of a kind, raising when it would exceed the cap."""
        ...


class BudgetExceededError(Exception):
    """Raised when the day's combined budget is spent.

    It raises before the model call, so nothing is spent. Both chokepoints are
    positioned so this surfaces as a deferral rather than a lost capture: the ingest
    passes report deferred curation, and the retain path leaves the already-durable page
    on disk for the next reindex.
    """


class BudgetGuard:
    """The daily call-count circuit-breaker checked before every model call (#16).

    :meth:`charge` is the single entry point. Each chokepoint calls it with its counter
    name before spending, and it raises once the day's combined count reaches the limit.
    A non-positive limit disables the guard entirely, so a deployment can opt out of the
    cap without removing the wiring.
    """

    def __init__(
        self,
        *,
        store: BudgetStore,
        limit: int,
        alerter: BudgetAlerterLike | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Stores the counter backend, the cap, the alert seam, and the clock.

        Args:
            store: Holds the per-day counters and the alert claim.
            limit: The combined daily call budget. A non-positive value disables it.
            alerter: Errors-to-Slack seam for the one-per-day notification, or None,
                in which case the cap still blocks but silently. The MCP server has
                no Slack target.
            clock: Current-time source, defaulting to UTC now. It derives the London
                day key and stamps the alert claim.
        """
        self._store = store
        self._limit = limit
        self._alerter = alerter
        self._clock = clock if clock is not None else utc_now

    @property
    def enabled(self) -> bool:
        """True when a positive budget is configured, so the guard will block."""
        return self._limit > 0

    def today(self) -> str:
        """Returns the current Europe/London calendar day."""
        return self._clock().astimezone(LONDON).date().isoformat()

    def charge(self, kind: str) -> None:
        """Accounts one call against today's budget, raising when the cap is hit.

        The check runs before the increment, so the call that would exceed the cap is
        blocked and not counted. Reaching the cap fires the one-per-day alert
        best-effort and raises. Every attempt counts, so a retried flapping dependency
        cannot burn past the cap.

        The read and the increment are separate store calls, so two guards racing on the
        last unit can both pass the check. The day therefore admits the limit and not a
        great deal more, which suits a single-writer daemon.

        Args:
            kind: The counter to charge.

        Raises:
            BudgetExceededError: when today's combined count has reached the budget.
        """
        if self._limit <= 0:
            return
        day = self.today()
        spent = self._store.total(day)
        if spent >= self._limit:
            _LOG.debug(
                "budget guard BLOCKED %s: spend=%d/%d for %s",
                kind,
                spent,
                self._limit,
                day,
            )
            self._maybe_alert(day)
            raise BudgetExceededError(
                f"daily LLM budget of {self._limit} call(s) reached for {day} "
                f"(Europe/London); work is deferred until the next day"
            )
        self._store.increment(day, kind)
        _LOG.debug(
            "budget guard allowed %s: spend=%d/%d for %s",
            kind,
            spent + 1,
            self._limit,
            day,
        )

    def _maybe_alert(self, day: str) -> None:
        """Posts the cap-tripped alert at most once per day, best-effort.

        The atomic per-day claim guarantees a single notification even though every
        blocked call routes through here.
        """
        if self._alerter is None:
            return
        try:
            if self._store.claim_alert(day, ts=self._clock().timestamp()):
                self._alerter.alert_budget_exceeded(
                    day=day, limit=self._limit, breakdown=self._store.breakdown(day)
                )
        except Exception:  # noqa: BLE001 - the alert path must never mask the block
            _LOG.exception("failed to emit the daily-budget alert for %s", day)
