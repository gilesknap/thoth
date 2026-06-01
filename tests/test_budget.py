"""Tests for :mod:`thoth.budget` -- the daily LLM cost circuit-breaker (issue #16).

Covers the persistent counter store, the guard's block/defer/reset semantics, the
exactly-one-per-day notification, and the two model chokepoints (:class:`thoth.llm.LLM`
and :class:`thoth.hindsight.Hindsight`) charging against the shared cap. The clock is
always injected so the Europe/London day boundary and the alert are deterministic.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from thoth.budget import (
    KIND_ANTHROPIC,
    KIND_HINDSIGHT,
    BudgetExceededError,
    BudgetGuard,
    BudgetStore,
    make_budget_guard,
)
from thoth.config import load_config
from thoth.hindsight import Hindsight
from thoth.llm import LLM, Message

# ---- helpers ----------------------------------------------------------------------


class _Clock:
    """A mutable injected clock returning a tz-aware UTC datetime that tests advance."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _RecordingAlerter:
    """Captures :meth:`alert_budget_exceeded` calls (the guard's notification seam)."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_post: bool = False

    def alert_budget_exceeded(
        self, *, day: str, limit: int, breakdown: dict[str, int]
    ) -> bool:
        """Record the call, or blow up when ``raise_on_post`` (guard must tolerate)."""
        if self.raise_on_post:
            raise RuntimeError("slack down")
        self.calls.append({"day": day, "limit": limit, "breakdown": breakdown})
        return True


@dataclass
class _FakeMessages:
    """A ``.create`` recorder so the LLM chokepoint can be exercised without the SDK."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        """Record the call and return a trivial response object."""
        self.calls.append(kwargs)
        return {"content": [{"type": "text", "text": "ok"}]}


class _FakeClient:
    """Structural Anthropic stand-in exposing ``.messages.create``."""

    def __init__(self) -> None:
        self.messages = _FakeMessages()


@dataclass
class _RecordingRunner:
    """A Hindsight :class:`~thoth.hindsight.SubprocessRunner` that records argv."""

    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self, argv: Any, *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Record the call and return a clean completed process."""
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout="[]", stderr=""
        )


def _store(tmp_path: Path) -> BudgetStore:
    """A budget store over a throwaway state DB."""
    return BudgetStore(tmp_path / "state.db")


def _utc(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    """A tz-aware UTC datetime helper."""
    return datetime(year, month, day, hour, minute, tzinfo=_dt.UTC)


# ---- BudgetStore ------------------------------------------------------------------


def test_store_increment_total_and_breakdown(tmp_path: Path) -> None:
    """Per-(day, kind) counters accumulate; total sums across kinds for the day."""
    store = _store(tmp_path)
    assert store.total("2026-06-01") == 0
    assert store.increment("2026-06-01", KIND_ANTHROPIC) == 1
    assert store.increment("2026-06-01", KIND_ANTHROPIC) == 2
    assert store.increment("2026-06-01", KIND_HINDSIGHT) == 1
    assert store.total("2026-06-01") == 3
    assert store.breakdown("2026-06-01") == {KIND_ANTHROPIC: 2, KIND_HINDSIGHT: 1}
    # A different day is a clean slate (the implicit reset).
    assert store.total("2026-06-02") == 0


def test_store_counts_survive_a_fresh_instance(tmp_path: Path) -> None:
    """Counters are on disk, so a new store at the same path sees prior counts."""
    _store(tmp_path).increment("2026-06-01", KIND_ANTHROPIC, amount=5)
    assert _store(tmp_path).total("2026-06-01") == 5


def test_store_claim_alert_is_atomic_once_per_day(tmp_path: Path) -> None:
    """The first claim for a day wins; later claims (even after restart) are refused."""
    store = _store(tmp_path)
    assert store.claim_alert("2026-06-01", ts=1.0) is True
    assert store.claim_alert("2026-06-01", ts=2.0) is False
    # A fresh instance still sees the day as claimed (durable).
    assert _store(tmp_path).claim_alert("2026-06-01", ts=3.0) is False
    # A new day can be claimed once.
    assert store.claim_alert("2026-06-02", ts=4.0) is True


# ---- BudgetGuard: block / defer / count -------------------------------------------


def test_guard_allows_up_to_limit_then_blocks(tmp_path: Path) -> None:
    """Exactly ``limit`` calls are admitted; the next raises and is not counted."""
    clock = _Clock(_utc(2026, 6, 1))
    store = _store(tmp_path)
    guard = BudgetGuard(store=store, limit=3, alerter=None, clock=clock)
    for _ in range(3):
        guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)
    # The blocked call did not increment: the day holds exactly the limit.
    assert store.total(guard.today()) == 3


def test_guard_combines_anthropic_and_hindsight_against_one_cap(tmp_path: Path) -> None:
    """Both counters draw down the same combined budget."""
    clock = _Clock(_utc(2026, 6, 1))
    guard = BudgetGuard(store=_store(tmp_path), limit=2, clock=clock)
    guard.charge(KIND_ANTHROPIC)
    guard.charge(KIND_HINDSIGHT)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)


@pytest.mark.parametrize("limit", [0, -1])
def test_guard_disabled_when_budget_non_positive(tmp_path: Path, limit: int) -> None:
    """A non-positive budget makes charge a no-op (never blocks, never records)."""
    store = _store(tmp_path)
    guard = BudgetGuard(store=store, limit=limit, clock=_Clock(_utc(2026, 6, 1)))
    assert guard.enabled is False
    for _ in range(10):
        guard.charge(KIND_ANTHROPIC)  # no raise
    assert store.total(guard.today()) == 0


def test_guard_resets_on_the_london_day_boundary(tmp_path: Path) -> None:
    """Advancing into the next London day gives a fresh budget."""
    clock = _Clock(_utc(2026, 6, 1))
    guard = BudgetGuard(store=_store(tmp_path), limit=1, clock=clock)
    guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)
    # Next calendar day in Europe/London: the counter starts over.
    clock.now = _utc(2026, 6, 2)
    guard.charge(KIND_ANTHROPIC)  # no raise


def test_guard_today_uses_europe_london_not_utc(tmp_path: Path) -> None:
    """The day key is the London calendar day (BST rollover crosses before UTC)."""
    # 23:30 UTC on 1 June is 00:30 on 2 June in BST (UTC+1).
    guard = BudgetGuard(
        store=_store(tmp_path), limit=1, clock=_Clock(_utc(2026, 6, 1, 23, 30))
    )
    assert guard.today() == "2026-06-02"
    # Mid-winter (GMT == UTC): no shift.
    winter = BudgetGuard(
        store=_store(tmp_path), limit=1, clock=_Clock(_utc(2026, 1, 1, 0, 30))
    )
    assert winter.today() == "2026-01-01"


# ---- BudgetGuard: the one-per-day notification ------------------------------------


def test_guard_alerts_exactly_once_per_day(tmp_path: Path) -> None:
    """Every blocked call routes through the alert path, but only the first posts."""
    clock = _Clock(_utc(2026, 6, 1))
    alerter = _RecordingAlerter()
    guard = BudgetGuard(store=_store(tmp_path), limit=1, alerter=alerter, clock=clock)
    guard.charge(KIND_ANTHROPIC)
    for _ in range(3):
        with pytest.raises(BudgetExceededError):
            guard.charge(KIND_ANTHROPIC)
    assert len(alerter.calls) == 1
    assert alerter.calls[0]["day"] == "2026-06-01"
    assert alerter.calls[0]["limit"] == 1
    assert alerter.calls[0]["breakdown"] == {KIND_ANTHROPIC: 1}
    # A new day re-arms the single alert.
    clock.now = _utc(2026, 6, 2)
    guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)
    assert len(alerter.calls) == 2
    assert alerter.calls[1]["day"] == "2026-06-02"


def test_guard_blocks_even_with_no_alerter(tmp_path: Path) -> None:
    """A guard with no alerter still enforces the cap (the MCP/no-Slack case)."""
    guard = BudgetGuard(store=_store(tmp_path), limit=1, clock=_Clock(_utc(2026, 6, 1)))
    guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)


def test_guard_alert_failure_does_not_mask_the_block(tmp_path: Path) -> None:
    """A throwing alerter is swallowed: the budget error still reaches the caller."""
    alerter = _RecordingAlerter(raise_on_post=True)
    guard = BudgetGuard(
        store=_store(tmp_path), limit=1, alerter=alerter, clock=_Clock(_utc(2026, 6, 1))
    )
    guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)


# ---- make_budget_guard ------------------------------------------------------------


def test_make_budget_guard_reads_config_budget(tmp_path: Path) -> None:
    """The factory wires the configured cap and the deployment state DB."""
    config = load_config(
        {
            "PKM_VAULT": str(tmp_path),
            "THOTH_HOME": str(tmp_path / "home"),
            "THOTH_DAILY_LLM_BUDGET": "1",
        }
    )
    guard = make_budget_guard(config, clock=_Clock(_utc(2026, 6, 1)))
    guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        guard.charge(KIND_ANTHROPIC)


def test_make_budget_guard_disabled_for_zero_budget(tmp_path: Path) -> None:
    """A zero budget yields a disabled guard."""
    config = load_config(
        {
            "PKM_VAULT": str(tmp_path),
            "THOTH_HOME": str(tmp_path / "home"),
            "THOTH_DAILY_LLM_BUDGET": "0",
        }
    )
    assert make_budget_guard(config).enabled is False


def test_make_budget_guard_limit_override_takes_precedence(tmp_path: Path) -> None:
    """A transient ``limit`` override (thoth capture --budget, #80) beats the config."""
    config = load_config(
        {
            "PKM_VAULT": str(tmp_path),
            "THOTH_HOME": str(tmp_path / "home"),
            "THOTH_DAILY_LLM_BUDGET": "1",
        }
    )
    # limit=None keeps the config cap of 1 (one charge then block).
    default_guard = make_budget_guard(config, clock=_Clock(_utc(2026, 6, 1)))
    default_guard.charge(KIND_ANTHROPIC)
    with pytest.raises(BudgetExceededError):
        default_guard.charge(KIND_ANTHROPIC)
    # A positive override raises the cap for this run; 0 disables the guard entirely.
    assert make_budget_guard(config, limit=50).enabled is True
    assert make_budget_guard(config, limit=0).enabled is False


# ---- chokepoint integration: LLM.complete -----------------------------------------


def test_llm_complete_charges_and_blocks(tmp_path: Path) -> None:
    """LLM.complete spends one Anthropic charge per call and blocks at the cap."""
    config = load_config({"PKM_VAULT": str(tmp_path)})
    client = _FakeClient()
    guard = BudgetGuard(store=_store(tmp_path), limit=1, clock=_Clock(_utc(2026, 6, 1)))
    llm = LLM(config, client=client, guard=guard)
    msgs = [Message(role="user", content="hi")]
    llm.complete(msgs)
    assert len(client.messages.calls) == 1
    # Cap reached: the next call is blocked *before* the client is touched.
    with pytest.raises(BudgetExceededError):
        llm.complete(msgs)
    assert len(client.messages.calls) == 1


def test_llm_complete_without_guard_never_charges(tmp_path: Path) -> None:
    """The guard is opt-in: an LLM with no guard calls the client every time."""
    config = load_config({"PKM_VAULT": str(tmp_path)})
    client = _FakeClient()
    llm = LLM(config, client=client)
    for _ in range(5):
        llm.complete([Message(role="user", content="hi")])
    assert len(client.messages.calls) == 5


# ---- chokepoint integration: Hindsight.retain -------------------------------------


def test_hindsight_retain_charges_and_blocks(tmp_path: Path) -> None:
    """retain spends one Hindsight charge per call and blocks at the cap."""
    config = load_config({"PKM_VAULT": str(tmp_path)})
    runner = _RecordingRunner()
    guard = BudgetGuard(store=_store(tmp_path), limit=1, clock=_Clock(_utc(2026, 6, 1)))
    hs = Hindsight(config, runner=runner, guard=guard)
    hs.retain("entities/a.md", "a fact")
    assert len(runner.calls) == 1
    with pytest.raises(BudgetExceededError):
        hs.retain("entities/b.md", "another fact")
    # Blocked before spawning the CLI.
    assert len(runner.calls) == 1


def test_hindsight_recall_does_not_charge(tmp_path: Path) -> None:
    """Only retain (Gemini extraction) is metered; recall (embedding-only) is free."""
    config = load_config({"PKM_VAULT": str(tmp_path)})
    runner = _RecordingRunner()
    store = _store(tmp_path)
    guard = BudgetGuard(store=store, limit=1, clock=_Clock(_utc(2026, 6, 1)))
    hs = Hindsight(config, runner=runner, guard=guard)
    hs.recall("anything")
    hs.recall("more")
    assert store.total(guard.today()) == 0
