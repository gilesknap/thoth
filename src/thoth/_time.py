"""Shared time primitives: the persona timezone and the default UTC clock.

This module is a leaf that imports only the standard library, so summary, lint, budget
and alerts all stay import-safe under pytest collection. A consumer that exposes
``LONDON`` in its own public surface re-exports the name from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = ["LONDON", "utc_now"]

LONDON: ZoneInfo = ZoneInfo("Europe/London")
"""The Europe/London timezone used for every calendar-date computation (SPEC section 9).

:class:`zoneinfo.ZoneInfo` resolves the name. The package declares ``tzdata`` as a base
dependency, so the name resolves identically across the 3.11-3.14 matrix, even on a
minimal container with no operating-system time-zone database.
"""


def utc_now() -> datetime:
    """Return the current UTC time (the default injectable clock)."""
    return datetime.now(UTC)
