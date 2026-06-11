"""The Slack redelivery-dedupe store (``processed_events`` table)."""

from __future__ import annotations

import sqlite3

from ._db import _StateStore


class EventStore(_StateStore):
    """Durable, single-writer store of processed Slack event ids (``processed_events``).

    Backs :class:`thoth.slack_app.EventDedupe` so a Slack redelivery that straddles a
    daemon restart is still recognised as already-processed (the in-memory TTL set is
    lost on restart; this table survives it). The table is
    ``processed_events(event_id TEXT PRIMARY KEY, ts REAL)`` where ``ts`` is the
    wall-clock seconds the id was first recorded, used to prune past the TTL.

    The connection-per-operation lifecycle, the no-op ``close`` / context-manager
    protocol, and the injectable clock come from the shared ``_StateStore`` base in
    :mod:`thoth.state`.
    """

    _SCHEMAS = (
        "CREATE TABLE IF NOT EXISTS processed_events ("
        "event_id TEXT PRIMARY KEY, ts REAL NOT NULL)",
    )

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
