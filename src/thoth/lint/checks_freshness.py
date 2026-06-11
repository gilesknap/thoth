"""Freshness / upkeep checks: 5 (stale content), 7 (source drift), 9 (page size)
and 12 (log rotation), plus the window and limit constants they apply.

Each check is a pure function over the parsed pages (or spine-file text) handed
to it by :class:`thoth.lint.LintEngine`; the only non-deterministic input -- the
current calendar date -- is passed in as ``today``.
"""

from __future__ import annotations

import datetime as _dt
import re
from datetime import date

from thoth.fmfields import _parse_date, _str_field
from thoth.summary import MEDIA_BACKLOG_STATUS
from thoth.summary.types import _MEDIA_KIND
from thoth.vault import Vault

from .model import Finding, Severity, _finding, _Page

__all__ = [
    "PAGE_SIZE_LIMIT",
    "LOG_ROTATE_LIMIT",
    "STALE_DAYS",
    "MEDIA_STALE_DAYS",
]

PAGE_SIZE_LIMIT: int = 200
"""Body line count above which a knowledge page is a split candidate (SPEC check 9)."""

LOG_ROTATE_LIMIT: int = 500
"""``## [`` entry count above which ``log.md`` should rotate (SPEC check 12)."""

STALE_DAYS: int = 90
"""A knowledge page is stale when ``updated`` is older than this many days (check 5)."""

MEDIA_STALE_DAYS: int = 180
"""An unconsumed media item is cold this many days after ``created`` (check 5)."""

# Action statuses that exempt an overdue action from the stale check (SPEC check 5).
_ACTION_CLOSED_STATUSES: frozenset[str] = frozenset({"done", "cancelled"})

# A log entry header line "## [YYYY-MM-DD] ...".
_LOG_ENTRY_RE: re.Pattern[str] = re.compile(r"^## \[", re.MULTILINE)


def _check_stale(
    curated: list[_Page], actionable: list[_Page], today: date
) -> list[Finding]:
    """Flag stale reference pages and overdue / cold actionable pages (check 5)."""
    findings: list[Finding] = []
    stale_floor = today - _dt.timedelta(days=STALE_DAYS)
    media_floor = today - _dt.timedelta(days=MEDIA_STALE_DAYS)
    for page in curated:
        updated = _parse_date(page.meta.get("updated") or page.meta.get("created"))
        if updated is not None and updated < stale_floor:
            findings.append(
                _finding(
                    5,
                    "stale",
                    Severity.STALE,
                    page.path,
                    f"reference page updated {updated.isoformat()} is older "
                    f"than {STALE_DAYS} days",
                )
            )
    for page in actionable:
        findings.extend(_stale_actionable(page, today, media_floor))
    return findings


def _stale_actionable(page: _Page, today: date, media_floor: date) -> list[Finding]:
    """Return overdue-action / cold-media findings for one actionable page.

    The media queue lives in ``actions/`` as an ``action`` with ``kind: media``
    (ADR 0013), so the cold-media check keys off the ``kind`` property plus the
    still-``todo`` backlog status rather than a separate ``media`` type / folder.
    """
    out: list[Finding] = []
    page_type = _str_field(page.meta.get("type"))
    status = _str_field(page.meta.get("status"))
    if page_type != "action":
        return out
    if status not in _ACTION_CLOSED_STATUSES:
        due = _parse_date(page.meta.get("due_date"))
        if due is not None and due < today:
            out.append(
                _finding(
                    5,
                    "overdue",
                    Severity.STALE,
                    page.path,
                    f"action is past its due date {due.isoformat()}",
                )
            )
    if (
        status == MEDIA_BACKLOG_STATUS
        and _str_field(page.meta.get("kind")) == _MEDIA_KIND
    ):
        added = _parse_date(page.meta.get("created"))
        if added is not None and added < media_floor:
            out.append(
                _finding(
                    5,
                    "media-cold",
                    Severity.STALE,
                    page.path,
                    f"media unconsumed since {added.isoformat()} is older "
                    f"than {MEDIA_STALE_DAYS} days",
                )
            )
    return out


def _check_source_drift(pages: list[_Page]) -> list[Finding]:
    """Flag ``raw/`` pages whose body sha256 differs from frontmatter (check 7)."""
    findings: list[Finding] = []
    for page in pages:
        stored = _str_field(page.meta.get("sha256"))
        if stored is None:
            continue
        recomputed = Vault.body_sha256(page.body)
        if recomputed != stored:
            findings.append(
                _finding(
                    7,
                    "source-drift",
                    Severity.DRIFT,
                    page.path,
                    "raw body sha256 has drifted from its frontmatter "
                    "(raw edited or source changed)",
                )
            )
    return findings


def _check_page_size(pages: list[_Page]) -> list[Finding]:
    """Flag curated pages over :data:`PAGE_SIZE_LIMIT` body lines (check 9)."""
    findings: list[Finding] = []
    for page in pages:
        line_count = len(page.body.splitlines())
        if line_count > PAGE_SIZE_LIMIT:
            findings.append(
                _finding(
                    9,
                    "page-size",
                    Severity.STYLE,
                    page.path,
                    f"body is {line_count} lines (> {PAGE_SIZE_LIMIT}); "
                    "split into sub-topics",
                )
            )
    return findings


def _check_log_rotation(log_text: str) -> list[Finding]:
    """Flag a ``log.md`` with more than :data:`LOG_ROTATE_LIMIT` entries (check 12)."""
    count = len(_LOG_ENTRY_RE.findall(log_text))
    if count > LOG_ROTATE_LIMIT:
        return [
            _finding(
                12,
                "log-rotation",
                Severity.STYLE,
                "log.md",
                f"log.md has {count} entries (> {LOG_ROTATE_LIMIT}); "
                "rotate to log-YYYY.md",
            )
        ]
    return []
