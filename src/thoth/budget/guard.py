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
    """The one-method slice of :class:`thoth.alerts.Alerter` the guard posts through.

    Typing the guard's notification seam structurally (rather than the concrete
    :class:`~thoth.alerts.Alerter`) lets a test inject a tiny recorder without building
    a real alerter; the real :class:`~thoth.alerts.Alerter` satisfies it.
    """

    def alert_budget_exceeded(
        self, *, day: str, limit: int, breakdown: dict[str, int]
    ) -> bool:
        """Post the one-per-day cap-reached alert; return whether it was delivered."""
        ...


class BudgetGuardLike(Protocol):
    """The one-method slice of the budget guard the model chokepoints depend on.

    :meth:`thoth.llm.LLM.complete` and :meth:`thoth.hindsight.Hindsight.retain` take an
    optional guard typed by this Protocol and call :meth:`charge` before spending, so a
    test can inject a tiny fake (or ``None`` to disable) without building a real
    :class:`BudgetStore`. :class:`BudgetGuard` satisfies it.
    """

    def charge(self, kind: str) -> None:
        """Account one ``kind`` call; raise :class:`BudgetExceededError` if over cap."""
        ...


class BudgetExceededError(Exception):
    """Raised by :meth:`BudgetGuard.charge` when the day's combined budget is spent.

    It is raised *before* the model call, so nothing is spent. Both model chokepoints
    are positioned so this surfaces as a *deferral*, never a lost capture: in
    :meth:`thoth.llm.LLM.complete` it is caught by the ingest classify/curate passes and
    reported as deferred curation; in :meth:`thoth.hindsight.Hindsight.retain` the
    already-durable page is left on disk for the next reindex.
    """


class BudgetGuard:
    """The daily call-count circuit-breaker checked before every model call (issue #16).

    Construct it with a :class:`BudgetStore`, the combined daily ``limit``, an optional
    :class:`thoth.alerts.Alerter` (the one-per-day notification seam), and an injectable
    clock. :meth:`charge` is the single entry point: each model chokepoint calls it with
    its counter name *before* spending, and it raises :class:`BudgetExceededError` once
    the day's combined count has reached ``limit``.

    A non-positive ``limit`` disables the guard entirely (``charge`` becomes a no-op),
    so a deployment can opt out of the cap without removing the wiring, and so existing
    callers that pass no guard are unaffected.
    """

    def __init__(
        self,
        *,
        store: BudgetStore,
        limit: int,
        alerter: BudgetAlerterLike | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Store the counter backend, the cap, the alert seam, and the clock.

        Args:
            store: The :class:`BudgetStore` holding the per-day counters + alert claim.
            limit: The combined daily call budget; ``<= 0`` disables the guard.
            alerter: The errors-to-Slack seam for the one-per-day cap notification, or
                ``None`` (the cap still blocks, just silently -- e.g. the MCP server,
                which has no Slack target).
            clock: A source of the current tz-aware :class:`~datetime.datetime`;
                defaults to :func:`datetime.now` in UTC. Used to derive the London day
                key and to stamp the alert claim.
        """
        self._store = store
        self._limit = limit
        self._alerter = alerter
        self._clock = clock if clock is not None else utc_now

    @property
    def enabled(self) -> bool:
        """``True`` iff a positive budget is configured (the guard will block)."""
        return self._limit > 0

    def today(self) -> str:
        """Return the current Europe/London calendar day as ``YYYY-MM-DD``."""
        return self._clock().astimezone(LONDON).date().isoformat()

    def charge(self, kind: str) -> None:
        """Account one ``kind`` call against today's budget; raise if the cap is hit.

        Checks *before* incrementing so the call that would exceed the cap is blocked
        and **not** counted (the day admits exactly ``limit`` calls). On reaching the
        cap it fires the one-per-day alert (best-effort) and raises
        :class:`BudgetExceededError`; otherwise it records the call and returns. Every
        attempt counts, so a retried flapping dependency cannot burn past the cap
        (issue #16 pairs with the #11 retry).

        Args:
            kind: The counter to charge (:data:`KIND_ANTHROPIC` /
                :data:`KIND_HINDSIGHT`).

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
        """Post the cap-tripped alert at most once per day (best-effort).

        The atomic per-day claim in :meth:`BudgetStore.claim_alert` guarantees a single
        notification even though every blocked call routes through here.
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
