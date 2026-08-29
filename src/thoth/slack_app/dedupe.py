"""The transient-over-durable redelivery dedupe for Slack events (SPEC section 10)."""

from __future__ import annotations

import time
from collections.abc import Callable

from thoth.state import EventStore

DEDUPE_TTL_SECONDS: float = 3600.0
"""Prune processed-event ids older than one hour (SPEC section 10)."""


class EventDedupe:
    """TTL dedupe of processed Slack event ids, in-memory cache over a durable store.

    Slack redelivers events on a missed ack, so each handler drops a redelivery by
    asking :meth:`seen` once per event, and entries older than ``ttl_seconds`` are
    pruned (SPEC section 10). The in-memory dict is a fast front cache. When a
    :class:`thoth.state.EventStore` is injected it is the durable backing, so a
    redelivery that straddles a daemon restart, where the cache is gone, is still
    recognised by a fresh ``EventDedupe`` built over the same state DB.

    With no store injected the behaviour is the transient-only set.

    Both layers must use the same clock for the TTL to agree. The store defaults to
    wall-clock :func:`time.time`, since a recorded timestamp must survive a restart that
    a monotonic clock would reset, so this class defaults to it too.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEDUPE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
        store: EventStore | None = None,
    ) -> None:
        """Builds a dedupe over an optional durable store.

        Args:
            ttl_seconds: How long a recorded event id is remembered before pruning
            clock: A wall-clock time source in seconds, defaulting to :func:`time.time`
            store: The durable event store, or ``None`` for an in-memory-only dedupe
        """
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.time
        self._store = store
        self._seen: dict[str, float] = {}

    def seen(self, event_id: str) -> bool:
        """Reports whether ``event_id`` was already processed, recording it if new.

        Expired cache entries are pruned first, then the front cache is checked: a hit
        there is an immediate True. On a miss the durable store is consulted, since its
        atomic insert-or-ignore is the source of truth across restarts, and whatever it
        reports is cached and returned.

        With no store, a miss records the id and returns False. An empty ``event_id`` is
        always unseen and never recorded, because a missing id cannot be deduped.

        Args:
            event_id: The Slack event id, or the client message id

        Returns:
            True if this id was seen before, else False
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
        """Records ``event_id`` as processed now, in the cache and the durable store."""
        if not event_id:
            return
        self._seen[event_id] = self._clock()
        if self._store is not None:
            self._store.mark(event_id, ttl_seconds=self._ttl)

    def prune(self) -> None:
        """Drops every cache entry older than ``ttl_seconds``; the store self-prunes."""
        cutoff = self._clock() - self._ttl
        self._seen = {
            event_id: ts for event_id, ts in self._seen.items() if ts >= cutoff
        }
