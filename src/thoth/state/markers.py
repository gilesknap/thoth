"""The liveness / heartbeat marker store (``markers`` table, issue #15)."""

from __future__ import annotations

from ._db import _StateStore

MARKER_CAPTURE: str = "capture"
"""Liveness marker name for a successful capture/ingest (issue #15)."""

MARKER_REINDEX: str = "reindex"
"""Liveness marker name for a successful Hindsight reindex (issue #15)."""

MARKER_PUSH: str = "push"
"""Liveness marker name for a successful vault commit+push (issue #15)."""

HEARTBEAT_MARKERS: tuple[str, ...] = (MARKER_CAPTURE, MARKER_REINDEX, MARKER_PUSH)
"""The pipeline stages the daily heartbeat reports, in display order (issue #15)."""


class MarkerStore(_StateStore):
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

    The connection-per-operation lifecycle, the no-op ``close`` / context-manager
    protocol, and the injectable clock come from the shared ``_StateStore`` base in
    :mod:`thoth.state`; the same file backs :class:`thoth.state.EventStore` and the
    two tables coexist.
    """

    _SCHEMAS = (
        "CREATE TABLE IF NOT EXISTS markers (name TEXT PRIMARY KEY, ts REAL NOT NULL)",
    )

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
