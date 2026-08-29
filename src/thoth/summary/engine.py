"""The :class:`SummaryEngine` -- canonical frontmatter scans + digest composition.

See :mod:`thoth.summary` for the digest contract. The frozen item types live in
:mod:`thoth.summary.types` and the pure sorting/rendering helpers in
:mod:`thoth.summary.render`.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import PurePosixPath

import frontmatter
import yaml

from thoth._time import LONDON
from thoth.config import Config
from thoth.fmfields import _is_truthy, _parse_date, _str_field
from thoth.render import render_vault_ref
from thoth.state import HEARTBEAT_MARKERS, MarkerStore
from thoth.vault import Vault

from .render import (
    _counts_by_type,
    _date_key,
    _format_day,
    _media_line,
    _render,
    _review_line,
    _section,
    _sort_actions,
)
from .types import (
    _ACTIONS_DIR,
    _CURATED_DIRS,
    _MARKER_LABELS,
    _MEDIA_DIR,
    _MEDIA_NUDGE_LIMIT,
    _MEDIA_TYPE,
    _REVIEW_STATUS,
    _WEEK_DAYS,
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


class SummaryEngine:
    """Composes daily and weekly digests from vault frontmatter.

    All retrieval is a pure read over the vault folders, with no LLM and no network. The
    one non-deterministic input, the current time, is injected as ``now``, so every date
    window is reproducible under a frozen clock.
    """

    def __init__(
        self,
        config: Config,
        vault: Vault,
        *,
        now: datetime | None = None,
        markers: MarkerStore | None = None,
    ) -> None:
        """Stores collaborators and resolves the injected clock to Europe/London.

        Args:
            config: Frozen runtime config, for the vault name and Slack defaults.
            vault: The path-confined vault facade, the only disk surface used.
            now: Current time for all date math. None reads the London clock. An
                aware value is coerced to London and a naive one is assumed to be
                London already.
            markers: Optional liveness store. When wired, the daily digest gains a
                terse heartbeat so silence is itself diagnostic (issue #15). None
                omits it.
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
        """Composes the daily digest from vault frontmatter.

        The sections follow the SPEC appendix: overdue, today and next-N-days actions,
        yesterday's curated ingests grouped by type, a media-backlog nudge, and
        review-flagged pages. An empty section is omitted from the body, and the digest
        counts as empty only when every section is.

        Returns:
            The rendered daily digest.
        """
        overdue = self.overdue_actions()
        today_due = self._actions_due_on(self._today)
        soon = self.due_soon_actions()
        recent = self.recent_pages(days=1)
        media = self.media_backlog()[:_MEDIA_NUDGE_LIMIT]
        review = self.review_flagged()

        title = f"Daily PKM Summary - {_format_day(self._today)} (Europe/London)"
        sections: list[str] = []

        action_lines: list[str] = []
        action_lines.extend(self._action_line(a, "Overdue") for a in overdue)
        action_lines.extend(self._action_line(a, "Today") for a in today_due)
        action_lines.extend(self._action_line(a, "Next") for a in soon)
        if action_lines:
            sections.append(_section("ACTIONS", action_lines))

        if recent:
            sections.append(
                _section(
                    f"INGESTED YESTERDAY ({len(recent)})",
                    self._grouped_recent_lines(recent),
                )
            )

        if media:
            sections.append(_section("MEDIA BACKLOG", [_media_line(m) for m in media]))

        if review:
            sections.append(
                _section("FLAGGED FOR REVIEW", [_review_line(p) for p in review])
            )

        # The actionable sections decide emptiness. The heartbeat is diagnostic
        # plumbing rather than news, so it must not make an empty digest look full. It
        # trails the body either way, so a stale last-push shows on a quiet day (#15)
        is_empty = not sections
        heartbeat = self.heartbeat_line()
        text = _render(title, sections, is_empty, footer=heartbeat)
        return Digest(kind="daily", title=title, text=text, is_empty=is_empty)

    def weekly_digest(self) -> Digest:
        """Composes the weekly digest over seven-day windows.

        The sections follow the SPEC appendix: a week-in-review of ingest counts by
        type, an actions-status summary, the next week's deadlines, and a
        suggested-review section of flagged pages plus the oldest media backlog. An
        empty section is omitted, and the digest counts as empty only when every section
        is.

        Returns:
            The rendered weekly digest.
        """
        week_pages = self.recent_pages(days=_WEEK_DAYS)
        counts = _counts_by_type(week_pages)
        open_actions = self.open_actions()
        overdue = self.overdue_actions()
        next_week = self.due_soon_actions(days=_WEEK_DAYS)
        review = self.review_flagged()
        media = self.media_backlog()[:_MEDIA_NUDGE_LIMIT]

        title = f"Weekly PKM Summary - {_format_day(self._today)} (Europe/London)"
        sections: list[str] = []

        if counts:
            lines = [f"{count} {ptype}" for ptype, count in counts]
            sections.append(
                _section(f"WEEK IN REVIEW ({len(week_pages)} ingests)", lines)
            )

        status_lines = [
            f"Open actions: {len(open_actions)}",
            f"Overdue: {len(overdue)}",
        ]
        sections.append(_section("ACTIONS STATUS", status_lines))

        if next_week:
            sections.append(
                _section(
                    "NEXT WEEK'S DEADLINES",
                    [self._action_line(a, "Due") for a in next_week],
                )
            )

        review_lines = [_review_line(p) for p in review]
        review_lines.extend(_media_line(m) for m in media)
        if review_lines:
            sections.append(_section("SUGGESTED REVIEW", review_lines))

        # The actions-status section is always present, so the weekly digest is empty
        # only when there is genuinely nothing to report
        is_empty = not (
            counts or open_actions or overdue or next_week or review or media
        )
        text = _render(title, sections, is_empty)
        return Digest(kind="weekly", title=title, text=text, is_empty=is_empty)

    def post(
        self,
        poster: SlackPoster,
        digest: Digest,
        *,
        channel: str,
        skip_when_empty: bool = False,
    ) -> bool:
        """Delivers a digest to a Slack channel.

        Args:
            poster: The injected Slack delivery seam.
            digest: The composed digest to post.
            channel: The channel or DM id to post to.
            skip_when_empty: Do not post an empty digest.

        Returns:
            True when a message was posted, False when it was skipped as empty.
        """
        if skip_when_empty and digest.is_empty:
            return False
        poster.chat_postMessage(channel=channel, text=digest.text)
        return True

    # ---- liveness / heartbeat (issue #15) -------------------------------------------

    def heartbeat_line(self) -> str | None:
        """Renders the terse "still alive" line naming each stage's last success.

        Each pipeline stage records its last-success wall-clock time, and a stage with
        no success reads as never, so a stale or missing marker is visible on the daily
        digest.

        Returns:
            The heartbeat line, or None when no marker store is wired.
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
        """Formats a marker epoch in London time."""
        if ts is None:
            return "never"
        when = datetime.fromtimestamp(ts, tz=LONDON)
        return when.strftime("%Y-%m-%d %H:%M")

    # ---- pure frontmatter scans (reused by mcp_server) ------------------------------

    def open_actions(self) -> list[ActionItem]:
        """Returns the open actions, sorted by due date then path.

        Items with no due date sort last, and the path breaks ties for stability.

        Returns:
            The open actions.
        """
        items = [
            item for item in self._scan_actions() if item.status in ACTION_OPEN_STATUSES
        ]
        return _sort_actions(items)

    def closed_actions(self) -> list[ActionItem]:
        """Returns closed actions, meaning a non-blank status outside the open set.

        A missing or blank status counts as open and is excluded. Scan order is kept for
        determinism.

        Returns:
            The closed actions.
        """
        return [
            item
            for item in self._scan_actions()
            if item.status and item.status not in ACTION_OPEN_STATUSES
        ]

    def overdue_actions(self) -> list[ActionItem]:
        """Returns open actions due strictly before today.

        An action due today is not overdue, it belongs to the today bucket, and an
        action with no due date is never overdue.

        Returns:
            The overdue actions, earliest due first.
        """
        return [
            item
            for item in self.open_actions()
            if item.due_date is not None and item.due_date < self._today
        ]

    def due_soon_actions(self, *, days: int = DUE_SOON_DAYS) -> list[ActionItem]:
        """Returns open actions due after today through ``today + days``.

        The window excludes today, which belongs to its own bucket, and includes the far
        edge. So a due date exactly ``days`` out is included and one day later is not.

        Args:
            days: The inclusive look-ahead window in days.

        Returns:
            The due-soon actions, earliest due first.
        """
        horizon = self._today + _dt.timedelta(days=days)
        return [
            item
            for item in self.open_actions()
            if item.due_date is not None and self._today < item.due_date <= horizon
        ]

    def media_backlog(self) -> list[MediaItem]:
        """Returns the unconsumed media backlog, oldest first.

        Sorted by the added date ascending, so the longest-waiting item leads. Items
        with no date sort last.

        Returns:
            The backlog items, oldest first.
        """
        items = [
            item
            for item, status in self._scan_media_with_status()
            if status == MEDIA_BACKLOG_STATUS
        ]
        return sorted(items, key=lambda item: _date_key(item.added, item.path))

    def recent_pages(self, *, days: int = 1) -> list[PageRef]:
        """Returns curated pages updated within the look-back window.

        A window of one day covers yesterday and today, which is the daily digest's
        ingests, and seven covers the weekly review. A page with no date is excluded,
        since it cannot be placed in the window.

        Only the reference folders are scanned. The actionable folder is excluded,
        because its status churn is not an ingest.

        Args:
            days: The look-back window in days.

        Returns:
            The recent pages, most recently updated first.
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
        """Returns curated pages flagged for review.

        A page is flagged when its frontmatter carries a truthy ``review`` or a status
        of review. Sorted by path for stability.

        Returns:
            The flagged pages.
        """
        flagged = [
            ref for ref, meta in self._scan_curated_with_meta() if _is_flagged(meta)
        ]
        flagged.sort(key=lambda r: r.path)
        return flagged

    # ---- internal scans -------------------------------------------------------------

    def _scan_actions(self) -> list[ActionItem]:
        """Parses every ``actions/*.md`` page into an action item, skipping strays.

        Media shares the actionable lifecycle but is its own type in its own folder (ADR
        0015), so a media page manually moved into ``actions/`` is skipped. Without that
        an unwatched film could surface as an open action in the daily digest and the
        ``pkm_actions`` tool.
        """
        items: list[ActionItem] = []
        for rel, meta in self._iter_pages(_ACTIONS_DIR):
            if _str_field(meta.get("type")) == _MEDIA_TYPE:
                continue
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
        """Parses every ``media/*.md`` page into an item and status pair.

        Only pages whose type is media are kept, which guards against a stray page moved
        into the folder. The status rides alongside the item rather than on it, because
        the frozen item contract declares no status field, so :meth:`media_backlog` can
        still filter on it.
        """
        pairs: list[tuple[MediaItem, str]] = []
        for rel, meta in self._iter_pages(_MEDIA_DIR):
            if _str_field(meta.get("type")) != _MEDIA_TYPE:
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
        """Parses every curated page into a page reference."""
        return [ref for ref, _ in self._scan_curated_with_meta()]

    def _scan_curated_with_meta(self) -> list[tuple[PageRef, dict[str, object]]]:
        """Parses every curated page into a reference and frontmatter pair."""
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
        """Returns the path and frontmatter pairs for one folder.

        The folder is resolved through the vault for confinement, and a missing one
        yields nothing. A file that will not parse is skipped rather than crashing the
        scan, because a malformed page must never wedge the daily digest.

        The list is read once per folder and cached. Every caller builds a fresh engine
        per invocation, so the cache only dedupes repeated scans within one digest.
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
        """Returns open actions due on one day."""
        return [
            item
            for item in self.open_actions()
            if item.due_date is not None and item.due_date == day
        ]

    def _grouped_recent_lines(self, refs: Sequence[PageRef]) -> list[str]:
        """Renders recent pages as one concise shared ref each, grouped by type.

        Every line goes through the one shared :func:`thoth.render.render_vault_ref`
        helper, giving a title-only clickable link with no trailing path and no dead
        wikilink (issue #63).
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
        """Renders one action line: bucket flag, due and priority, then the ref."""
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


# ---- module-level frontmatter helpers (pure, total) -------------------------------


def _wikilink(rel: str) -> str:
    """Renders the folder-qualified wikilink for a vault path."""
    return f"[[{rel.removesuffix('.md')}]]"


def _title(meta: dict[str, object], slug: str) -> str:
    """Returns the frontmatter title, falling back to the slug."""
    value = meta.get("title")
    if isinstance(value, str) and value.strip():
        return value
    return slug


def _is_flagged(meta: dict[str, object]) -> bool:
    """Reports whether frontmatter marks a page for review."""
    if _is_truthy(meta.get("review")):
        return True
    status = meta.get("status")
    return isinstance(status, str) and status.strip().lower() == _REVIEW_STATUS
