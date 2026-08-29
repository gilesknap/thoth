"""The transient, single-writer ``~/.thoth/state.db`` SQLite store (SPEC section 10).

This package owns the appliance's **only** state outside the vault: a small, disposable,
gitignored SQLite database whose **P1 guardrail** (SPEC section 10) is that it is
*never* a knowledge store, only transport bookkeeping such as Slack redelivery dedupe,
in-flight capture buffers and an optional TTL'd chat context. The instant knowledge
exists it is a vault file, so losing the VPS loses only dedupe history and mid-flight
captures, both cheap, and the database is explicitly **not** backed up nor part of
recovery.

The store is **single-writer** by construction: exactly one daemon process, the Slack
bot, opens it, so there is no git or two-writer surface to reconcile. Each operation
opens a short-lived connection in WAL journal mode with a bounded busy-timeout, so a
brief lock such as a concurrent prune waits rather than errors, and closes it at once.
No connection outlives a call, so nothing leaks a handle or emits an
``unclosed database`` ``ResourceWarning``, a hard error under ``-W error`` and notably
on Python 3.13+. Dedupe is one check per Slack event, so the per-call connect cost is
negligible.

Two tables are implemented: ``processed_events(event_id, ts)``, the Slack redelivery
dedupe pruned past a TTL (SPEC section 10), and ``markers(name, ts)``, the **liveness or
heartbeat** key-to-timestamp table (issue #15) that the daemon and the cron entrypoints
stamp with a last-success time per pipeline stage. The ``captures`` and
``conversations`` tables the SPEC also names live behind the same single-writer seam,
and arrive when their callers are built.

Module level imports only the standard library (``sqlite3``, ``pathlib``, ``time``), and
CPython ships ``sqlite3``, so importing this package at pytest collection is always
safe. The database path comes from
:attr:`thoth.config.Config.state_db_path` (``<THOTH_HOME>/state.db``), and a test passes
an explicit ``tmp_path`` location so no real ``~/.thoth`` is touched.
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
