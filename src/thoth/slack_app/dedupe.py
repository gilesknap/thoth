"""The transient-over-durable redelivery dedupe for Slack events (SPEC section 10)."""

from __future__ import annotations

import time
from collections.abc import Callable

from thoth.state import EventStore

DEDUPE_TTL_SECONDS: float = 3600.0
"""Prune processed-event ids older than one hour (SPEC section 10)."""


class EventDedupe:
    """TTL dedupe of processed Slack event ids: in-memory cache over a durable store.

    Slack redelivers events on a missed ack, so each handler drops a redelivery by
    asking :meth:`seen` once per event. Entries older than ``ttl_seconds`` are pruned
    (SPEC section 10). The in-memory dict is a **fast front cache**; when a
    :class:`thoth.state.EventStore` is injected it is the **durable** backing
    (``processed_events`` in ``~/.thoth/state.db``), so a redelivery that straddles a
    daemon restart -- where the in-memory cache is gone -- is still recognised as
    already-processed by a *fresh* ``EventDedupe`` built over the same state DB. With no
    store injected the behaviour is the legacy transient-only set (used where no daemon
    persistence is wanted). The clock is injectable for deterministic tests.

    Both layers must use the **same clock** for the TTL to agree; the store defaults to
    wall-clock :func:`time.time` (a recorded timestamp must survive a restart, which a
    monotonic clock would reset), so this class also defaults to :func:`time.time` (not
    :func:`time.monotonic`).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEDUPE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
        store: EventStore | None = None,
    ) -> None:
        """Build a dedupe over an optional durable store.

        Args:
            ttl_seconds: How long a recorded event id is remembered before pruning.
            clock: A wall-clock time source returning seconds; defaults to
                :func:`time.time` so recorded timestamps survive a process restart and
                agree with the store's own clock.
            store: The durable :class:`thoth.state.EventStore` backing
                ``processed_events``; when ``None`` the dedupe is in-memory only (the
                legacy transient behaviour). Pass the same clock to both for the TTL to
                agree across the cache and the store.
        """
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.time
        self._store = store
        self._seen: dict[str, float] = {}

    def seen(self, event_id: str) -> bool:
        """Report whether ``event_id`` was already processed, recording it if new.

        Prunes expired cache entries first, then checks the **fast front cache**: a hit
        there is an immediate ``True`` (drop the redelivery). On a cache miss the
        durable :class:`~thoth.state.EventStore` is consulted (its own atomic
        insert-or-ignore is the source of truth across restarts); whatever it reports is
        cached and returned. With no store, a cache miss records the id in the cache and
        returns ``False``. An empty ``event_id`` is always unseen and never recorded (a
        missing id cannot be deduped).

        Args:
            event_id: The Slack event id (or client message id).

        Returns:
            ``True`` if this id was seen before, else ``False``.
        """
        self.prune()
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        already = (
            self._store.seen(event_id, ttl_seconds=self._ttl)
            if self._store is not None
            else False
        )
        self._seen[event_id] = self._clock()
        return already

    def mark(self, event_id: str) -> None:
        """Record ``event_id`` as processed now in the cache and the durable store."""
        if not event_id:
            return
        self._seen[event_id] = self._clock()
        if self._store is not None:
            self._store.mark(event_id, ttl_seconds=self._ttl)

    def prune(self) -> None:
        """Drop every cache entry older than ``ttl_seconds`` (the store self-prunes)."""
        cutoff = self._clock() - self._ttl
        self._seen = {
            event_id: ts for event_id, ts in self._seen.items() if ts >= cutoff
        }
