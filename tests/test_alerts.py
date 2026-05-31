"""Tests for :mod:`thoth.alerts` -- the errors-to-Slack surface (issue #15).

These exercise the :class:`~thoth.alerts.Alerter` formatting + delivery and the
:func:`~thoth.alerts.make_alerter` target/poster resolution with a tiny recording fake
:class:`_RecordingPoster` (no ``slack_sdk`` is ever imported). Every acceptance the
issue names is covered: a configured target posts a visible alert; a missing target /
poster no-ops; a poster that raises is swallowed (an alert must never crash its caller);
and the unpushed-divergence alert reports the commit count + "since T".
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime
from typing import Any

from thoth.alerts import Alerter, make_alerter
from thoth.config import load_config

UTC = _dt.UTC
FROZEN = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


class _RecordingPoster:
    """A fake Slack client recording every ``chat.postMessage`` call."""

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[tuple[str, str]] = []

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Record ``(channel, text)`` and return an ok-ish response."""
        self.calls.append((channel, text))
        return {"ok": True}


class _RaisingPoster:
    """A fake Slack client whose post always raises (transport failure)."""

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Always raise to simulate a Slack transport error."""
        raise RuntimeError("slack is down")


def _alerter(poster: Any, target: str | None = "C-ALERT") -> Alerter:
    """Build an Alerter with a frozen clock over ``poster``/``target``."""
    return Alerter(target=target, poster=poster, clock=lambda: FROZEN)


# --- posting + no-op semantics ----------------------------------------------------


def test_post_delivers_to_configured_target() -> None:
    """A wired alerter posts the text to its target and reports success."""
    poster = _RecordingPoster()
    alerter = _alerter(poster)
    assert alerter.enabled is True
    assert alerter.post("hello") is True
    assert poster.calls == [("C-ALERT", "hello")]


def test_post_is_noop_without_target() -> None:
    """No target -> no post, returns False, and is not enabled (does not raise)."""
    poster = _RecordingPoster()
    alerter = _alerter(poster, target=None)
    assert alerter.enabled is False
    assert alerter.post("hello") is False
    assert poster.calls == []


def test_post_is_noop_without_poster() -> None:
    """No poster -> no post, returns False (the box has no Slack configured)."""
    alerter = Alerter(target="C-ALERT", poster=None, clock=lambda: FROZEN)
    assert alerter.enabled is False
    assert alerter.post("hello") is False


def test_post_swallows_transport_error() -> None:
    """A poster that raises is swallowed: post returns False, never re-raises."""
    alerter = _alerter(_RaisingPoster())
    # Must not raise -- reporting a failure must never raise a NEW failure.
    assert alerter.post("hello") is False


# --- exception alerts -------------------------------------------------------------


def test_alert_exception_formats_context_kind_and_message() -> None:
    """An exception alert names the context, the type, and the message."""
    poster = _RecordingPoster()
    alerter = _alerter(poster)
    try:
        raise ValueError("quota exhausted")
    except ValueError as exc:
        assert alerter.alert_exception("slack daemon", exc) is True
    _, text = poster.calls[0]
    assert "slack daemon" in text
    assert "ValueError" in text
    assert "quota exhausted" in text
    # The frozen stamp appears so the alert is timestamped.
    assert "2026-05-30 12:00" in text


def test_alert_exception_truncates_a_huge_traceback() -> None:
    """A runaway message is truncated so the Slack post stays bounded."""
    poster = _RecordingPoster()
    alerter = _alerter(poster)
    big = "x" * 50_000
    try:
        raise RuntimeError(big)
    except RuntimeError as exc:
        alerter.alert_exception("cron: reindex", exc)
    _, text = poster.calls[0]
    assert len(text) < 5_000
    # The tail (the truncation marker) is present.
    assert "..." in text


# --- unpushed-divergence alert ----------------------------------------------------


def test_alert_unpushed_divergence_reports_count_and_since() -> None:
    """The divergence alert names N commits unpushed and the 'since T' time."""
    poster = _RecordingPoster()
    alerter = _alerter(poster)
    since = datetime(2026, 5, 29, 8, 30, tzinfo=UTC)
    assert (
        alerter.alert_unpushed_divergence(
            commits_ahead=3, since=since, detail="entities/x.md"
        )
        is True
    )
    _, text = poster.calls[0]
    assert "3 commits unpushed" in text
    assert "since 2026-05-29 08:30" in text
    assert "Obsidian" in text
    assert "entities/x.md" in text


def test_alert_unpushed_divergence_singular_and_unknown_count() -> None:
    """One commit reads singular; an unknown (-1) count reads 'one or more'."""
    poster = _RecordingPoster()
    alerter = _alerter(poster)
    alerter.alert_unpushed_divergence(commits_ahead=1, since=None)
    alerter.alert_unpushed_divergence(commits_ahead=-1, since=None)
    assert "1 commit unpushed" in poster.calls[0][1]
    assert "commits unpushed" not in poster.calls[0][1]
    assert "one or more commits unpushed" in poster.calls[1][1]


def test_alert_budget_exceeded_reports_day_limit_and_breakdown() -> None:
    """The daily-budget alert names the day, cap, and per-counter split (#16)."""
    poster = _RecordingPoster()
    alerter = _alerter(poster)
    assert (
        alerter.alert_budget_exceeded(
            day="2026-06-01", limit=200, breakdown={"anthropic": 198, "hindsight": 2}
        )
        is True
    )
    channel, text = poster.calls[0]
    assert channel == "C-ALERT"
    assert "2026-06-01" in text
    assert "200-call cap" in text
    assert "198 anthropic" in text
    assert "2 hindsight" in text
    assert "fail-safe" in text


# --- make_alerter resolution ------------------------------------------------------


def test_make_alerter_uses_alert_channel_and_injected_poster() -> None:
    """make_alerter resolves SLACK_ALERT_CHANNEL and uses the injected poster build."""
    poster = _RecordingPoster()
    config = load_config(
        {
            "PKM_VAULT": "/tmp/v",
            "SLACK_ALERT_CHANNEL": "C-DEDICATED",
            "SLACK_BOT_TOKEN": "test-token",
        }
    )
    alerter = make_alerter(config, poster_factory=lambda _c: poster)
    assert alerter.enabled is True
    alerter.post("ping")
    assert poster.calls == [("C-DEDICATED", "ping")]


def test_make_alerter_falls_back_to_allow_listed_dm() -> None:
    """With no alert channel, make_alerter posts to the first allow-listed user DM."""
    poster = _RecordingPoster()
    config = load_config(
        {
            "PKM_VAULT": "/tmp/v",
            "SLACK_ALLOWED_USERS": "<@U-ME|giles>, U-OTHER",
            "SLACK_BOT_TOKEN": "test-token",
        }
    )
    alerter = make_alerter(config, poster_factory=lambda _c: poster)
    alerter.post("ping")
    assert poster.calls == [("U-ME", "ping")]


def test_make_alerter_is_noop_without_target() -> None:
    """No alert channel and no allow-list -> a deliberately disabled alerter."""
    config = load_config({"PKM_VAULT": "/tmp/v", "SLACK_BOT_TOKEN": "test-token"})
    called = {"n": 0}

    def factory(_c: Any) -> _RecordingPoster:
        called["n"] += 1
        return _RecordingPoster()

    alerter = make_alerter(config, poster_factory=factory)
    assert alerter.enabled is False
    # The poster factory is never even invoked when there is no target.
    assert called["n"] == 0


def test_make_alerter_is_noop_without_bot_token() -> None:
    """A target but no bot token -> disabled (cannot build a real client)."""
    config = load_config({"PKM_VAULT": "/tmp/v", "SLACK_ALERT_CHANNEL": "C-X"})
    alerter = make_alerter(config, poster_factory=lambda _c: _RecordingPoster())
    assert alerter.enabled is False
