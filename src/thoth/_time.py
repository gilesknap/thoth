"""Shared time primitives: the persona timezone and the default UTC clock.

Stdlib-only leaf module, so summary, lint, budget and alerts stay import-safe under
pytest collection. Consumers that expose ``LONDON`` publicly re-export it from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = ["LONDON", "utc_now"]

LONDON: ZoneInfo = ZoneInfo("Europe/London")
"""The Europe/London timezone every calendar-date computation runs on (SPEC section 9).

``tzdata`` is a base dependency, so this resolves identically across the 3.11-3.14
matrix even on a container with no OS time-zone database.
"""


def utc_now() -> datetime:
    """Return the current UTC time (the default injectable clock)."""
    return datetime.now(UTC)
