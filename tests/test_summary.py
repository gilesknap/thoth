"""Tests for :mod:`thoth.summary` -- daily/weekly digests from vault frontmatter.

These build a real seeded vault under ``tmp_path`` (hand-authored ``actions/``,
``media/`` and curated pages carrying the relevant frontmatter) and a real
:class:`~thoth.vault.Vault` over it, so the frontmatter scans, date parsing and folder
confinement are exercised for real. The single non-deterministic input -- the current
time -- is injected as a frozen tz-aware ``now`` so every due/overdue/next-3-days/
yesterday window is reproducible. The Slack delivery seam is a tiny fake
:class:`_FakePoster` recording ``chat_postMessage`` kwargs; no ``slack_bolt`` /
``slack_sdk`` is imported anywhere (a test asserts the module does not pull one in).
"""

from __future__ import annotations

import datetime as _dt
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from thoth.config import Config, load_config
from thoth.state import MARKER_CAPTURE, MARKER_PUSH, MARKER_REINDEX, MarkerStore
from thoth.summary import (
    ACTION_OPEN_STATUSES,
    DUE_SOON_DAYS,
    LONDON,
    MEDIA_OPEN_STATUS,
    ActionItem,
    Digest,
    MediaItem,
    PageRef,
    SummaryEngine,
    SummaryError,
)
from thoth.vault import Vault

# A frozen "now": Monday 2026-06-01 07:00 London (the SPEC worked example anchor).
NOW = datetime(2026, 6, 1, 7, 0, tzinfo=LONDON)

_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "concepts",
    "comparisons",
    "queries",
    "actions",
    "media",
    "memories",
    "people",
    "inbox",
)


# --------------------------------------------------------------------------------------
# fixtures + helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """A real Vault over an empty seeded folder skeleton under tmp_path."""
    for folder in _FOLDERS:
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    config = load_config({"PKM_VAULT": str(tmp_path)})
    return Vault(config)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """The frozen Config matching the ``vault`` fixture's root."""
    return load_config({"PKM_VAULT": str(tmp_path)})


def _write(
    vault: Vault, rel: str, frontmatter: dict[str, Any], body: str = "x"
) -> None:
    """Write a raw page file directly (bypassing write_page validation) for fixtures.

    The summary scans read frontmatter straight off disk, so tests author pages with
    arbitrary frontmatter (including statuses/dates) without going through the
    validating writer.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(str(v) for v in value) + "]"
            lines.append(f"{key}: {rendered}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path = vault.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _action(
    vault: Vault,
    slug: str,
    *,
    title: str,
    status: str = "todo",
    due_date: str | None = None,
    priority: str | None = None,
) -> None:
    """Author an actions/<slug>.md page."""
    meta: dict[str, Any] = {
        "title": title,
        "type": "action",
        "created": "2026-05-20",
        "updated": "2026-05-20",
        "source": "slack",
        "tags": ["task"],
        "status": status,
    }
    if due_date is not None:
        meta["due_date"] = due_date
    if priority is not None:
        meta["priority"] = priority
    _write(vault, f"actions/{slug}.md", meta)


def _media(
    vault: Vault,
    slug: str,
    *,
    title: str,
    status: str = MEDIA_OPEN_STATUS,
    created: str | None = "2026-05-20",
    media_type: str | None = "book",
) -> None:
    """Author a media/<slug>.md page."""
    meta: dict[str, Any] = {
        "title": title,
        "type": "media",
        "updated": "2026-05-20",
        "source": "slack",
        "tags": ["media"],
        "status": status,
    }
    if created is not None:
        meta["created"] = created
    if media_type is not None:
        meta["media_type"] = media_type
    _write(vault, f"media/{slug}.md", meta)


def _curated(
    vault: Vault,
    folder: str,
    slug: str,
    *,
    title: str,
    page_type: str,
    updated: str | None = "2026-05-31",
    created: str | None = "2026-05-31",
    review: Any = None,
    status: str | None = None,
) -> None:
    """Author a curated <folder>/<slug>.md page."""
    meta: dict[str, Any] = {
        "title": title,
        "type": page_type,
        "source": "slack",
        "tags": ["controls"],
    }
    if created is not None:
        meta["created"] = created
    if updated is not None:
        meta["updated"] = updated
    if review is not None:
        meta["review"] = review
    if status is not None:
        meta["status"] = status
    _write(vault, f"{folder}/{slug}.md", meta)


class _FakePoster:
    """Records ``chat_postMessage`` kwargs; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat_postMessage(  # noqa: N802 - matches the Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"channel": channel, "text": text, **kwargs})
        return {"ok": True}


def _engine(vault: Vault, config: Config, *, now: datetime = NOW) -> SummaryEngine:
    """Build a SummaryEngine with the frozen clock."""
    return SummaryEngine(config, vault, now=now)


# --------------------------------------------------------------------------------------
# import safety
# --------------------------------------------------------------------------------------


def test_module_does_not_import_slack_sdk() -> None:
    """Importing thoth.summary pulls in no Slack/anthropic/mcp SDK."""
    import thoth.summary  # noqa: F401  (assert on sys.modules, already imported)

    banned = {"slack_bolt", "slack_sdk", "anthropic", "mcp", "exa_py", "firecrawl"}
    assert banned.isdisjoint(sys.modules)


# --------------------------------------------------------------------------------------
# action bucketing (overdue / today / next-N-days)
# --------------------------------------------------------------------------------------


def test_daily_buckets_overdue_today_and_next_three_days(
    vault: Vault, config: Config
) -> None:
    """An action due before/at/within 3 days of today lands in the right bucket."""
    _action(vault, "overdue-one", title="Reply to review", due_date="2026-05-29")
    _action(vault, "today-one", title="Finish roadmap", due_date="2026-06-01")
    _action(vault, "soon-one", title="Renew domain", due_date="2026-06-03")
    _action(vault, "far-one", title="Later thing", due_date="2026-06-10")

    engine = _engine(vault, config)

    overdue = engine.overdue_actions()
    assert [a.path for a in overdue] == ["actions/overdue-one.md"]

    soon = engine.due_soon_actions()
    assert [a.path for a in soon] == ["actions/soon-one.md"]

    # The far action is in neither overdue nor due-soon nor today.
    daily = engine.daily_digest()
    assert "Reply to review" in daily.text
    assert "[overdue]" in daily.text
    assert "Finish roadmap" in daily.text
    assert "[today]" in daily.text
    assert "Renew domain" in daily.text
    assert "Later thing" not in daily.text


def test_due_soon_window_boundaries_are_inclusive(vault: Vault, config: Config) -> None:
    """today is not overdue; today+DUE_SOON_DAYS in, today+DUE_SOON_DAYS+1 out."""
    today = NOW.date()
    edge = today + _dt.timedelta(days=DUE_SOON_DAYS)
    beyond = today + _dt.timedelta(days=DUE_SOON_DAYS + 1)
    _action(vault, "today", title="Today", due_date=today.isoformat())
    _action(vault, "edge", title="Edge", due_date=edge.isoformat())
    _action(vault, "beyond", title="Beyond", due_date=beyond.isoformat())

    engine = _engine(vault, config)

    # due == today is not overdue
    assert engine.overdue_actions() == []
    soon_paths = {a.path for a in engine.due_soon_actions()}
    assert "actions/edge.md" in soon_paths
    assert "actions/beyond.md" not in soon_paths
    assert "actions/today.md" not in soon_paths  # today is its own bucket


def test_closed_actions_never_surface(vault: Vault, config: Config) -> None:
    """done/completed/cancelled actions are excluded from every open scan."""
    _action(vault, "done", title="Done", status="done", due_date="2026-05-01")
    _action(
        vault, "completed", title="Completed", status="completed", due_date="2026-05-01"
    )
    _action(
        vault, "cancelled", title="Cancelled", status="cancelled", due_date="2026-06-02"
    )
    _action(vault, "open", title="Open", status="todo", due_date="2026-06-02")

    engine = _engine(vault, config)

    assert {a.status for a in engine.open_actions()} <= ACTION_OPEN_STATUSES
    assert [a.path for a in engine.open_actions()] == ["actions/open.md"]
    assert engine.overdue_actions() == []
    assert [a.path for a in engine.due_soon_actions()] == ["actions/open.md"]


def test_in_progress_counts_as_open(vault: Vault, config: Config) -> None:
    """status in_progress is an open action."""
    _action(vault, "wip", title="WIP", status="in_progress", due_date="2026-06-02")
    engine = _engine(vault, config)
    assert [a.path for a in engine.open_actions()] == ["actions/wip.md"]


def test_action_with_no_due_date_lists_but_never_overdue_or_soon(
    vault: Vault, config: Config
) -> None:
    """A dateless open action is listed by open_actions but not bucketed by date."""
    _action(vault, "no-date", title="No date", status="todo", due_date=None)
    engine = _engine(vault, config)
    assert [a.path for a in engine.open_actions()] == ["actions/no-date.md"]
    assert engine.overdue_actions() == []
    assert engine.due_soon_actions() == []


# --------------------------------------------------------------------------------------
# recent pages (yesterday's ingests), grouped by type
# --------------------------------------------------------------------------------------


def test_recent_pages_days_one_is_yesterday_or_today(
    vault: Vault, config: Config
) -> None:
    """recent_pages(days=1) returns only curated pages updated yesterday or today."""
    _curated(
        vault,
        "concepts",
        "fresh",
        title="Fresh",
        page_type="concept",
        updated="2026-05-31",
    )  # yesterday
    _curated(
        vault,
        "entities",
        "today",
        title="Today",
        page_type="entity",
        updated="2026-06-01",
    )  # today
    _curated(
        vault, "concepts", "old", title="Old", page_type="concept", updated="2026-05-20"
    )  # too old

    engine = _engine(vault, config)
    refs = engine.recent_pages(days=1)
    paths = {r.path for r in refs}
    assert paths == {"concepts/fresh.md", "entities/today.md"}
    assert "concepts/old.md" not in paths


def test_recent_pages_excludes_life_admin_folders(vault: Vault, config: Config) -> None:
    """Only curated folders are scanned as ingests; actions/media churn is excluded."""
    _action(vault, "recent-task", title="Recent task", due_date="2026-06-02")
    _media(vault, "recent-media", title="Recent media", created="2026-05-31")
    _curated(
        vault,
        "concepts",
        "fresh",
        title="Fresh",
        page_type="concept",
        updated="2026-05-31",
    )

    engine = _engine(vault, config)
    paths = {r.path for r in engine.recent_pages(days=7)}
    assert paths == {"concepts/fresh.md"}


def test_recent_pages_grouped_and_counted_by_type_in_daily(
    vault: Vault, config: Config
) -> None:
    """The daily text shows yesterday's ingest count and groups lines by type."""
    _curated(
        vault,
        "concepts",
        "a",
        title="Concept A",
        page_type="concept",
        updated="2026-05-31",
    )
    _curated(
        vault,
        "concepts",
        "b",
        title="Concept B",
        page_type="concept",
        updated="2026-05-31",
    )
    _curated(
        vault,
        "entities",
        "c",
        title="Entity C",
        page_type="entity",
        updated="2026-05-31",
    )

    engine = _engine(vault, config)
    daily = engine.daily_digest()
    assert "INGESTED YESTERDAY (3)" in daily.text
    assert "concept: Concept A" in daily.text
    assert "entity: Entity C" in daily.text
    # type grouping: both concepts precede the entity line in the rendered block.
    assert daily.text.index("Concept A") < daily.text.index("Entity C")


def test_page_with_no_date_excluded_from_recent(vault: Vault, config: Config) -> None:
    """A curated page with no parseable date cannot be placed in the window."""
    _curated(
        vault,
        "concepts",
        "no-dates",
        title="No dates",
        page_type="concept",
        updated=None,
        created=None,
    )
    engine = _engine(vault, config)
    assert engine.recent_pages(days=7) == []


# --------------------------------------------------------------------------------------
# media backlog
# --------------------------------------------------------------------------------------


def test_media_backlog_only_to_consume_oldest_first(
    vault: Vault, config: Config
) -> None:
    """media_backlog returns only to_consume items, oldest-added first."""
    _media(vault, "newest", title="Newest", created="2026-05-30")
    _media(vault, "oldest", title="Oldest", created="2026-05-01")
    _media(vault, "consumed", title="Consumed", status="consumed", created="2026-04-01")

    engine = _engine(vault, config)
    backlog = engine.media_backlog()
    assert [m.path for m in backlog] == ["media/oldest.md", "media/newest.md"]
    assert "media/consumed.md" not in {m.path for m in backlog}


def test_daily_surfaces_media_nudge_with_wikilink(vault: Vault, config: Config) -> None:
    """The daily MEDIA BACKLOG section carries a [[media/...]] wikilink, capped at 2."""
    _media(
        vault,
        "ddia",
        title="Designing Data-Intensive Applications",
        created="2026-05-20",
    )
    _media(vault, "second", title="Second", created="2026-05-21")
    _media(vault, "third", title="Third", created="2026-05-22")

    engine = _engine(vault, config)
    daily = engine.daily_digest()
    assert "MEDIA BACKLOG" in daily.text
    assert "[[media/ddia]]" in daily.text
    # capped at two nudges
    assert "[[media/third]]" not in daily.text


def test_media_item_dataclass_has_no_status_field() -> None:
    """MediaItem stays frontmatter-faithful (no status field on the contract)."""
    item = MediaItem(
        path="media/x.md",
        title="X",
        media_type="book",
        added=None,
        wikilink="[[media/x]]",
    )
    assert not hasattr(item, "status")


# --------------------------------------------------------------------------------------
# review-flagged
# --------------------------------------------------------------------------------------


def test_review_flagged_picks_up_review_true_and_status_review(
    vault: Vault, config: Config
) -> None:
    """review: true OR status: review flags a curated page; plain pages do not."""
    _curated(
        vault,
        "concepts",
        "by-bool",
        title="By bool",
        page_type="concept",
        review="true",
    )
    _curated(
        vault,
        "concepts",
        "by-status",
        title="By status",
        page_type="concept",
        status="review",
    )
    _curated(vault, "concepts", "plain", title="Plain", page_type="concept")

    engine = _engine(vault, config)
    flagged = {r.path for r in engine.review_flagged()}
    assert flagged == {"concepts/by-bool.md", "concepts/by-status.md"}


def test_review_flagged_appears_in_daily(vault: Vault, config: Config) -> None:
    """A flagged page is listed under the FLAGGED FOR REVIEW section."""
    _curated(
        vault,
        "concepts",
        "distributed",
        title="Distributed Systems",
        page_type="concept",
        review="true",
    )
    engine = _engine(vault, config)
    daily = engine.daily_digest()
    assert "FLAGGED FOR REVIEW" in daily.text
    assert "[[distributed]]" in daily.text


# --------------------------------------------------------------------------------------
# weekly digest
# --------------------------------------------------------------------------------------


def test_weekly_counts_status_deadlines_and_review(
    vault: Vault, config: Config
) -> None:
    """Weekly: ingest counts by type, actions status, next-week deadlines, review."""
    _curated(
        vault, "concepts", "c1", title="C1", page_type="concept", updated="2026-05-29"
    )
    _curated(
        vault, "entities", "e1", title="E1", page_type="entity", updated="2026-05-28"
    )
    _action(vault, "soon", title="Soon", due_date="2026-06-05")  # within 7 days
    _action(vault, "overdue", title="Overdue", due_date="2026-05-25")
    _action(vault, "far", title="Far", due_date="2026-06-20")  # outside 7 days
    _curated(
        vault,
        "concepts",
        "flag",
        title="Flag",
        page_type="concept",
        updated="2026-05-29",
        review="true",
    )

    engine = _engine(vault, config)
    weekly = engine.weekly_digest()
    assert weekly.kind == "weekly"
    assert "WEEK IN REVIEW" in weekly.text
    assert "2 concept" in weekly.text  # c1 + flag both updated within 7d
    assert "1 entity" in weekly.text
    assert "ACTIONS STATUS" in weekly.text
    assert "Open actions: 3" in weekly.text
    assert "Overdue: 1" in weekly.text
    assert "NEXT WEEK'S DEADLINES" in weekly.text
    assert "Soon" in weekly.text
    assert "Far" not in weekly.text  # outside 7-day window
    assert "SUGGESTED REVIEW" in weekly.text
    assert "[[flag]]" in weekly.text


def test_weekly_next_week_includes_seven_day_edge(vault: Vault, config: Config) -> None:
    """due_soon_actions(days=7) is inclusive of today+7 and excludes today+8."""
    today = NOW.date()
    edge = today + _dt.timedelta(days=7)
    beyond = today + _dt.timedelta(days=8)
    _action(vault, "edge", title="Edge", due_date=edge.isoformat())
    _action(vault, "beyond", title="Beyond", due_date=beyond.isoformat())
    engine = _engine(vault, config)
    soon = {a.path for a in engine.due_soon_actions(days=7)}
    assert "actions/edge.md" in soon
    assert "actions/beyond.md" not in soon


# --------------------------------------------------------------------------------------
# post() delivery
# --------------------------------------------------------------------------------------


def test_post_calls_slack_once_with_channel_and_text(
    vault: Vault, config: Config
) -> None:
    """post() forwards channel + digest.text to chat_postMessage exactly once."""
    _action(vault, "open", title="Open", due_date="2026-06-02")
    engine = _engine(vault, config)
    digest = engine.daily_digest()
    poster = _FakePoster()

    posted = engine.post(poster, digest, channel="D0B61LKA3NV")
    assert posted is True
    assert len(poster.calls) == 1
    assert poster.calls[0]["channel"] == "D0B61LKA3NV"
    assert poster.calls[0]["text"] == digest.text


def test_post_skips_empty_digest_when_requested(vault: Vault, config: Config) -> None:
    """skip_when_empty + an empty digest returns False and posts nothing."""
    engine = _engine(vault, config)  # empty vault -> empty daily digest
    digest = engine.daily_digest()
    assert digest.is_empty is True

    poster = _FakePoster()
    posted = engine.post(poster, digest, channel="D0", skip_when_empty=True)
    assert posted is False
    assert poster.calls == []


def test_post_empty_digest_still_posts_without_skip_flag(
    vault: Vault, config: Config
) -> None:
    """Without skip_when_empty, even an empty digest is delivered."""
    engine = _engine(vault, config)
    digest = engine.daily_digest()
    poster = _FakePoster()
    assert engine.post(poster, digest, channel="D0") is True
    assert len(poster.calls) == 1


# --------------------------------------------------------------------------------------
# is_empty / header
# --------------------------------------------------------------------------------------


def test_empty_daily_digest_is_empty_and_has_header(
    vault: Vault, config: Config
) -> None:
    """A vault with nothing actionable yields is_empty with the dated header line."""
    engine = _engine(vault, config)
    daily = engine.daily_digest()
    assert daily.is_empty is True
    assert daily.kind == "daily"
    assert "Daily PKM Summary - Mon 2026-06-01 (Europe/London)" in daily.text
    assert "Nothing to report" in daily.text


def test_non_empty_digest_renders_header_line(vault: Vault, config: Config) -> None:
    """Any actionable item makes the digest non-empty and renders the SPEC header."""
    _action(vault, "open", title="Open", due_date="2026-06-02")
    engine = _engine(vault, config)
    daily = engine.daily_digest()
    assert daily.is_empty is False
    assert daily.text.startswith("Daily PKM Summary - Mon 2026-06-01 (Europe/London)")


# --------------------------------------------------------------------------------------
# date parsing robustness
# --------------------------------------------------------------------------------------


def test_date_parsing_accepts_string_date_and_datetime_due(
    vault: Vault, config: Config
) -> None:
    """A YYYY-MM-DD string, a real date object, and a YYYY-MM-DD HH:MM all parse."""
    # string date
    _action(vault, "str-date", title="Str", due_date="2026-06-02")
    # YAML will parse a bare date to a date object; force that via a real date string
    # written without quotes (already a string on disk, parsed by frontmatter to date).
    _write(
        vault,
        "actions/real-date.md",
        {
            "title": "Real",
            "type": "action",
            "created": "2026-05-20",
            "updated": "2026-05-20",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
            "due_date": "2026-06-02",
        },
    )
    # datetime-with-time string
    _action(vault, "dt", title="DT", due_date="2026-06-02 14:30")

    engine = _engine(vault, config)
    due = {a.path: a.due_date for a in engine.open_actions()}
    assert due["actions/str-date.md"] == date(2026, 6, 2)
    assert due["actions/real-date.md"] == date(2026, 6, 2)
    assert due["actions/dt.md"] == date(2026, 6, 2)


def test_malformed_date_treated_as_no_date_never_crashes(
    vault: Vault, config: Config
) -> None:
    """A malformed/empty due_date lists the action but with no due date."""
    _write(
        vault,
        "actions/bad.md",
        {
            "title": "Bad",
            "type": "action",
            "created": "2026-05-20",
            "updated": "2026-05-20",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
            "due_date": "not-a-date",
        },
    )
    _write(
        vault,
        "actions/empty.md",
        {
            "title": "Empty",
            "type": "action",
            "created": "2026-05-20",
            "updated": "2026-05-20",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
            "due_date": "",
        },
    )
    engine = _engine(vault, config)
    open_actions = {a.path: a.due_date for a in engine.open_actions()}
    assert open_actions["actions/bad.md"] is None
    assert open_actions["actions/empty.md"] is None
    # never crashes the digest
    assert engine.daily_digest().kind == "daily"


def test_real_date_object_in_frontmatter_parses(vault: Vault, config: Config) -> None:
    """When YAML yields a real date object (unquoted YYYY-MM-DD), it parses."""
    # python-frontmatter (via PyYAML) parses an unquoted ISO date to datetime.date.
    path = vault.root / "actions" / "yaml-date.md"
    path.write_text(
        "---\n"
        "title: YAML date\n"
        "type: action\n"
        "created: 2026-05-20\n"
        "updated: 2026-05-20\n"
        "source: slack\n"
        "tags: [task]\n"
        "status: todo\n"
        "due_date: 2026-06-03\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    engine = _engine(vault, config)
    due = {a.path: a.due_date for a in engine.open_actions()}
    assert due["actions/yaml-date.md"] == date(2026, 6, 3)


# --------------------------------------------------------------------------------------
# timezone correctness
# --------------------------------------------------------------------------------------


def test_now_coerced_to_london_from_utc(vault: Vault, config: Config) -> None:
    """A UTC now late on a London BST day is bucketed by the London calendar date."""
    # 2026-06-01 23:30 UTC is 2026-06-02 00:30 BST (London is UTC+1 in summer).
    utc_now = datetime(2026, 6, 1, 23, 30, tzinfo=ZoneInfo("UTC"))
    engine = SummaryEngine(config, vault, now=utc_now)
    assert engine.today == date(2026, 6, 2)
    assert engine.now.tzinfo is not None
    # An action due 2026-06-02 is "today" under London, not "tomorrow".
    _action(vault, "june2", title="June 2", due_date="2026-06-02")
    daily = engine.daily_digest()
    assert "due today" in daily.text


def test_naive_now_assumed_london(vault: Vault, config: Config) -> None:
    """A naive now is treated as already London-local (no shift)."""
    engine = SummaryEngine(config, vault, now=datetime(2026, 6, 1, 7, 0))
    assert engine.today == date(2026, 6, 1)
    assert engine.now.tzinfo is not None


def test_default_now_is_used_when_not_injected(vault: Vault, config: Config) -> None:
    """Omitting now uses the current London time (a real date)."""
    engine = SummaryEngine(config, vault)
    assert isinstance(engine.today, date)
    assert engine.now.tzinfo is not None


# --------------------------------------------------------------------------------------
# scan robustness / confinement
# --------------------------------------------------------------------------------------


def test_missing_folder_yields_no_items(tmp_path: Path) -> None:
    """A vault with no actions/ folder yields no actions (no crash)."""
    # Only create the bare root; no subfolders.
    config = load_config({"PKM_VAULT": str(tmp_path)})
    vault = Vault(config)
    engine = SummaryEngine(config, vault, now=NOW)
    assert engine.open_actions() == []
    assert engine.media_backlog() == []
    assert engine.recent_pages(days=7) == []
    assert engine.review_flagged() == []
    assert engine.daily_digest().is_empty is True


def test_unparseable_page_is_skipped(vault: Vault, config: Config) -> None:
    """A file whose frontmatter cannot be parsed is skipped, not fatal."""
    # A real, valid action plus a garbage file in the same folder.
    _action(vault, "good", title="Good", due_date="2026-06-02")
    bad = vault.root / "actions" / "garbage.md"
    bad.write_text("---\n: : : not yaml : :\n---\nbody\n", encoding="utf-8")
    engine = _engine(vault, config)
    paths = {a.path for a in engine.open_actions()}
    # The good action is always present; the garbage file must not crash the scan.
    assert "actions/good.md" in paths


def test_title_falls_back_to_slug(vault: Vault, config: Config) -> None:
    """A page with no title uses its slug as the displayed title."""
    _write(
        vault,
        "actions/no-title.md",
        {
            "type": "action",
            "created": "2026-05-20",
            "updated": "2026-05-20",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
            "due_date": "2026-06-02",
        },
    )
    engine = _engine(vault, config)
    titles = {a.path: a.title for a in engine.open_actions()}
    assert titles["actions/no-title.md"] == "no-title"


def test_action_priority_surfaces(vault: Vault, config: Config) -> None:
    """priority is parsed and rendered on the action line."""
    _action(
        vault, "p", title="Priority task", due_date="2026-06-02", priority="2 - High"
    )
    engine = _engine(vault, config)
    item = engine.open_actions()[0]
    assert item.priority == "2 - High"
    assert "2 - High" in engine.daily_digest().text


# --------------------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------------------


def test_dataclasses_are_frozen() -> None:
    """ActionItem / MediaItem / PageRef / Digest are immutable."""
    action = ActionItem(
        path="actions/x.md",
        title="X",
        status="todo",
        priority=None,
        due_date=None,
        wikilink="[[actions/x]]",
    )
    ref = PageRef(
        path="concepts/x.md",
        title="X",
        page_type="concept",
        updated=None,
        wikilink="[[x]]",
    )
    digest = Digest(kind="daily", title="t", text="b", is_empty=False)
    with pytest.raises(AttributeError):
        action.status = "done"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        ref.title = "Y"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        digest.text = "z"  # type: ignore[misc]


def test_summary_error_is_exception() -> None:
    """SummaryError is a plain Exception subclass."""
    assert issubclass(SummaryError, Exception)


# --------------------------------------------------------------------------------------
# liveness heartbeat (issue #15)
# --------------------------------------------------------------------------------------


def _epoch(when: datetime) -> float:
    """Return the POSIX timestamp for a tz-aware datetime."""
    return when.timestamp()


def test_heartbeat_line_none_without_markers(vault: Vault, config: Config) -> None:
    """With no MarkerStore wired the heartbeat is omitted (returns None)."""
    engine = SummaryEngine(config, vault, now=NOW)
    assert engine.heartbeat_line() is None
    # ...and the daily digest carries no liveness line.
    assert "still alive" not in engine.daily_digest().text


def test_heartbeat_reports_seeded_marker_times(
    vault: Vault, config: Config, tmp_path: Path
) -> None:
    """The daily heartbeat reports last-success times for capture/reindex/push.

    Acceptance (issue #15): with seeded markers and an injected ``now`` the digest's
    "still alive -- last ingest/reindex/push at T" line shows each recorded time.
    """
    markers = MarkerStore(tmp_path / "state.db")
    # Seed three distinct success times (London-local, formatted by the heartbeat).
    markers.record(MARKER_CAPTURE, ts=_epoch(datetime(2026, 6, 1, 6, 5, tzinfo=LONDON)))
    markers.record(
        MARKER_REINDEX, ts=_epoch(datetime(2026, 6, 1, 6, 30, tzinfo=LONDON))
    )
    markers.record(MARKER_PUSH, ts=_epoch(datetime(2026, 6, 1, 6, 50, tzinfo=LONDON)))

    engine = SummaryEngine(config, vault, now=NOW, markers=markers)
    line = engine.heartbeat_line()
    assert line is not None
    assert line.startswith("still alive -- last ")
    assert "ingest 2026-06-01 06:05" in line
    assert "reindex 2026-06-01 06:30" in line
    assert "push 2026-06-01 06:50" in line

    # The line also appears verbatim in the rendered daily digest.
    assert "still alive -- last " in engine.daily_digest().text


def test_heartbeat_reports_never_for_missing_marker(
    vault: Vault, config: Config, tmp_path: Path
) -> None:
    """A stage that never succeeded reads ``never`` so silence is visible."""
    markers = MarkerStore(tmp_path / "state.db")
    markers.record(MARKER_PUSH, ts=_epoch(datetime(2026, 6, 1, 6, 50, tzinfo=LONDON)))
    engine = SummaryEngine(config, vault, now=NOW, markers=markers)
    line = engine.heartbeat_line()
    assert line is not None
    assert "ingest never" in line
    assert "reindex never" in line
    assert "push 2026-06-01 06:50" in line


def test_heartbeat_present_even_on_empty_day(
    vault: Vault, config: Config, tmp_path: Path
) -> None:
    """An otherwise-empty digest still carries the heartbeat (silence is diagnostic).

    The digest stays ``is_empty`` (the heartbeat is plumbing, not news) but the rendered
    text contains both the "Nothing to report" line and the liveness footer.
    """
    markers = MarkerStore(tmp_path / "state.db")
    markers.record(MARKER_PUSH, ts=_epoch(datetime(2026, 5, 20, 6, 0, tzinfo=LONDON)))
    engine = SummaryEngine(config, vault, now=NOW, markers=markers)
    daily = engine.daily_digest()
    assert daily.is_empty is True
    assert "Nothing to report" in daily.text
    # The stale push time (11 days ago) is visible despite the quiet day.
    assert "still alive -- last " in daily.text
    assert "push 2026-05-20 06:00" in daily.text


def test_heartbeat_does_not_make_empty_digest_nonempty(
    vault: Vault, config: Config, tmp_path: Path
) -> None:
    """The heartbeat never flips is_empty (skip-when-empty still skips a quiet day)."""
    markers = MarkerStore(tmp_path / "state.db")
    markers.record(MARKER_CAPTURE)
    engine = SummaryEngine(config, vault, now=NOW, markers=markers)
    assert engine.daily_digest().is_empty is True


def test_heartbeat_survives_marker_read_failure(vault: Vault, config: Config) -> None:
    """A MarkerStore that raises on read does not break the digest (best-effort)."""

    class _BoomMarkers:
        def all(self) -> dict[str, float]:
            raise RuntimeError("db gone")

    engine = SummaryEngine(config, vault, now=NOW, markers=_BoomMarkers())  # type: ignore[arg-type]
    line = engine.heartbeat_line()
    # Every marker reads "never" rather than crashing the daily digest.
    assert line is not None
    assert line.count("never") == 3
