"""Shared time primitives: the persona timezone and the default UTC clock.

A standard-library-only leaf module, so every consumer stays import-safe under pytest
collection: summary, lint, budget and alerts. A consumer exposing ``LONDON`` publicly
re-exports it from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = ["LONDON", "utc_now"]

LONDON: ZoneInfo = ZoneInfo("Europe/London")
"""The Europe/London timezone used for every calendar-date computation (SPEC section 9).

:class:`zoneinfo.ZoneInfo` resolves it, and the base dependencies declare ``tzdata``, so
it resolves the same way across the 3.11-3.14 matrix, even on a minimal container with
no operating-system time-zone database.
"""


def utc_now() -> datetime:
    """Return the current UTC time, the default injectable clock."""
    return datetime.now(UTC)
