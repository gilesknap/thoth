"""The daily LLM spend guard: a persistent, fail-safe cost circuit-breaker (issue #16).

thoth runs unattended on pay-as-you-go Anthropic and Gemini keys with no spend ceiling,
and Hindsight does **LLM fact-extraction** (SPEC section 8), so every ingest *and* every
reindexed page is a model call. A redelivery storm, a flapping dependency retried to
death, or an accidental ``reindex --full-rebuild`` of a large vault therefore costs
without bound. This package is the guard the SPEC's "budget-ready" Phase-3 goal calls
for: a small **daily call-count budget**, checked *before* each model call, that **fails
safe** and defers rather than spends once the cap is reached, emitting **exactly one**
notification through the errors-to-Slack surface (:mod:`thoth.alerts`).

The design mirrors the rest of the closed-surface appliance:

* **One combined daily budget.** The appliance's own Anthropic calls
  (:meth:`thoth.llm.LLM.complete`) and the Gemini extraction a Hindsight ``retain``
  triggers (:meth:`thoth.hindsight.Hindsight.retain`, the only observable Gemini cost,
  as token usage is not) both count against one ``THOTH_DAILY_LLM_BUDGET`` ceiling. The
  **separate counters** :data:`KIND_ANTHROPIC` and :data:`KIND_HINDSIGHT` exist purely
  so the alert can report the split, and the *check* is on their sum. A non-positive
  budget **disables** the guard, the escape hatch for a box that wants no cap.
* **Persisted in the disposable state DB.** The per-day counters live in
  :attr:`thoth.config.Config.state_db_path`, the gitignored, not-backed-up
  ``~/.thoth/state.db`` that also backs :class:`thoth.state.EventStore` and
  :class:`~thoth.state.MarkerStore`. Keying on the **Europe/London** calendar day makes
  the cap survive a daemon restart and reset at the London midnight the persona runs on,
  and losing the DB resets today's count and nothing more, never knowledge (the P1
  guardrail).
* **Fail-safe, not fail-loud.** :meth:`BudgetGuard.charge` raises
  :class:`BudgetExceededError` *before* the spend. The ingest pipeline already treats a
  classify or curate failure as a *deferral*, holding the raw durably for a later sweep
  to re-curate (see :mod:`thoth.ingest`), so a trip there loses nothing and reindex
  aborts the rebuild cleanly mid-walk. A capture is deferred, never dropped.
* **Exactly one alert per day.** The first charge to trip the cap claims a per-day alert
  row with an atomic ``INSERT OR IGNORE``, the test-and-set
  :class:`~thoth.state.EventStore` also uses, and every *later* blocked call stays
  silent.

Module level imports only the standard library, ``thoth._time``, :mod:`thoth.state` and
:mod:`thoth.config`, the last two themselves standard-library-only, so importing this
package at pytest collection is always safe. The clock is injectable, so the day
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
    """Build a :class:`BudgetGuard` over the deployment's state DB and configured cap.

    The cap defaults to :attr:`thoth.config.Config.daily_llm_budget`, set by
    ``THOTH_DAILY_LLM_BUDGET``. A non-positive value yields a disabled guard. One state
    DB backs every guard, so guards built separately at the Slack, MCP and reindex
    entrypoints share one set of per-day counters. The DB is the coordination point, so
    no single instance need be threaded through the graph.

    ``limit`` is a **transient per-run override** (issue #80). The ``thoth capture``
    backfill passes ``--budget N`` so a bulk import can raise the cap for that one run,
    or disable it with ``0`` through the guard's ``limit <= 0`` rule, without mutating
    the frozen :class:`~thoth.config.Config`. ``None``, the default, leaves the Slack,
    MCP and reindex callers unaffected.

    Args:
        config: The frozen runtime configuration: the budget and the state DB path.
        alerter: The optional errors-to-Slack seam for the one-per-day notification.
        clock: An injectable current-time source forwarded to the guard.
        limit: An optional transient override for the daily cap. ``None`` uses
            ``config.daily_llm_budget``. A non-positive value disables the guard.

    Returns:
        A wired :class:`BudgetGuard`, disabled when the effective budget is <= 0.
    """
    return BudgetGuard(
        store=BudgetStore(config.state_db_path),
        limit=config.daily_llm_budget if limit is None else limit,
        alerter=alerter,
        clock=clock,
    )
