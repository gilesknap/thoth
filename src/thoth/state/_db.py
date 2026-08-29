"""Shared SQLite plumbing for the per-operation stores over ``state.db``.

:class:`_StateStore` owns the connection-per-operation lifecycle that every store in
the state database shares: :class:`thoth.state.EventStore`,
:class:`thoth.state.MarkerStore` and :class:`thoth.budget.BudgetStore`. It creates the
parent directory, opens a short-lived connection with the WAL and busy-timeout pragmas,
applies the subclass's schema statements, and closes the connection before the operation
returns. Module level imports only the standard library, so this module is always safe
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
    subclass's :attr:`_SCHEMAS` statements, and closes it before returning. No handle
    outlives a call, no caller discipline is needed, and nothing leaks an
    ``unclosed database`` ``ResourceWarning``, a hard error under ``-W error`` and
    notably on Python 3.13+. :meth:`close` and the context-manager protocol stay as
    no-ops for API compatibility. The clock is injectable, so time-dependent behaviour
    is testable without the wall clock.
    """

    _SCHEMAS: tuple[str, ...] = ()
    """The ``CREATE TABLE IF NOT EXISTS`` statements applied on every connection."""

    def __init__(
        self, db_path: Path, *, clock: Callable[[], float] | None = None
    ) -> None:
        """Bind the store to ``db_path``, creating the parent on first use.

        Args:
            db_path: The SQLite file path: :attr:`thoth.config.Config.state_db_path` in
                production, a ``tmp_path`` location in tests. Creates its parent
                directory when absent. The same file backs every store, and the tables
                coexist.
            clock: A wall-clock time source returning seconds, defaulting to
                :func:`time.time`. Wall clock, not monotonic, so a recorded timestamp
                survives the process restart that monotonic time would reset.
        """
        self._db_path = db_path
        self._clock = clock if clock is not None else time.time

    # ---- connection lifecycle ----------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived connection: file, schema, pragmas, closed on exit."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Use contextlib.closing, not the connection's own context manager, whose
        # __exit__ commits or rolls back but never closes the handle.
        with closing(sqlite3.connect(self._db_path)) as conn:
            # WAL and a bounded busy timeout suit a single-writer daemon: a brief lock,
            # such as a concurrent prune, waits rather than raises, and a reader never
            # blocks the writer. The timeout is finite so a test never hangs. WAL is a
            # persistent on-disk property, so setting it per connection is idempotent.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            for schema in self._SCHEMAS:
                conn.execute(schema)
            conn.commit()
            yield conn

    def close(self) -> None:
        """Do nothing, idempotently: a connection is per-operation and already closed.

        Retained so existing callers and the context-manager protocol stay valid.
        """
        return None

    def __enter__(self) -> Self:
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
