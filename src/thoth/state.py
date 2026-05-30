"""The transient, single-writer ``~/.thoth/state.db`` SQLite store (SPEC section 10).

This module owns the appliance's **only** state outside the vault: a small, disposable,
gitignored SQLite database whose **P1 guardrail** (SPEC section 10) is that it is
*never* a knowledge store -- only transport bookkeeping (Slack redelivery dedupe,
in-flight capture buffers, optional TTL'd chat context). The instant knowledge exists it
is a vault file; lose the VPS and you lose only dedupe history + mid-flight captures,
both cheap, so the DB is explicitly **not** backed up and **not** part of recovery.

The store is **single-writer** by construction: exactly one daemon process (the Slack
bot) opens it, so there is no git / two-writer surface to reconcile. Each operation
opens a short-lived connection in WAL journal mode with a bounded busy-timeout (so a
brief lock -- for example a concurrent prune -- waits rather than erroring) and closes
it immediately, so no connection ever outlives a call and nothing can leak a handle or
emit an ``unclosed database`` ``ResourceWarning`` (a hard error under ``-W error``,
notably on Python 3.13+). Dedupe is one check per Slack event, so per-call connect
cost is negligible.

Two tables are implemented. ``processed_events(event_id, ts)`` is the Slack redelivery
dedupe (pruned past a TTL, SPEC section 10). ``markers(name, ts)`` is the **liveness /
heartbeat** key->timestamp table (issue #15): the daemon and the cron entrypoints record
a last-success wall-clock time per named pipeline stage (capture/ingest, reindex, push)
so the daily summary can report a terse "still alive -- last ingest/reindex/push at T"
line and silence is itself diagnostic. The ``captures`` and ``conversations`` tables the
SPEC also names live behind the same single-writer seam and are added when their callers
are built.

Only the standard library (``sqlite3``, ``pathlib``, ``time``) is imported at module
level, so importing this module at pytest collection is always safe; ``sqlite3`` ships
with CPython. The DB path is taken from :attr:`thoth.config.Config.state_db_path`
(``<THOTH_HOME>/state.db``); a test passes an explicit ``tmp_path`` location so no real
``~/.thoth`` is touched.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

__all__ = [
    "EventStore",
    "MarkerStore",
    "MARKER_CAPTURE",
    "MARKER_REINDEX",
    "MARKER_PUSH",
    "HEARTBEAT_MARKERS",
]

MARKER_CAPTURE: str = "capture"
"""Liveness marker name for a successful capture/ingest (issue #15)."""

MARKER_REINDEX: str = "reindex"
"""Liveness marker name for a successful Hindsight reindex (issue #15)."""

MARKER_PUSH: str = "push"
"""Liveness marker name for a successful vault commit+push (issue #15)."""

HEARTBEAT_MARKERS: tuple[str, ...] = (MARKER_CAPTURE, MARKER_REINDEX, MARKER_PUSH)
"""The pipeline stages the daily heartbeat reports, in display order (issue #15)."""


class EventStore:
    """Durable, single-writer store of processed Slack event ids (``processed_events``).

    Backs :class:`thoth.slack_app.EventDedupe` so a Slack redelivery that straddles a
    daemon restart is still recognised as already-processed (the in-memory TTL set is
    lost on restart; this table survives it). The table is
    ``processed_events(event_id TEXT PRIMARY KEY, ts REAL)`` where ``ts`` is the
    wall-clock seconds the id was first recorded, used to prune past the TTL.

    Each call opens a short-lived connection and closes it before returning, so no
    handle outlives the operation (no caller discipline is required and nothing can leak
    an ``unclosed database`` ``ResourceWarning``). :meth:`close` and the context-manager
    protocol are retained as no-ops for API compatibility. The clock is injectable so
    the TTL pruning is testable without sleeping.
    """

    _SCHEMA: str = (
        "CREATE TABLE IF NOT EXISTS processed_events ("
        "event_id TEXT PRIMARY KEY, ts REAL NOT NULL)"
    )

    def __init__(
        self, db_path: Path, *, clock: Callable[[], float] | None = None
    ) -> None:
        """Open (creating the parent directory + schema) the state DB at ``db_path``.

        Args:
            db_path: The SQLite file path (``Config.state_db_path`` in production, a
                ``tmp_path`` location in tests). Its parent directory is created if
                absent.
            clock: A wall-clock time source returning seconds; defaults to
                :func:`time.time` (wall clock, not monotonic, so a recorded timestamp
                survives the process restart that monotonic time would reset).
        """
        self._db_path = db_path
        self._clock = clock if clock is not None else time.time

    # ---- connection lifecycle ----------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived connection (file, schema, pragmas), closed on exit."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            # WAL + a bounded busy timeout suit a single-writer daemon: a brief lock
            # (a concurrent prune) waits rather than raising, and readers never block
            # the writer. The timeout is generous but finite so a test never hangs.
            # WAL is a persistent on-disk property, so setting it per connection is
            # idempotent and keeps the db in WAL mode across the open/close cycle.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(self._SCHEMA)
            conn.commit()
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """No-op (idempotent): connections are per-operation and already closed.

        Retained so existing callers and the context-manager protocol stay valid.
        """
        return None

    def __enter__(self) -> EventStore:
        """Enter a context manager managing the connection lifetime."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection on context-manager exit."""
        self.close()

    # ---- processed_events operations ---------------------------------------------

    def seen(self, event_id: str, *, ttl_seconds: float) -> bool:
        """Record ``event_id`` if new and report whether it was already processed.

        Prunes entries older than ``ttl_seconds`` first (so a long-expired id is treated
        as fresh after redelivery, matching the in-memory set), then: an unknown id is
        inserted with the current timestamp and ``False`` is returned (process it); a
        known, un-pruned id returns ``True`` (drop the redelivery). An empty
        ``event_id`` is never recorded and returns ``False`` (cannot dedupe a missing).

        Args:
            event_id: The Slack event id (or client message id).
            ttl_seconds: How long a recorded id is remembered before pruning.

        Returns:
            ``True`` if this id was already recorded (and still within the TTL), else
            ``False``.
        """
        if not event_id:
            return False
        now = self._clock()
        with self._connect() as conn:
            self._prune(conn, cutoff=now - ttl_seconds)
            # INSERT OR IGNORE is the atomic test-and-set: the PRIMARY KEY makes a
            # second insert of the same id a no-op, so rowcount tells us if it was new.
            cursor = conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_id, ts) VALUES (?, ?)",
                (event_id, now),
            )
            conn.commit()
            # rowcount == 1 means the row was inserted (id was new -> unseen).
            return cursor.rowcount == 0

    def mark(self, event_id: str, *, ttl_seconds: float) -> None:
        """Record ``event_id`` as processed now (no-op for an empty id).

        Prunes past the TTL first, then upserts the id with the current timestamp so a
        later :meth:`seen` reports it as already processed.
        """
        if not event_id:
            return
        now = self._clock()
        with self._connect() as conn:
            self._prune(conn, cutoff=now - ttl_seconds)
            conn.execute(
                "INSERT OR REPLACE INTO processed_events (event_id, ts) VALUES (?, ?)",
                (event_id, now),
            )
            conn.commit()

    def prune(self, *, ttl_seconds: float) -> int:
        """Delete every recorded id older than ``ttl_seconds`` from now.

        Args:
            ttl_seconds: The retention window; ids with ``ts`` before
                ``now - ttl_seconds`` are removed.

        Returns:
            The number of rows deleted.
        """
        with self._connect() as conn:
            return self._prune(conn, cutoff=self._clock() - ttl_seconds)

    @staticmethod
    def _prune(conn: sqlite3.Connection, *, cutoff: float) -> int:
        """Delete rows with ``ts < cutoff`` on an open connection; return the count."""
        cursor = conn.execute("DELETE FROM processed_events WHERE ts < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount


class MarkerStore:
    """Durable, single-writer key->timestamp liveness markers (``markers`` table).

    Backs the unattended-observability heartbeat (issue #15, SPEC section 10): each
    pipeline stage that completes -- a capture/ingest, a Hindsight reindex, a vault
    commit+push -- records its last-success wall-clock time here, keyed by a stable
    marker name (:data:`MARKER_CAPTURE` / :data:`MARKER_REINDEX` / :data:`MARKER_PUSH`).
    The daily summary reads them back so it can report a terse "still alive -- last
    ingest/reindex/push at T" line; the *absence* of a recent marker is itself the
    diagnostic signal on an isolated VPS with no other failure channel.

    The table is ``markers(name TEXT PRIMARY KEY, ts REAL)``: at most one row per stage,
    upserted on each success so it always holds the *latest* success time. ``ts`` is
    wall-clock seconds (not monotonic, so a recorded time survives the daemon restart
    that monotonic time would reset). These markers are pure bookkeeping -- never a
    knowledge store -- and share the disposable, gitignored, not-backed-up state DB (the
    P1 guardrail, SPEC section 10): on VPS loss they start empty and the next successful
    run repopulates them.

    Each call opens a short-lived connection and closes it before returning (the same
    connection-per-operation pattern as :class:`EventStore`), so no handle outlives the
    operation and nothing can leak an ``unclosed database`` ``ResourceWarning`` (a hard
    error under ``-W error``, notably on Python 3.13+). :meth:`close` and the
    context-manager protocol are retained as no-ops for API compatibility. The clock is
    injectable so recording and reporting are testable without the wall clock.
    """

    _SCHEMA: str = (
        "CREATE TABLE IF NOT EXISTS markers (name TEXT PRIMARY KEY, ts REAL NOT NULL)"
    )

    def __init__(
        self, db_path: Path, *, clock: Callable[[], float] | None = None
    ) -> None:
        """Open (creating the parent directory + schema) the state DB at ``db_path``.

        Args:
            db_path: The SQLite file path (``Config.state_db_path`` in production, a
                ``tmp_path`` location in tests). Its parent directory is created if
                absent. The same file backs :class:`EventStore`; the two tables coexist.
            clock: A wall-clock time source returning seconds; defaults to
                :func:`time.time` (wall clock, not monotonic, so a recorded marker
                survives the process restart that monotonic time would reset).
        """
        self._db_path = db_path
        self._clock = clock if clock is not None else time.time

    # ---- connection lifecycle ----------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived connection (file, schema, pragmas), closed on exit."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(self._SCHEMA)
            conn.commit()
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """No-op (idempotent): connections are per-operation and already closed."""
        return None

    def __enter__(self) -> MarkerStore:
        """Enter a context manager managing the connection lifetime."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection on context-manager exit."""
        self.close()

    # ---- markers operations ------------------------------------------------------

    def record(self, name: str, *, ts: float | None = None) -> None:
        """Record ``name``'s last-success time (defaults to now); no-op for empty name.

        Upserts the marker so the row always holds the latest success time. A monotone
        guard is intentionally *not* applied: callers pass the time of an event that has
        just succeeded, so the newest write is the correct value even if a clock is
        injected non-monotonically in a test.

        Args:
            name: The marker name (e.g. :data:`MARKER_PUSH`).
            ts: The success time in wall-clock seconds; defaults to the injected clock.
        """
        if not name:
            return
        when = self._clock() if ts is None else ts
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO markers (name, ts) VALUES (?, ?)",
                (name, when),
            )
            conn.commit()

    def get(self, name: str) -> float | None:
        """Return ``name``'s recorded last-success time, or ``None`` if never recorded.

        Args:
            name: The marker name to look up.

        Returns:
            The wall-clock seconds last recorded for ``name``, or ``None``.
        """
        if not name:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ts FROM markers WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        value = row[0]
        return float(value) if value is not None else None

    def all(self) -> dict[str, float]:
        """Return every recorded marker as a ``{name: ts}`` mapping (possibly empty)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT name, ts FROM markers").fetchall()
        return {str(name): float(ts) for name, ts in rows if ts is not None}
