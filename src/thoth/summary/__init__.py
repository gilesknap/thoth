"""Compose the daily and weekly PKM digest from vault frontmatter, post to Slack.

This is the proactive side of the appliance (SPEC section 9 and the Appendix "Summary
content"). A :class:`SummaryEngine` reads the vault's frontmatter, using neither an LLM
nor the network to compose, and renders a Slack ``mrkdwn`` digest:

* **Daily**: due and overdue actions, with overdue flagged, deadlines in the next
  :data:`DUE_SOON_DAYS` days, yesterday's ingests, meaning curated pages whose
  ``created`` or ``updated`` date is yesterday, grouped and counted by ``type``, a
  media-backlog nudge listing ``kind: media`` items still at
  :data:`MEDIA_BACKLOG_STATUS` oldest first, and review-flagged pages carrying
  ``review: true`` or ``status: review``. A media item is excluded from the action
  buckets, because ADR 0013 gives it the action lifecycle but its own queue.
* **Weekly**: a week-in-review of ingest counts by ``type`` over the last seven days,
  an actions-status summary of open and overdue, the next week's deadlines with a
  ``due_date`` within seven days, and a suggested review and stale section.

The daily digest also carries a terse **liveness heartbeat** (issue #15) when a
:class:`~thoth.state.MarkerStore` is wired, reading "still alive, last
ingest/reindex/push at T" from the last-success markers each pipeline stage records. It
appears whether or not the digest is otherwise empty, so *silence itself is diagnostic*
on the isolated VPS, and a stale "last push" time is the backstop for a wedged sync.

All date arithmetic happens in Python, in Europe/London through :data:`LONDON`, against
an **injected** ``now``, a tz-aware :class:`~datetime.datetime`. Every window is
therefore fully deterministic under a frozen clock in tests: today, overdue, next-3-days
and yesterday. The Slack delivery seam is the injectable :class:`SlackPoster`
``chat.postMessage`` protocol, and nothing here imports ``slack_bolt`` or ``slack_sdk``,
so the package is always import-safe under pytest collection, module level importing
only the standard library and ``thoth.*``.

**A summary is delivered Slack-only and is never filed as a vault page.**
:data:`~thoth.vault.FOLDER_TYPE_CONTRACT` has no ``summaries`` folder and the
``summary`` ``type`` has no folder mapping, so this package never calls
:meth:`~thoth.vault.Vault.write_page` and the security-critical contract needs no change
(carry-forward item 4). The vault's ``index.md`` Home page, which carries
``type: summary``, is hand-authored or migration-seeded rather than written here.

The cron delivery surface is the ``thoth summary {daily,weekly}`` subcommand,
:func:`thoth.__main__.run_summary`. It builds a real Slack ``WebClient`` from
``config.slack_bot_token``, resolves the target channel from ``SLACK_SUMMARY_CHANNEL``
through :meth:`thoth.config.Config.require_slack_summary_channel` rather than a
hard-coded id, and calls :meth:`SummaryEngine.post`.

The canonical frontmatter scans live here: :meth:`SummaryEngine.open_actions`,
:meth:`~SummaryEngine.overdue_actions`, :meth:`~SummaryEngine.due_soon_actions`,
:meth:`~SummaryEngine.media_backlog`, :meth:`~SummaryEngine.recent_pages` and
:meth:`~SummaryEngine.review_flagged`. ``mcp_server.pkm_todos`` and ``pkm_recent``
reuse them, so the action and recent logic lives in one place. A missing or malformed
frontmatter date counts as "no date", so the item is still listed and the scan never
crashes.
"""

from __future__ import annotations

from thoth._time import LONDON

from .engine import SummaryEngine
from .types import (
    ACTION_OPEN_STATUSES,
    DUE_SOON_DAYS,
    MEDIA_BACKLOG_STATUS,
    ActionItem,
    Digest,
    MediaItem,
    PageRef,
    SlackPoster,
    SummaryError,
)

__all__ = [
    "LONDON",
    "ACTION_OPEN_STATUSES",
    "MEDIA_BACKLOG_STATUS",
    "DUE_SOON_DAYS",
    "ActionItem",
    "MediaItem",
    "PageRef",
    "Digest",
    "SlackPoster",
    "SummaryEngine",
    "SummaryError",
]
