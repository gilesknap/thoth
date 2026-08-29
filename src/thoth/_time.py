"""Shared time primitives: the persona timezone and the default UTC clock.

A standard-library-only leaf module, so summary, lint, budget and alerts stay
import-safe under pytest collection. A consumer that exposes ``LONDON`` re-exports it
from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = ["LONDON", "utc_now"]

LONDON: ZoneInfo = ZoneInfo("Europe/London")
"""The Europe/London timezone used for every calendar-date computation (SPEC section 9).

:class:`zoneinfo.ZoneInfo` resolves it, and the declared ``tzdata`` dependency keeps
that resolution identical across the 3.11-3.14 matrix, even without a system time-zone
database.
"""


def utc_now() -> datetime:
    """Return the current UTC time, the default injectable clock."""
    return datetime.now(UTC)
