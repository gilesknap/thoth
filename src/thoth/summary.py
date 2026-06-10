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
module is always import-safe under pytest collection (only the standard library plus
``thoth.*`` is imported at module level).

**Summaries are delivered Slack-only and are never filed as vault pages.** The
:data:`~thoth.vault.FOLDER_TYPE_CONTRACT` has no ``summaries`` folder and the
``summary`` ``type`` has no folder mapping, so this module never calls
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

import datetime as _dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath

import frontmatter
import yaml

from thoth._time import LONDON
from thoth.config import Config
from thoth.fmfields import _is_truthy, _page_tags, _parse_date, _str_field
from thoth.render import SlackPoster, render_vault_ref
from thoth.state import HEARTBEAT_MARKERS, MarkerStore
from thoth.vault import CURATED_DIRS, Vault

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

# Human labels for the liveness markers, shown in the daily heartbeat line (issue #15).
_MARKER_LABELS: dict[str, str] = {
    "capture": "ingest",
    "reindex": "reindex",
    "push": "push",
}

ACTION_OPEN_STATUSES: frozenset[str] = frozenset({"todo", "in_progress"})
"""Action ``status`` values treated as still open (SPEC frontmatter contract)."""

MEDIA_OPEN_STATUS: str = "to_consume"
"""The media ``status`` value treated as an unconsumed backlog item."""

DUE_SOON_DAYS: int = 3
"""The inclusive look-ahead window (in days) for the daily "next N days" bucket."""

# Reference folders whose pages count as "ingests" for the recent/week scans. The
# actionable folder (actions/) churns for unrelated reasons (status changes), so it is
# excluded from the ingest-count view (SPEC Appendix: "new/changed curated pages").
# Derived from the canonical vault.CURATED_DIRS so the folder vocabulary lives in
# exactly one place (ADR 0005); a divergence is caught by the tests.
_CURATED_DIRS: tuple[str, ...] = CURATED_DIRS

# Folder holding actionable pages (todos + the media consume queue). ADR 0005 folded the
# old media/ folder in here: a media item is an action tagged 'media'.
_ACTIONS_DIR: str = "actions"

# The tag that marks an action as a media-queue item (ADR 0005).
_MEDIA_TAG: str = "media"

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
    """The action ``priority`` (e.g. ``2 - High``), or ``None`` when unset."""
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


class SummaryEngine:
    """Compose daily/weekly digests from vault frontmatter against an injected clock.

    All retrieval is a pure read over the vault's curated and life-admin folders; no
    LLM and no network are used to compose. The single non-deterministic input -- the
    current time -- is injected as ``now`` so every date window is reproducible under a
    frozen clock in tests.
    """

    def __init__(
        self,
        config: Config,
        vault: Vault,
        *,
        now: datetime | None = None,
        markers: MarkerStore | None = None,
    ) -> None:
        """Store collaborators and resolve the injected clock to Europe/London.

        Args:
            config: The frozen runtime config (for the vault name / Slack defaults).
            vault: The path-confined vault facade (the only disk surface used).
            now: A tz-aware "current time" used for all date math; when ``None``,
                :meth:`datetime.now` in :data:`LONDON` is used. A tz-aware value is
                coerced into :data:`LONDON`; a naive value is assumed to already be
                London-local.
            markers: Optional liveness :class:`~thoth.state.MarkerStore`; when wired,
                the daily digest gains a terse "still alive -- last
                ingest/reindex/push at T" heartbeat so silence is itself diagnostic
                (issue #15). ``None`` (the default) omits it, so callers/tests are
                unaffected.
        """
        self._config = config
        self._vault = vault
        resolved = now if now is not None else datetime.now(LONDON)
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=LONDON)
        else:
            resolved = resolved.astimezone(LONDON)
        self._now = resolved
        self._today = resolved.date()
        self._markers = markers
        self._page_cache: dict[str, list[tuple[str, dict[str, object]]]] = {}

    @property
    def now(self) -> datetime:
        """The injected current time, coerced to Europe/London."""
        return self._now

    @property
    def today(self) -> date:
        """The London calendar date derived from :attr:`now`."""
        return self._today

    # ---- digest composition ---------------------------------------------------------

    def daily_digest(self) -> Digest:
        """Compose the daily digest from vault frontmatter using :attr:`today`.

        Sections (SPEC Appendix "Summary content"): overdue / today / next-N-days
        actions, yesterday's curated ingests grouped by ``type``, a media-backlog
        nudge, and review-flagged pages. A section is omitted from the body when it has
        no items; the digest's :attr:`Digest.is_empty` is ``True`` only when every
        section is empty.

        Returns:
            The rendered daily :class:`Digest`.
        """
        overdue = self.overdue_actions()
        today_due = self._actions_due_on(self._today)
        soon = self.due_soon_actions()
        recent = self.recent_pages(days=1)
        media = self.media_backlog()[:_MEDIA_NUDGE_LIMIT]
        review = self.review_flagged()

        title = f"Daily PKM Summary - {self._format_day(self._today)} (Europe/London)"
        sections: list[str] = []

        action_lines: list[str] = []
        action_lines.extend(self._action_line(a, "Overdue") for a in overdue)
        action_lines.extend(self._action_line(a, "Today") for a in today_due)
        action_lines.extend(self._action_line(a, "Next") for a in soon)
        if action_lines:
            sections.append(self._section("ACTIONS", action_lines))

        if recent:
            sections.append(
                self._section(
                    f"INGESTED YESTERDAY ({len(recent)})",
                    self._grouped_recent_lines(recent),
                )
            )

        if media:
            sections.append(
                self._section("MEDIA BACKLOG", [self._media_line(m) for m in media])
            )

        if review:
            sections.append(
                self._section(
                    "FLAGGED FOR REVIEW", [self._review_line(p) for p in review]
                )
            )

        # The actionable sections decide emptiness; the heartbeat is diagnostic
        # plumbing, not "news", so it must NOT make an otherwise-empty digest look
        # non-empty. It is rendered as a trailing line that appears whether or not the
        # digest is empty, so silence (a stale "last push") shows on a quiet day (#15).
        is_empty = not sections
        heartbeat = self.heartbeat_line()
        text = self._render(title, sections, is_empty, footer=heartbeat)
        return Digest(kind="daily", title=title, text=text, is_empty=is_empty)

    def weekly_digest(self) -> Digest:
        """Compose the weekly digest (seven-day windows) from vault frontmatter.

        Sections (SPEC Appendix): a week-in-review of curated ingest counts by ``type``,
        an actions-status summary (open / overdue counts), the next week's deadlines
        (``due_date`` within seven days), and a suggested review / stale section
        (review-flagged pages plus the oldest media backlog). A section is omitted when
        empty; :attr:`Digest.is_empty` is ``True`` only when every section is empty.

        Returns:
            The rendered weekly :class:`Digest`.
        """
        week_pages = self.recent_pages(days=_WEEK_DAYS)
        counts = self._counts_by_type(week_pages)
        open_actions = self.open_actions()
        overdue = self.overdue_actions()
        next_week = self.due_soon_actions(days=_WEEK_DAYS)
        review = self.review_flagged()
        media = self.media_backlog()[:_MEDIA_NUDGE_LIMIT]

        title = f"Weekly PKM Summary - {self._format_day(self._today)} (Europe/London)"
        sections: list[str] = []

        if counts:
            lines = [f"{count} {ptype}" for ptype, count in counts]
            sections.append(
                self._section(f"WEEK IN REVIEW ({len(week_pages)} ingests)", lines)
            )

        status_lines = [
            f"Open actions: {len(open_actions)}",
            f"Overdue: {len(overdue)}",
        ]
        sections.append(self._section("ACTIONS STATUS", status_lines))

        if next_week:
            sections.append(
                self._section(
                    "NEXT WEEK'S DEADLINES",
                    [self._action_line(a, "Due") for a in next_week],
                )
            )

        review_lines = [self._review_line(p) for p in review]
        review_lines.extend(self._media_line(m) for m in media)
        if review_lines:
            sections.append(self._section("SUGGESTED REVIEW", review_lines))

        # The actions-status section is always present, so the weekly digest is empty
        # only when there is genuinely nothing to report (no actions, no ingests, etc.).
        is_empty = not (
            counts or open_actions or overdue or next_week or review or media
        )
        text = self._render(title, sections, is_empty)
        return Digest(kind="weekly", title=title, text=text, is_empty=is_empty)

    def post(
        self,
        poster: SlackPoster,
        digest: Digest,
        *,
        channel: str,
        skip_when_empty: bool = False,
    ) -> bool:
        """Deliver ``digest`` to ``channel`` via ``poster.chat_postMessage``.

        Args:
            poster: The injected Slack delivery seam.
            digest: The composed digest to post.
            channel: The Slack channel (or DM) id to post to.
            skip_when_empty: When ``True`` and ``digest.is_empty``, do not post.

        Returns:
            ``True`` if a message was posted, ``False`` if skipped because the digest
            was empty and ``skip_when_empty`` was set.
        """
        if skip_when_empty and digest.is_empty:
            return False
        poster.chat_postMessage(channel=channel, text=digest.text)
        return True

    # ---- liveness / heartbeat (issue #15) -------------------------------------------

    def heartbeat_line(self) -> str | None:
        """Render the terse "still alive -- last ingest/reindex/push at T" line.

        Reads the liveness :class:`~thoth.state.MarkerStore` (each pipeline stage
        records its last-success wall-clock time): for each of capture/reindex/push it
        reports the recorded time (formatted in :data:`LONDON`) or ``never`` when no
        success has been recorded, so a stale or missing marker is visible on the daily
        digest. Returns ``None`` when no marker store is wired (heartbeat then omitted).

        Returns:
            The ``mrkdwn`` heartbeat line, or ``None`` when no markers are available.
        """
        if self._markers is None:
            return None
        try:
            recorded = self._markers.all()
        except Exception:  # noqa: BLE001 - a marker-read failure must not break the post
            recorded = {}
        parts: list[str] = []
        for name in HEARTBEAT_MARKERS:
            label = _MARKER_LABELS.get(name, name)
            ts = recorded.get(name)
            parts.append(f"{label} {self._format_marker_ts(ts)}")
        return "still alive -- last " + ", ".join(parts)

    def _format_marker_ts(self, ts: float | None) -> str:
        """Format a marker epoch as ``YYYY-MM-DD HH:MM`` in London, or ``never``."""
        if ts is None:
            return "never"
        when = datetime.fromtimestamp(ts, tz=LONDON)
        return when.strftime("%Y-%m-%d %H:%M")

    # ---- pure frontmatter scans (reused by mcp_server) ------------------------------

    def open_actions(self) -> list[ActionItem]:
        """Return open actions (``status`` in :data:`ACTION_OPEN_STATUSES`).

        Sorted by due date (items with no due date last), then by path for stability.

        Returns:
            The open :class:`ActionItem` list.
        """
        items = [
            item for item in self._scan_actions() if item.status in ACTION_OPEN_STATUSES
        ]
        return self._sort_actions(items)

    def closed_actions(self) -> list[ActionItem]:
        """Return closed actions (a non-blank ``status`` not in the open set).

        A missing/blank status counts as open and is therefore excluded. Kept in scan
        order (path-sorted) for determinism.

        Returns:
            The closed :class:`ActionItem` list.
        """
        return [
            item
            for item in self._scan_actions()
            if item.status and item.status not in ACTION_OPEN_STATUSES
        ]

    def overdue_actions(self) -> list[ActionItem]:
        """Return open actions whose ``due_date`` is strictly before :attr:`today`.

        An action with ``due_date`` equal to today is *not* overdue (it is "today").
        Actions with no due date are never overdue.

        Returns:
            The overdue :class:`ActionItem` list, earliest due first.
        """
        return [
            item
            for item in self.open_actions()
            if item.due_date is not None and item.due_date < self._today
        ]

    def due_soon_actions(self, *, days: int = DUE_SOON_DAYS) -> list[ActionItem]:
        """Return open actions due strictly after today through ``today + days``.

        The window is ``today < due_date <= today + days`` -- it excludes today (those
        belong to the "today" bucket and to :meth:`overdue_actions` only when past) and
        is inclusive of the far edge. So with ``days == DUE_SOON_DAYS`` a due date of
        ``today + DUE_SOON_DAYS`` is included and ``today + DUE_SOON_DAYS + 1`` is not.

        Args:
            days: The inclusive look-ahead window length in days.

        Returns:
            The due-soon :class:`ActionItem` list, earliest due first.
        """
        horizon = self._today + _dt.timedelta(days=days)
        return [
            item
            for item in self.open_actions()
            if item.due_date is not None and self._today < item.due_date <= horizon
        ]

    def media_backlog(self) -> list[MediaItem]:
        """Return unconsumed media (``status == MEDIA_OPEN_STATUS``), oldest first.

        Items are sorted by their ``added`` (``created``) date ascending so the
        longest-waiting backlog item is first; items with no date sort last.

        Returns:
            The media-backlog :class:`MediaItem` list, oldest first.
        """
        items = [
            item
            for item, status in self._scan_media_with_status()
            if status == MEDIA_OPEN_STATUS
        ]
        return sorted(items, key=lambda item: _date_key(item.added, item.path))

    def recent_pages(self, *, days: int = 1) -> list[PageRef]:
        """Return curated pages whose ``updated``/``created`` is within ``days``.

        The window is ``today - days <= page_date <= today``, so ``days == 1`` covers
        yesterday and today (the daily digest's "yesterday's ingests"), and ``days ==
        7`` covers the last week (the weekly week-in-review). A page with no date
        is excluded (it cannot be placed in the window). Only the reference folders
        (entities/notes/memories) are scanned; the actionable ``actions/`` folder is
        excluded (its status churn is not an "ingest").

        Args:
            days: The look-back window length in days (``1`` = yesterday + today).

        Returns:
            The recent :class:`PageRef` list, most-recently-updated first.
        """
        floor = self._today - _dt.timedelta(days=max(days, 1))
        refs = [
            ref
            for ref in self._scan_curated()
            if ref.updated is not None and floor <= ref.updated <= self._today
        ]
        refs.sort(key=lambda r: (r.updated or date.min, r.path), reverse=True)
        return refs

    def review_flagged(self) -> list[PageRef]:
        """Return curated pages flagged for review.

        A page is flagged when its frontmatter has ``review: true`` (any truthy form)
        or ``status: review``. Sorted by path for stability.

        Returns:
            The review-flagged :class:`PageRef` list.
        """
        flagged = [
            ref for ref, meta in self._scan_curated_with_meta() if _is_flagged(meta)
        ]
        flagged.sort(key=lambda r: r.path)
        return flagged

    # ---- internal scans -------------------------------------------------------------

    def _scan_actions(self) -> list[ActionItem]:
        """Parse every ``actions/*.md`` page into an :class:`ActionItem`."""
        items: list[ActionItem] = []
        for rel, meta in self._iter_pages(_ACTIONS_DIR):
            slug = PurePosixPath(rel).stem
            items.append(
                ActionItem(
                    path=rel,
                    title=_title(meta, slug),
                    status=_str_field(meta.get("status")) or "",
                    priority=_str_field(meta.get("priority")),
                    due_date=_parse_date(meta.get("due_date")),
                    wikilink=_wikilink(rel),
                    obsidian_uri=self._vault.obsidian_uri(rel),
                )
            )
        return items

    def _scan_media_with_status(self) -> list[tuple[MediaItem, str]]:
        """Parse every media-tagged ``actions/*.md`` page into a (item, status) pair.

        ADR 0005: the media queue lives in ``actions/`` as an ``action`` tagged
        ``media``, so this walks ``actions/`` and keeps only pages whose ``tags``
        contain ``media``. The status is returned alongside the item (rather than stored
        on the frozen :class:`MediaItem`, whose contract has no status field) so
        :meth:`media_backlog` can filter on ``to_consume`` without the item carrying a
        field it does not declare.
        """
        pairs: list[tuple[MediaItem, str]] = []
        for rel, meta in self._iter_pages(_ACTIONS_DIR):
            if _MEDIA_TAG not in _page_tags(meta):
                continue
            slug = PurePosixPath(rel).stem
            item = MediaItem(
                path=rel,
                title=_title(meta, slug),
                media_type=_str_field(meta.get("media_type")),
                added=_parse_date(meta.get("created")),
                wikilink=_wikilink(rel),
                obsidian_uri=self._vault.obsidian_uri(rel),
            )
            pairs.append((item, _str_field(meta.get("status")) or ""))
        return pairs

    def _scan_curated(self) -> list[PageRef]:
        """Parse every curated page into a :class:`PageRef`."""
        return [ref for ref, _ in self._scan_curated_with_meta()]

    def _scan_curated_with_meta(self) -> list[tuple[PageRef, dict[str, object]]]:
        """Parse every curated page into a (``PageRef``, frontmatter) pair."""
        pairs: list[tuple[PageRef, dict[str, object]]] = []
        for folder in _CURATED_DIRS:
            for rel, meta in self._iter_pages(folder):
                slug = PurePosixPath(rel).stem
                pairs.append(
                    (
                        PageRef(
                            path=rel,
                            title=_title(meta, slug),
                            page_type=_str_field(meta.get("type")) or "",
                            updated=_parse_date(
                                meta.get("updated") or meta.get("created")
                            ),
                            wikilink=f"[[{slug}]]",
                            obsidian_uri=self._vault.obsidian_uri(rel),
                        ),
                        meta,
                    )
                )
        return pairs

    def _iter_pages(self, folder: str) -> list[tuple[str, dict[str, object]]]:
        """Return the ``(vault_relative_path, frontmatter)`` pairs for ``folder``.

        The folder is resolved through the vault for confinement; a missing folder
        yields nothing. A file that cannot be parsed is skipped rather than crashing the
        scan (a malformed page must never wedge the daily digest). The list is read once
        per folder and cached on the engine -- every caller builds a fresh engine per
        invocation, so the cache only dedupes the repeated scans within one digest.
        """
        cached = self._page_cache.get(folder)
        if cached is not None:
            return cached
        try:
            base = self._vault.resolve(folder)
        except Exception as exc:  # pragma: no cover - defensive, resolve is total here
            raise SummaryError(f"cannot resolve folder {folder!r}: {exc}") from exc
        pages: list[tuple[str, dict[str, object]]] = []
        if base.is_dir():
            for path in sorted(base.glob("*.md")):
                rel = PurePosixPath(folder) / path.name
                try:
                    post = frontmatter.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
                    continue
                pages.append((rel.as_posix(), dict(post.metadata)))
        return self._page_cache.setdefault(folder, pages)

    # ---- rendering helpers ----------------------------------------------------------

    def _actions_due_on(self, day: date) -> list[ActionItem]:
        """Return open actions whose ``due_date`` equals ``day``."""
        return [
            item
            for item in self.open_actions()
            if item.due_date is not None and item.due_date == day
        ]

    @staticmethod
    def _sort_actions(items: list[ActionItem]) -> list[ActionItem]:
        """Sort actions by due date (no-date last), then path, stably."""
        return sorted(items, key=lambda item: _date_key(item.due_date, item.path))

    @staticmethod
    def _counts_by_type(refs: Sequence[PageRef]) -> list[tuple[str, int]]:
        """Count pages by ``page_type``, returned sorted by type name."""
        return sorted(Counter(ref.page_type for ref in refs).items())

    def _grouped_recent_lines(self, refs: Sequence[PageRef]) -> list[str]:
        """Render recent pages as one concise shared ref per page, grouped by type.

        Each line is ``type: <obsidian-uri|title>`` via the one shared
        :func:`thoth.render.render_vault_ref` helper -- a title-only clickable link with
        no trailing path and no dead ``[[wikilink]]`` (issue #63).
        """
        lines: list[str] = []
        for ref in sorted(refs, key=lambda r: (r.page_type, r.path)):
            label = ref.page_type or "page"
            shared = render_vault_ref(
                obsidian_uri=ref.obsidian_uri, title=ref.title, path=ref.path
            )
            lines.append(f"{label}: {shared}")
        return lines

    def _action_line(self, item: ActionItem, bucket: str) -> str:
        """Render one action line: bucket flag + due/priority + the shared ref (#53)."""
        flag = {"Overdue": "[overdue]", "Today": "[today]"}.get(bucket, f"[{bucket}]")
        due = (
            "due today"
            if item.due_date == self._today
            else f"due {item.due_date.isoformat()}"
            if item.due_date is not None
            else "no due date"
        )
        prio = f", {item.priority}" if item.priority else ""
        ref = render_vault_ref(
            obsidian_uri=item.obsidian_uri, title=item.title, path=item.path
        )
        return f"{flag} ({due}{prio}) {ref}"

    @staticmethod
    def _media_line(item: MediaItem) -> str:
        """Render one media-backlog nudge line with the shared ref (issue #53)."""
        kind = f" ({item.media_type})" if item.media_type else ""
        added = f" - added {item.added.isoformat()}" if item.added is not None else ""
        ref = render_vault_ref(
            obsidian_uri=item.obsidian_uri, title=item.title, path=item.path
        )
        return f"{ref}{kind}{added}"

    @staticmethod
    def _review_line(ref: PageRef) -> str:
        """Render one review-flagged page line as the shared ref (issue #53)."""
        return render_vault_ref(
            obsidian_uri=ref.obsidian_uri, title=ref.title, path=ref.path
        )

    @staticmethod
    def _section(heading: str, lines: Sequence[str]) -> str:
        """Render a digest section as a heading followed by bullet lines."""
        body = "\n".join(f"  - {line}" for line in lines)
        return f"*{heading}*\n{body}"

    @staticmethod
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

    @staticmethod
    def _format_day(day: date) -> str:
        """Format a date as ``Mon 2026-06-01`` (weekday abbreviation + ISO date)."""
        return f"{day.strftime('%a')} {day.isoformat()}"


# ---- module-level frontmatter helpers (pure, total) -------------------------------


def _date_key(d: date | None, path: str) -> tuple[int, date, str]:
    """Sort key: dated items first (date ascending), undated last, then by path."""
    return (1, date.max, path) if d is None else (0, d, path)


def _wikilink(rel: str) -> str:
    """Render the folder-qualified ``[[wikilink]]`` for a vault-relative md path."""
    return f"[[{rel.removesuffix('.md')}]]"


def _title(meta: dict[str, object], slug: str) -> str:
    """Return the frontmatter ``title`` as a string, falling back to ``slug``."""
    value = meta.get("title")
    if isinstance(value, str) and value.strip():
        return value
    return slug


def _is_flagged(meta: dict[str, object]) -> bool:
    """Return ``True`` if frontmatter marks a page for review (``review`` / status)."""
    if _is_truthy(meta.get("review")):
        return True
    status = meta.get("status")
    return isinstance(status, str) and status.strip().lower() == _REVIEW_STATUS
