"""The transient, single-writer ``~/.thoth/state.db`` SQLite store (SPEC section 10).

This is the appliance's only state outside the vault: a small, disposable, gitignored
database whose P1 guardrail is that it is never a knowledge store, only transport
bookkeeping. The instant knowledge exists it is a vault file. Lose the VPS and you lose
dedupe history plus mid-flight captures, both cheap, so the DB is not backed up and is
not part of recovery.

It is single-writer by construction, since only the Slack daemon opens it, so there is
no git or two-writer surface to reconcile. Each operation opens a short-lived WAL
connection with a bounded busy-timeout and closes it immediately, so nothing outlives a
call and nothing emits an ``unclosed database`` ``ResourceWarning``, a hard error under
``-W error`` on Python 3.13+.

Two tables are implemented. ``processed_events(event_id, ts)`` is the Slack redelivery
dedupe, pruned past a TTL. ``markers(name, ts)`` is the liveness heartbeat (issue #15):
the daemon and the cron entrypoints record a last-success time per stage, so the daily
summary can report "last ingest/reindex/push at T" and silence is itself diagnostic.

The ``captures`` and ``conversations`` tables the SPEC names are added when their
callers are built.

Only the standard library is imported at module level, so importing this at pytest
collection is always safe. The DB path comes from
:attr:`thoth.config.Config.state_db_path`, and a test passes an explicit ``tmp_path``.
"""

from __future__ import annotations

from .events import EventStore
from .markers import (
    HEARTBEAT_MARKERS,
    MARKER_CAPTURE,
    MARKER_PUSH,
    MARKER_REINDEX,
    MarkerStore,
)

__all__ = [
    "EventStore",
    "MarkerStore",
    "MARKER_CAPTURE",
    "MARKER_REINDEX",
    "MARKER_PUSH",
    "HEARTBEAT_MARKERS",
]
