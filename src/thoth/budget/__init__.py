"""The daily LLM spend guard, a persistent fail-safe circuit-breaker (issue #16).

thoth runs unattended on pay-as-you-go keys with no spend ceiling, and because the index
does LLM fact-extraction (SPEC section 8) every ingest and every reindexed page is a
model call. So a redelivery storm, a flapping dependency retried to death, or an
accidental full rebuild of a large vault has unbounded cost. This is a small daily
call-count budget, checked before each call, that fails safe by deferring rather than
spending once the cap is reached, and emits exactly one notification through the
errors-to-Slack surface. Four things shape the design:

* **One combined budget.** The appliance's own calls and the extraction behind Hindsight
  retain both count against one ceiling. They are tracked as separate counters purely so
  the alert can report the split, and the check is on their sum. A non-positive budget
  disables the guard, the escape hatch for a box that wants no cap.
* **Persisted in the disposable state DB.** The counters are keyed by the Europe/London
  day, so the cap survives a restart and resets at the midnight the persona runs on.
  Losing the DB only resets today's count, never knowledge.
* **Fail-safe, not fail-loud.** The charge raises before the spend, and the ingest
  pipeline already treats a model failure as a deferral, so a trip there loses nothing
  while reindex aborts cleanly mid-walk. A capture is deferred, never dropped.
* **Exactly one alert per day.** The first charge to trip the cap claims a per-day row
  atomically, so every later blocked call stays silent.

Only the standard library and the stdlib-only thoth modules are imported here, so
importing this at pytest collection is always safe. The clock is injectable, so the day
boundary and the alert timestamp are deterministic in tests without the wall clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from thoth._time import LONDON
from thoth.config import Config

from .guard import (
    COUNTED_KINDS,
    KIND_ANTHROPIC,
    KIND_HINDSIGHT,
    BudgetAlerterLike,
    BudgetExceededError,
    BudgetGuard,
    BudgetGuardLike,
)
from .store import BudgetStore

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


def make_budget_guard(
    config: Config,
    *,
    alerter: BudgetAlerterLike | None = None,
    clock: Callable[[], datetime] | None = None,
    limit: int | None = None,
) -> BudgetGuard:
    """Builds a guard over the deployment's state DB and configured cap.

    The same state DB backs every guard, so guards built independently at the Slack, MCP
    and reindex entrypoints share one set of per-day counters. The DB is the
    coordination point, so no single instance need be threaded through the graph.

    Args:
        config: Frozen runtime config, supplying the budget and state DB path.
        alerter: Optional errors-to-Slack seam for the one-per-day notification.
        clock: Injectable current-time source forwarded to the guard.
        limit: Transient per-run override (issue #80), letting a bulk import raise or
            disable the cap for one run without mutating the frozen config. None uses
            the configured budget, and a non-positive value disables the guard.

    Returns:
        The wired guard, disabled when the effective budget is not positive.
    """
    return BudgetGuard(
        store=BudgetStore(config.state_db_path),
        limit=config.daily_llm_budget if limit is None else limit,
        alerter=alerter,
        clock=clock,
    )
