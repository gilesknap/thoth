"""The persistent per-day call counters behind the budget guard (issue #16)."""

from __future__ import annotations

from thoth.state._db import _StateStore


class BudgetStore(_StateStore):
    """Durable, single-writer per-day call counters in the state DB (issue #16).

    Two tables share the disposable, not-backed-up state DB (the P1 guardrail, SPEC
    section 10): ``daily_budget(day, kind, count)`` holds one row per London day and
    kind, and ``budget_alerts(day, ts)`` records the single per-day cap-tripped claim.
    Keying on the calendar-day string makes the reset implicit, since a new day simply
    has no rows yet, and puts the counter on disk so it survives a daemon restart. The
    connection-per-operation lifecycle comes from the shared ``_StateStore`` base in
    :mod:`thoth.state`, and the same file backs :class:`thoth.state.EventStore` and
    :class:`~thoth.state.MarkerStore` with the tables coexisting.
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
    _SCHEMAS = (_SCHEMA_BUDGET, _SCHEMA_ALERTS)

    def total(self, day: str) -> int:
        """Returns the combined call count recorded for ``day`` across every counter."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM daily_budget WHERE day = ?",
                (day,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def increment(self, day: str, kind: str, *, amount: int = 1) -> int:
        """Adds ``amount`` to the ``(day, kind)`` counter and returns its new value.

        The row is upserted, so the first charge of a kind creates it and later charges
        accumulate.
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
        """Returns ``{kind: count}`` for ``day``, only counters that were charged."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, count FROM daily_budget WHERE day = ?", (day,)
            ).fetchall()
        return {str(kind): int(count) for kind, count in rows if count is not None}

    def claim_alert(self, day: str, *, ts: float) -> bool:
        """Claims the one-per-day cap-tripped alert, reporting whether it won.

        ``INSERT OR IGNORE`` on the ``day`` primary key is the test-and-set: the first
        caller of the day inserts its row and posts the alert, and every later caller is
        ignored and stays silent. That is what makes the notification fire exactly once
        per day across many blocked calls and a daemon restart.

        Args:
            day: The London calendar-day key the cap was tripped on
            ts: The wall-clock seconds to stamp the claim with

        Returns:
            True if this call claimed the day's alert, False if it was already claimed
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO budget_alerts (day, ts) VALUES (?, ?)",
                (day, ts),
            )
            conn.commit()
            return cursor.rowcount == 1
