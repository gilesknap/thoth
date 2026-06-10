"""The daily LLM spend guard: a persistent, fail-safe cost circuit-breaker (issue #16).

thoth runs unattended on pay-as-you-go Anthropic + Gemini keys with no spend ceiling.
Because Hindsight does **LLM fact-extraction** (SPEC section 8), every ingest *and*
every reindexed page is a model call, so a redelivery storm, a flapping dependency
retried to death, or an accidental ``reindex --full-rebuild`` of a large vault has
unbounded cost. This module is the concrete guard the SPEC's "budget-ready" Phase-3 goal
calls for: a small **daily call-count budget**, checked *before* each model call, that
**fails safe** (defers rather than spends) once the day's cap is reached and emits
**exactly one** notification via the errors-to-Slack surface (:mod:`thoth.alerts`).

The design mirrors the rest of the closed-surface appliance:

* **One combined daily budget.** Both the appliance's own Anthropic calls
  (:meth:`thoth.llm.LLM.complete`) and the Gemini extraction triggered through Hindsight
  ``retain`` (:meth:`thoth.hindsight.Hindsight.retain`, the only observable Gemini cost
  -- token usage is not) count against a single ``THOTH_DAILY_LLM_BUDGET`` ceiling. The
  two are tracked as **separate counters** (:data:`KIND_ANTHROPIC` /
  :data:`KIND_HINDSIGHT`) purely so the alert can report the split; the *check* is on
  their sum. A non-positive budget **disables** the guard (unlimited), the escape hatch
  for a box that wants no cap.
* **Persisted in the disposable state DB.** The per-day counters live in
  :attr:`thoth.config.Config.state_db_path` (the same gitignored, not-backed-up
  ``~/.thoth/state.db`` that backs :class:`thoth.state.EventStore` /
  :class:`~thoth.state.MarkerStore`), keyed by the **Europe/London** calendar day so the
  cap survives a daemon restart and resets at the London midnight the persona runs on.
  Losing the DB only resets today's count -- never a knowledge loss (the P1 guardrail).
* **Fail-safe, not fail-loud.** :meth:`BudgetGuard.charge` raises
  :class:`BudgetExceededError` *before* the spend when the cap is reached. The ingest
  pipeline already treats any classify/curate model failure as a *deferral* (the raw is
  held durably and re-curated by a later sweep, see :mod:`thoth.ingest`), so a budget
  trip there loses nothing; reindex aborts the rebuild cleanly mid-walk. A capture is
  deferred, never dropped.
* **Exactly one alert per day.** The first charge that trips the cap claims a per-day
  alert row (an atomic ``INSERT OR IGNORE``, the same test-and-set
  :class:`~thoth.state.EventStore` uses) so every *later* blocked call that day stays
  silent -- one notification, not one per blocked call.

Only the standard library, ``thoth._time``, :mod:`thoth.config`, and :mod:`thoth.alerts`
(themselves standard-library-only at import) are imported at module level, so importing
this module at pytest collection is always safe. The clock is injectable so the day
boundary and the alert timestamp are deterministic in tests without touching the wall
clock.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from thoth._time import LONDON, utc_now
from thoth.config import Config

__all__ = [
    "COUNTED_KINDS",
    "KIND_ANTHROPIC",
    "KIND_HINDSIGHT",
    "LONDON",
    "BudgetAlerterLike",
    "BudgetExceededError",
    "BudgetGuard",
    "BudgetGuardLike",
    "BudgetStore",
    "make_budget_guard",
]

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


class BudgetStore:
    """Durable, single-writer per-day call counters in the state DB (issue #16).

    Two tables share the disposable, not-backed-up state DB (the P1 guardrail, SPEC
    section 10): ``daily_budget(day, kind, count)`` holds one row per (London day, kind)
    counter, and ``budget_alerts(day, ts)`` records the single per-day "cap tripped"
    alert claim. Keying on the calendar-day string makes the reset implicit -- a new day
    simply has no rows yet -- and makes the counter survive a daemon restart (it is on
    disk, not in memory).

    Each call opens a short-lived connection in WAL mode with a bounded busy-timeout and
    closes it before returning (the same connection-per-operation pattern as
    :class:`thoth.state.EventStore`), so no handle outlives an operation and nothing can
    leak an ``unclosed database`` ``ResourceWarning`` (a hard error under ``-W error``).
    """

    _SCHEMA_BUDGET: str = (
        "CREATE TABLE IF NOT EXISTS daily_budget ("
        "day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL, "
        "PRIMARY KEY (day, kind))"
    )
    _SCHEMA_ALERTS: str = (
        "CREATE TABLE IF NOT EXISTS budget_alerts ("
        "day TEXT PRIMARY KEY, ts REAL NOT NULL)"
    )

    def __init__(self, db_path: Path) -> None:
        """Bind the store to the state DB at ``db_path`` (parent created on first use).

        Args:
            db_path: The SQLite file path (:attr:`thoth.config.Config.state_db_path` in
                production, a ``tmp_path`` location in tests). The same file backs
                :class:`thoth.state.EventStore` / :class:`~thoth.state.MarkerStore`; the
                tables coexist.
        """
        self._db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived connection (file, schema, pragmas), closed on exit."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(self._SCHEMA_BUDGET)
            conn.execute(self._SCHEMA_ALERTS)
            conn.commit()
            yield conn
        finally:
            conn.close()

    def total(self, day: str) -> int:
        """Return the combined call count recorded for ``day`` across every counter."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM daily_budget WHERE day = ?",
                (day,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def increment(self, day: str, kind: str, *, amount: int = 1) -> int:
        """Add ``amount`` to ``(day, kind)``'s counter and return its new value.

        Upserts the per-(day, kind) row so the first charge of a kind creates it and
        later charges accumulate. Returns the counter's value after the increment.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO daily_budget (day, kind, count) VALUES (?, ?, ?) "
                "ON CONFLICT (day, kind) DO UPDATE SET count = count + excluded.count",
                (day, kind, amount),
            )
            conn.commit()
            row = conn.execute(
                "SELECT count FROM daily_budget WHERE day = ? AND kind = ?",
                (day, kind),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def breakdown(self, day: str) -> dict[str, int]:
        """Return ``{kind: count}`` for ``day`` (only counters that were charged)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, count FROM daily_budget WHERE day = ?", (day,)
            ).fetchall()
        return {str(kind): int(count) for kind, count in rows if count is not None}

    def claim_alert(self, day: str, *, ts: float) -> bool:
        """Atomically claim the one-per-day "cap tripped" alert; report if newly won.

        Uses ``INSERT OR IGNORE`` on the ``day`` primary key as a test-and-set: the
        first caller of the day inserts its row and gets ``True`` (post the alert);
        every later caller is ignored and gets ``False`` (stay silent). This is what
        makes the notification fire exactly once per day even across many blocked calls
        and a daemon restart.

        Args:
            day: The London calendar-day key the cap was tripped on.
            ts: The wall-clock seconds to stamp the claim with.

        Returns:
            ``True`` if this call claimed the day's alert, ``False`` if already claimed.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO budget_alerts (day, ts) VALUES (?, ?)",
                (day, ts),
            )
            conn.commit()
            return cursor.rowcount == 1


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


def make_budget_guard(
    config: Config,
    *,
    alerter: BudgetAlerterLike | None = None,
    clock: Callable[[], datetime] | None = None,
    limit: int | None = None,
) -> BudgetGuard:
    """Build a :class:`BudgetGuard` over the deployment's state DB and configured cap.

    The cap defaults to :attr:`thoth.config.Config.daily_llm_budget`
    (``THOTH_DAILY_LLM_BUDGET``); a non-positive value yields a disabled guard. The same
    state DB backs every guard, so independently-constructed guards at the Slack / MCP /
    reindex entrypoints share one set of per-day counters (the DB is the coordination
    point) -- no single instance need be threaded through the graph.

    ``limit`` is a **transient per-run override** (issue #80): the ``thoth capture``
    backfill passes ``--budget N`` so a bulk import can raise (or, with ``0``, disable
    via the guard's ``limit <= 0`` rule) the cap for that one run without mutating the
    frozen :class:`~thoth.config.Config`. ``None`` (the default) preserves today's
    behaviour, so the Slack / MCP / reindex callers that pass nothing are unaffected.

    Args:
        config: The frozen runtime configuration (the budget + the state DB path).
        alerter: The optional errors-to-Slack seam for the one-per-day notification.
        clock: An injectable current-time source forwarded to the guard.
        limit: An optional transient override for the daily cap; ``None`` uses
            ``config.daily_llm_budget``. A non-positive value disables the guard.

    Returns:
        A wired :class:`BudgetGuard` (disabled when the effective budget is <= 0).
    """
    return BudgetGuard(
        store=BudgetStore(config.state_db_path),
        limit=config.daily_llm_budget if limit is None else limit,
        alerter=alerter,
        clock=clock,
    )
