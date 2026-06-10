"""Shared time primitives: the persona timezone and the default UTC clock.

Stdlib-only leaf module so every consumer (summary, lint, budget, alerts) stays
import-safe under pytest collection. Consumers that expose ``LONDON`` as part of
their public surface re-export it from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = ["LONDON", "utc_now"]

LONDON: ZoneInfo = ZoneInfo("Europe/London")
"""The Europe/London timezone used for every calendar-date computation (SPEC section 9).

Resolved via :class:`zoneinfo.ZoneInfo`; the ``tzdata`` package is declared as a base
dependency so this resolves identically across the 3.11-3.14 matrix even on a minimal
container with no OS time-zone database.
"""


def utc_now() -> datetime:
    """Return the current UTC time (the default injectable clock)."""
    return datetime.now(UTC)
