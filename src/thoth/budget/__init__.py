"""The daily LLM spend guard: a persistent, fail-safe cost circuit-breaker (issue #16).

thoth runs unattended on pay-as-you-go Anthropic + Gemini keys with no spend ceiling.
Because Hindsight does **LLM fact-extraction** (SPEC section 8), every ingest *and*
every reindexed page is a model call, so a redelivery storm, a flapping dependency
retried to death, or an accidental ``reindex --full-rebuild`` of a large vault has
unbounded cost. This package is the concrete guard the SPEC's "budget-ready" Phase-3
goal calls for: a small **daily call-count budget**, checked *before* each model call,
that **fails safe** (defers rather than spends) once the day's cap is reached and emits
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

Only the standard library, ``thoth._time``, :mod:`thoth.state`, and :mod:`thoth.config`
(themselves standard-library-only at import) are imported at module level, so importing
this package at pytest collection is always safe. The clock is injectable so the day
boundary and the alert timestamp are deterministic in tests without touching the wall
clock.
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
