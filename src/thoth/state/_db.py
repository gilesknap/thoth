"""Shared SQLite plumbing for the per-operation stores over ``state.db``.

:class:`_StateStore` owns the connection-per-operation lifecycle every store in the
state DB shares: create the parent directory, open a short-lived connection with the WAL
and busy-timeout pragmas, apply the subclass's schema statements, and close before the
operation returns. Only the standard library is imported, so the module is always safe
to import at pytest collection.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self


class _StateStore:
    """Base for the single-writer, connection-per-operation ``state.db`` stores.

    Each operation opens a short-lived connection, applies the pragmas and the
    subclass's :attr:`_SCHEMAS`, and closes before returning, so no handle outlives a
    call, no caller discipline is required, and nothing can leak an ``unclosed
    database`` ``ResourceWarning``, which is a hard error under ``-W error`` on Python
    3.13+. :meth:`close` and the context-manager protocol stay as no-ops for API
    compatibility, and the clock is injectable so time-dependent behaviour is testable
    without the wall clock.
    """

    _SCHEMAS: tuple[str, ...] = ()
    """The ``CREATE TABLE IF NOT EXISTS`` statements applied on every connection."""

    def __init__(
        self, db_path: Path, *, clock: Callable[[], float] | None = None
    ) -> None:
        """Binds the store to the state DB, creating any missing parent directory.

        The clock is the wall clock rather than a monotonic one, so a recorded timestamp
        survives the process restart monotonic time would reset. The same file backs
        every store and the tables coexist.

        Args:
            db_path: The SQLite file path, a ``tmp_path`` location in tests
            clock: A time source returning seconds, defaulting to :func:`time.time`
        """
        self._db_path = db_path
        self._clock = clock if clock is not None else time.time

    # ---- connection lifecycle ----------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yields a short-lived connection, schema and pragmas applied, closed after."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # contextlib.closing, not the connection's own context manager, whose __exit__
        # commits or rolls back but never closes the handle
        with closing(sqlite3.connect(self._db_path)) as conn:
            # WAL plus a bounded busy timeout suit a single-writer daemon: a brief
            # lock waits rather than raising, and the finite timeout means a test never
            # hangs. WAL is a persistent on-disk property, so setting it per connection
            # is idempotent
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            for schema in self._SCHEMAS:
                conn.execute(schema)
            conn.commit()
            yield conn

    def close(self) -> None:
        """No-op: connections are per-operation and already closed.

        Retained so existing callers and the context-manager protocol stay valid.
        """
        return None

    def __enter__(self) -> Self:
        """Returns self, since the connection lifetime is already per-operation."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Closes on context-manager exit, which is a no-op."""
        self.close()
