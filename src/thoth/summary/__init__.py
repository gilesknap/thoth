"""Compose the daily and weekly PKM digest from vault frontmatter, post to Slack.

This is the proactive side of the appliance (SPEC section 9 and the Appendix "Summary
content"). A :class:`SummaryEngine` reads the vault's frontmatter -- never an LLM, never
the network for composition -- and renders a Slack ``mrkdwn`` digest:

* **Daily** -- due/overdue actions (overdue flagged), deadlines in the next
  :data:`DUE_SOON_DAYS` days, yesterday's ingests (curated pages whose ``created`` /
  ``updated`` date is yesterday, grouped/counted by ``type``), a media-backlog nudge
  (``status: to_consume`` items, oldest first), and review-flagged pages
  (``review: true`` or ``status: review``).
* **Weekly** -- a week-in-review of ingest counts by ``type`` over the last seven days,
  an actions-status summary (open / overdue), the next week's deadlines (``due_date``
  within seven days), and a suggested review / stale section.

The daily digest also carries a terse **liveness heartbeat** (issue #15) when a
:class:`~thoth.state.MarkerStore` is wired: "still alive -- last ingest/reindex/push at
T", read from the last-success markers each pipeline stage records. It appears whether
or not the digest is otherwise empty, so *silence itself is diagnostic* on the isolated
VPS (a stale "last push" time is the backstop for a wedged sync).

All date arithmetic is done in Python in Europe/London via :data:`LONDON` against an
**injected** ``now`` (a tz-aware :class:`~datetime.datetime`), so every window
(today / overdue / next-3-days / yesterday) is fully deterministic under a frozen clock
in tests. The Slack delivery seam is the injectable :class:`SlackPoster` protocol
(``chat.postMessage``); nothing here imports ``slack_bolt`` / ``slack_sdk``, so the
package is always import-safe under pytest collection (only the standard library plus
``thoth.*`` is imported at module level).

**Summaries are delivered Slack-only and are never filed as vault pages.** The
:data:`~thoth.vault.FOLDER_TYPE_CONTRACT` has no ``summaries`` folder and the
``summary`` ``type`` has no folder mapping, so this package never calls
:meth:`~thoth.vault.Vault.write_page` and the security-critical contract needs no
change (carry-forward item 4). The vault's ``index.md`` Home page (which carries
``type: summary``) is hand-authored / migration-seeded, not written here.

The cron delivery surface is the ``thoth summary {daily,weekly}`` subcommand
(:func:`thoth.__main__.run_summary`): it builds a real Slack ``WebClient`` from
``config.slack_bot_token``, resolves the target channel from ``SLACK_SUMMARY_CHANNEL``
(:meth:`thoth.config.Config.require_slack_summary_channel`, never a hard-coded id), and
calls :meth:`SummaryEngine.post`.

The canonical frontmatter scans live here (:meth:`SummaryEngine.open_actions`,
:meth:`~SummaryEngine.overdue_actions`, :meth:`~SummaryEngine.due_soon_actions`,
:meth:`~SummaryEngine.media_backlog`, :meth:`~SummaryEngine.recent_pages`,
:meth:`~SummaryEngine.review_flagged`) and are reused by
``mcp_server.pkm_todos`` / ``pkm_recent`` so the action / recent logic lives in one
place. A missing or malformed frontmatter date is treated as "no date" -- the item is
still listed and the scan never crashes.
"""

from __future__ import annotations

from thoth._time import LONDON

from .engine import SummaryEngine
from .types import (
    ACTION_OPEN_STATUSES,
    DUE_SOON_DAYS,
    MEDIA_OPEN_STATUS,
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
    "MEDIA_OPEN_STATUS",
    "DUE_SOON_DAYS",
    "ActionItem",
    "MediaItem",
    "PageRef",
    "Digest",
    "SlackPoster",
    "SummaryEngine",
    "SummaryError",
]
