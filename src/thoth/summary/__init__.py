"""Compose the daily and weekly PKM digest from vault frontmatter, post to Slack.

This is the proactive side of the appliance (SPEC section 9). A :class:`SummaryEngine`
reads the vault's frontmatter, never an LLM and never the network, and renders a Slack
``mrkdwn`` digest:

* **Daily** -- due and overdue actions with overdue flagged, deadlines in the next
  :data:`DUE_SOON_DAYS` days, yesterday's ingests grouped by ``type``, a media-backlog
  nudge of items still :data:`MEDIA_BACKLOG_STATUS` oldest first, and review-flagged
  pages. Media items are excluded from the action buckets, since ADR 0013 gives them
  their own queue on a shared lifecycle.
* **Weekly** -- ingest counts by ``type`` over seven days, an actions-status summary of
  open against overdue, the next week's deadlines, and a suggested review section.

The daily digest also carries a terse liveness heartbeat (issue #15) when a
:class:`~thoth.state.MarkerStore` is wired, read from the last-success markers each
pipeline stage records. It appears whether or not the digest is otherwise empty, so
silence itself is diagnostic on the isolated VPS and a stale "last push" time is the
backstop for a wedged sync.

All date arithmetic happens in Python in Europe/London against an injected ``now``, so
every window is deterministic under a frozen clock in tests. The Slack delivery seam is
the injectable :class:`SlackPoster` protocol, and nothing here imports a Slack SDK, so
the package is always import-safe under pytest collection.

Summaries are delivered Slack-only and are never filed as vault pages. The
:data:`~thoth.vault.FOLDER_TYPE_CONTRACT` has no ``summaries`` folder and the
``summary`` type has no folder mapping, so this package never calls
:meth:`~thoth.vault.Vault.write_page`. The vault's ``index.md`` Home page carries
``type: summary`` but is hand-authored, not written here.

The cron delivery surface is ``thoth summary {daily,weekly}``
(:func:`thoth.__main__.run_summary`), which builds a real Slack ``WebClient``, resolves
the target channel from ``SLACK_SUMMARY_CHANNEL`` rather than any hard-coded id, and
calls :meth:`SummaryEngine.post`.

The canonical frontmatter scans live here and are reused by ``mcp_server.pkm_todos`` and
``pkm_recent``, so the action and recent logic lives in one place. A missing or
malformed frontmatter date is treated as "no date": the item is still listed and the
scan never crashes.
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
