"""The transient, single-writer ``~/.thoth/state.db`` SQLite store (SPEC section 10).

This module owns the appliance's **only** state outside the vault: a small, disposable,
gitignored SQLite database whose **P1 guardrail** (SPEC section 10) is that it is
*never* a knowledge store -- only transport bookkeeping (Slack redelivery dedupe,
in-flight capture buffers, optional TTL'd chat context). The instant knowledge exists it
is a vault file; lose the VPS and you lose only dedupe history + mid-flight captures,
both cheap, so the DB is explicitly **not** backed up and **not** part of recovery.

The store is **single-writer** by construction: exactly one daemon process (the Slack
bot) opens it, so there is no git / two-writer surface to reconcile. It is opened in
WAL journal mode with a bounded busy-timeout so a brief lock (for example a concurrent
prune) waits rather than erroring, and every connection is closed cleanly so tests leak
no file handles.

Currently one table is implemented -- ``processed_events(event_id, ts)`` (the Slack
redelivery dedupe, pruned past a TTL, SPEC section 10). The ``captures`` and
``conversations`` tables the SPEC also names live behind the same single-writer seam and
are added when their callers are built.

Only the standard library (``sqlite3``, ``pathlib``, ``time``) is imported at module
level, so importing this module at pytest collection is always safe; ``sqlite3`` ships
with CPython. The DB path is taken from :attr:`thoth.config.Config.state_db_path`
(``<THOTH_HOME>/state.db``); a test passes an explicit ``tmp_path`` location so no real
``~/.thoth`` is touched.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

__all__ = ["EventStore"]


class EventStore:
    """Durable, single-writer store of processed Slack event ids (``processed_events``).

    Backs :class:`thoth.slack_app.EventDedupe` so a Slack redelivery that straddles a
    daemon restart is still recognised as already-processed (the in-memory TTL set is
    lost on restart; this table survives it). The table is
    ``processed_events(event_id TEXT PRIMARY KEY, ts REAL)`` where ``ts`` is the
    wall-clock seconds the id was first recorded, used to prune past the TTL.

    The connection is opened lazily on first use and kept open for the (single-writer)
    process lifetime; :meth:`close` releases it and the instance is usable as a context
    manager so a test closes it deterministically. The clock is injectable so the TTL
    pruning is testable without sleeping.
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
        self._conn: sqlite3.Connection | None = None

    # ---- connection lifecycle ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Return the open connection, creating the file, schema, and pragmas once."""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path)
            # WAL + a bounded busy timeout suit a single-writer daemon: a brief lock
            # (a concurrent prune) waits rather than raising, and readers never block
            # the writer. The timeout is generous but finite so a test never hangs.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(self._SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """Close the underlying connection if it is open (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

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
        conn = self._connect()
        self.prune(ttl_seconds=ttl_seconds)
        now = self._clock()
        # INSERT OR IGNORE is the atomic test-and-set: the PRIMARY KEY makes a second
        # insert of the same id a no-op, so rowcount tells us whether it was new.
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
        conn = self._connect()
        self.prune(ttl_seconds=ttl_seconds)
        conn.execute(
            "INSERT OR REPLACE INTO processed_events (event_id, ts) VALUES (?, ?)",
            (event_id, self._clock()),
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
        conn = self._connect()
        cutoff = self._clock() - ttl_seconds
        cursor = conn.execute("DELETE FROM processed_events WHERE ts < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount
