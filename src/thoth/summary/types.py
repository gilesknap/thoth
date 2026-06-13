"""Constants, the error type and the frozen digest item types for the summary scans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from thoth.render import SlackPoster as SlackPoster
from thoth.vault import CURATED_DIRS

# Human labels for the liveness markers, shown in the daily heartbeat line (issue #15).
_MARKER_LABELS: dict[str, str] = {
    "capture": "ingest",
    "reindex": "reindex",
    "push": "push",
}

ACTION_OPEN_STATUSES: frozenset[str] = frozenset({"todo", "in_progress"})
"""Action ``status`` values treated as still open (SPEC frontmatter contract)."""

MEDIA_BACKLOG_STATUS: str = "todo"
"""The media ``status`` value treated as an unconsumed backlog item (ADR 0013).

Media items share the single action lifecycle (``todo``/``in_progress``/``done``/
``cancelled``); an untouched backlog item is simply still ``todo``.
"""

DUE_SOON_DAYS: int = 3
"""The inclusive look-ahead window (in days) for the daily "next N days" bucket."""

# Reference folders whose pages count as "ingests" for the recent/week scans. The
# actionable folder (actions/) churns for unrelated reasons (status changes), so it is
# excluded from the ingest-count view (SPEC Appendix: "new/changed curated pages").
# Derived from the canonical vault.CURATED_DIRS so the folder vocabulary lives in
# exactly one place (ADR 0005); a divergence is caught by the tests.
_CURATED_DIRS: tuple[str, ...] = CURATED_DIRS

# Folder holding todo/errand actions (``type: action``). ADR 0015 split the media
# consume queue back out into its own media/ folder, so this folder is todos only.
_ACTIONS_DIR: str = "actions"

# Folder holding the media consume queue (``type: media``, ADR 0015).
_MEDIA_DIR: str = "media"

# The ``type`` value that marks a media-queue item (ADR 0015, was ``kind: media``).
_MEDIA_TYPE: str = "media"

# Weekly window length in days.
_WEEK_DAYS: int = 7

# Cap on how many media-backlog nudges the daily digest surfaces (SPEC: "one or two").
_MEDIA_NUDGE_LIMIT: int = 2

# A status value that, on any page, flags it for review.
_REVIEW_STATUS: str = "review"


class SummaryError(Exception):
    """Raised when a digest cannot be composed (for example a missing vault root)."""


@dataclass(frozen=True, slots=True)
class ActionItem:
    """One life-admin action surfaced in a digest (parsed from its frontmatter)."""

    path: str
    """The vault-relative path of the action page (e.g. ``actions/fix-fence.md``)."""
    title: str
    """The page's human-readable title (frontmatter ``title``, else the slug)."""
    status: str
    """The action ``status`` (e.g. ``todo`` / ``in_progress``)."""
    priority: str | None
    """The action ``priority`` (e.g. ``High``), or ``None`` when unset."""
    due_date: date | None
    """The parsed ``due_date``, or ``None`` when absent / malformed."""
    wikilink: str
    """The ``[[actions/<slug>]]`` handle (vault body content, not the Slack line)."""
    obsidian_uri: str = ""
    """The ``obsidian://`` deep link for the Slack digest line (issue #53)."""


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One media-backlog item surfaced in a digest (parsed from its frontmatter)."""

    path: str
    """The vault-relative path of the media page (e.g. ``media/ddia.md``)."""
    title: str
    """The page's human-readable title (frontmatter ``title``, else the slug)."""
    media_type: str | None
    """The ``media_type`` (e.g. ``book`` / ``film``), or ``None`` when unset."""
    added: date | None
    """The date the item entered the backlog (``created``), or ``None`` if unknown."""
    wikilink: str
    """The ``[[media/<slug>]]`` handle for the page (vault body content, not Slack)."""
    obsidian_uri: str = ""
    """The ``obsidian://`` deep link for the Slack digest line (issue #53)."""


@dataclass(frozen=True, slots=True)
class PageRef:
    """A curated page surfaced in a digest (recent ingest or review-flagged)."""

    path: str
    """The vault-relative path of the page (e.g. ``concepts/distributed.md``)."""
    title: str
    """The page's human-readable title (frontmatter ``title``, else the slug)."""
    page_type: str
    """The frontmatter ``type`` (e.g. ``entity`` / ``concept``)."""
    updated: date | None
    """The newest of ``updated`` / ``created``, or ``None`` when both are absent."""
    wikilink: str
    """The ``[[<slug>]]`` handle (bare slug, Obsidian-resolved; vault body content)."""
    obsidian_uri: str = ""
    """The ``obsidian://`` deep link for the Slack digest line (issue #53)."""


@dataclass(frozen=True, slots=True)
class Digest:
    """A rendered digest ready to post to Slack."""

    kind: str
    """``'daily'`` or ``'weekly'``."""
    title: str
    """The header line (e.g. ``Daily PKM Summary - Mon 2026-06-01 (Europe/London)``)."""
    text: str
    """The rendered ``mrkdwn`` body posted to Slack (includes the title line)."""
    is_empty: bool
    """``True`` when no actionable item surfaced; a caller may then skip posting."""
