"""Tests for :mod:`thoth.state` -- the transient ``state.db`` SQLite store.

These exercise the durable ``processed_events`` dedupe table with a ``tmp_path`` state
DB and an injected clock, so the TTL pruning is deterministic and no real ``~/.thoth``
or network is touched. ``sqlite3`` is stdlib, so the module imports safely under
collection. The restart case is simulated by constructing a *fresh* :class:`EventStore`
over the same file (a new process would do exactly this), proving the row survives.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from thoth.state import EventStore

TTL = 3600.0


def _store(
    tmp_path: Path, clock: Callable[[], float] | None = None
) -> EventStore:
    """Build an EventStore at a tmp state.db with an optional injected clock."""
    db = tmp_path / "state.db"
    return EventStore(db, clock=clock)


def test_seen_first_unseen_then_seen(tmp_path: Path) -> None:
    """The first seen() is unseen (recorded); a second with the same id is seen."""
    store = _store(tmp_path, clock=lambda: 0.0)
    try:
        assert store.seen("E1", ttl_seconds=TTL) is False
        assert store.seen("E1", ttl_seconds=TTL) is True
    finally:
        store.close()


def test_seen_empty_id_never_recorded(tmp_path: Path) -> None:
    """An empty event id is always unseen and never recorded (cannot dedupe)."""
    store = _store(tmp_path, clock=lambda: 0.0)
    try:
        assert store.seen("", ttl_seconds=TTL) is False
        assert store.seen("", ttl_seconds=TTL) is False
    finally:
        store.close()


def test_seen_survives_a_simulated_restart(tmp_path: Path) -> None:
    """An id recorded before a restart is recognised by a fresh store over the same DB.

    This is the durability acceptance for the redelivery dedupe: closing the store and
    re-opening a brand-new :class:`EventStore` at the same ``state.db`` (what a
    restarted daemon does) still reports the prior event as already-processed.
    """
    db = tmp_path / "state.db"
    first = EventStore(db, clock=lambda: 10.0)
    try:
        assert first.seen("E-restart", ttl_seconds=TTL) is False
    finally:
        first.close()

    # A fresh store over the same file == a restarted process.
    second = EventStore(db, clock=lambda: 20.0)
    try:
        assert second.seen("E-restart", ttl_seconds=TTL) is True
    finally:
        second.close()


def test_prune_drops_expired_with_injected_clock(tmp_path: Path) -> None:
    """An id older than the TTL is pruned and so is unseen (and re-recorded) again."""
    now = {"t": 100.0}
    store = _store(tmp_path, clock=lambda: now["t"])
    try:
        assert store.seen("E1", ttl_seconds=10.0) is False
        now["t"] = 105.0  # within TTL
        assert store.seen("E1", ttl_seconds=10.0) is True
        now["t"] = 200.0  # past TTL -> pruned -> unseen again
        assert store.seen("E1", ttl_seconds=10.0) is False
    finally:
        store.close()


def test_prune_returns_deleted_count(tmp_path: Path) -> None:
    """prune() removes only expired rows and returns how many it deleted."""
    now = {"t": 0.0}
    store = _store(tmp_path, clock=lambda: now["t"])
    try:
        store.seen("old", ttl_seconds=1000.0)
        now["t"] = 500.0
        store.seen("new", ttl_seconds=1000.0)
        now["t"] = 1000.0  # 'old' (ts=0) is now >TTL=600 old; 'new' (ts=500) is not.
        assert store.prune(ttl_seconds=600.0) == 1
        # 'new' is still present, 'old' is gone.
        assert store.seen("new", ttl_seconds=600.0) is True
        assert store.seen("old", ttl_seconds=600.0) is False
    finally:
        store.close()


def test_mark_records_without_seen(tmp_path: Path) -> None:
    """mark() records an id so a later seen() reports it as already processed."""
    store = _store(tmp_path, clock=lambda: 0.0)
    try:
        store.mark("E9", ttl_seconds=TTL)
        assert store.seen("E9", ttl_seconds=TTL) is True
        store.mark("", ttl_seconds=TTL)  # empty is a no-op
        assert store.seen("", ttl_seconds=TTL) is False
    finally:
        store.close()


def test_context_manager_closes_connection(tmp_path: Path) -> None:
    """The store is usable as a context manager and closes its connection on exit."""
    db = tmp_path / "state.db"
    with EventStore(db, clock=lambda: 0.0) as store:
        assert store.seen("E1", ttl_seconds=TTL) is False
    # After close, a fresh store still sees the durably-written row.
    with EventStore(db, clock=lambda: 0.0) as store:
        assert store.seen("E1", ttl_seconds=TTL) is True


def test_creates_parent_directory(tmp_path: Path) -> None:
    """A state.db under a not-yet-existing THOTH_HOME is created (parents made)."""
    db = tmp_path / "nested" / "home" / "state.db"
    store = EventStore(db, clock=lambda: 0.0)
    try:
        assert store.seen("E1", ttl_seconds=TTL) is False
        assert db.is_file()
    finally:
        store.close()


def test_uses_wal_journal_mode(tmp_path: Path) -> None:
    """The store opens WAL journal mode (single-writer daemon friendly)."""
    db = tmp_path / "state.db"
    store = EventStore(db, clock=lambda: 0.0)
    try:
        store.seen("E1", ttl_seconds=TTL)  # force the connection open + pragmas
        # A separate read-only connection confirms the on-disk journal mode is WAL.
        probe = sqlite3.connect(db)
        try:
            mode = probe.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            probe.close()
        assert str(mode).lower() == "wal"
    finally:
        store.close()
