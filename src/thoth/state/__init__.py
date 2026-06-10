"""The transient, single-writer ``~/.thoth/state.db`` SQLite store (SPEC section 10).

This package owns the appliance's **only** state outside the vault: a small, disposable,
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
level, so importing this package at pytest collection is always safe; ``sqlite3`` ships
with CPython. The DB path is taken from :attr:`thoth.config.Config.state_db_path`
(``<THOTH_HOME>/state.db``); a test passes an explicit ``tmp_path`` location so no real
``~/.thoth`` is touched.
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
