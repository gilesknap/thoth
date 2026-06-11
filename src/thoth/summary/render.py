"""Pure sorting and ``mrkdwn`` rendering helpers for the digest body."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date

from thoth.render import render_vault_ref

from .types import ActionItem, MediaItem, PageRef


def _date_key(d: date | None, path: str) -> tuple[int, date, str]:
    """Sort key: dated items first (date ascending), undated last, then by path."""
    return (1, date.max, path) if d is None else (0, d, path)


def _sort_actions(items: list[ActionItem]) -> list[ActionItem]:
    """Sort actions by due date (no-date last), then path, stably."""
    return sorted(items, key=lambda item: _date_key(item.due_date, item.path))


def _counts_by_type(refs: Sequence[PageRef]) -> list[tuple[str, int]]:
    """Count pages by ``page_type``, returned sorted by type name."""
    return sorted(Counter(ref.page_type for ref in refs).items())


def _media_line(item: MediaItem) -> str:
    """Render one media-backlog nudge line with the shared ref (issue #53)."""
    kind = f" ({item.media_type})" if item.media_type else ""
    added = f" - added {item.added.isoformat()}" if item.added is not None else ""
    ref = render_vault_ref(
        obsidian_uri=item.obsidian_uri, title=item.title, path=item.path
    )
    return f"{ref}{kind}{added}"


def _review_line(ref: PageRef) -> str:
    """Render one review-flagged page line as the shared ref (issue #53)."""
    return render_vault_ref(
        obsidian_uri=ref.obsidian_uri, title=ref.title, path=ref.path
    )


def _section(heading: str, lines: Sequence[str]) -> str:
    """Render a digest section as a heading followed by bullet lines."""
    body = "\n".join(f"  - {line}" for line in lines)
    return f"*{heading}*\n{body}"


def _render(
    title: str,
    sections: Sequence[str],
    is_empty: bool,
    *,
    footer: str | None = None,
) -> str:
    """Assemble the title line, sections, and an optional footer into the body.

    The ``footer`` (the liveness heartbeat) is appended whether or not the digest is
    empty, so the "still alive -- last ... at T" line is present even on a quiet day
    (issue #15).
    """
    if is_empty:
        parts = [f"{title}\n\nNothing to report today."]
    else:
        parts = [title, *sections]
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def _format_day(day: date) -> str:
    """Format a date as ``Mon 2026-06-01`` (weekday abbreviation + ISO date)."""
    return f"{day.strftime('%a')} {day.isoformat()}"
