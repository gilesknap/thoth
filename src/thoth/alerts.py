"""Errors-to-Slack: the unattended appliance's only failure signal (issue #15).

thoth runs unattended on an isolated VPS, so a failure that no human sees is a silent
failure. This module is the **errors-to-Slack** surface (SPEC section 10 supervision):
a small :class:`Alerter` that formats and posts an error / alert to a dedicated Slack
target via the **same injectable** ``chat.postMessage`` seam the rest of the app uses
(:class:`thoth.summary.SlackPoster`). It is wired into:

* the top-level handler of the Slack daemon loop (:func:`thoth.slack_app.run`), so an
  unhandled daemon exception is reported before the process exits and systemd restarts
  it;
* the cron entrypoints (:func:`thoth.__main__.run_reindex` / ``run_summary``), so a
  reindex / summary crash surfaces in Slack instead of dying only into a log file; and
* the unpushed-divergence alert (:meth:`Alerter.alert_unpushed_divergence`) raised when
  a vault commit hits a rebase conflict (``VaultConflictError`` / ``GitSyncError``) and
  the push is refused -- it reports "N commits unpushed since T -- vault conflict needs
  resolving in Obsidian", with N computed from git.

Design constraints (the same closed-surface rules as the rest of the app):

* The alert **target** is resolved from configuration -- :meth:`thoth.config.Config.
  alert_target` returns ``SLACK_ALERT_CHANNEL`` or, failing that, the first allow-listed
  user id as a DM target -- never a hard-coded id. When neither is set the alerter
  **no-ops** rather than raising: an alert path must not itself crash the caller.
* Every post is best-effort and **swallows transport errors** (a failed alert post is
  logged via the injected logger and returns ``False``); reporting a failure must never
  raise a *new* failure out of an exception handler.
* ``slack_sdk`` / ``slack_bolt`` are **never** imported at module top level (absent in
  CI). The real Slack ``WebClient`` is built lazily by :func:`make_alerter` only when a
  target is configured; the testable :class:`Alerter` logic takes an injected poster.

Only the standard library plus :mod:`thoth.config` is imported at module level, so this
module is always import-safe under pytest collection.
"""

from __future__ import annotations

import datetime as _dt
import logging
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from thoth.config import Config

__all__ = ["AlertPoster", "Alerter", "make_alerter"]

_LOG = logging.getLogger("thoth.alerts")

# Cap how much of a formatted traceback / message is posted so a runaway exception
# cannot post a multi-megabyte Slack message; the tail (the actual error line) is kept.
_MAX_DETAIL_CHARS: int = 1500


class AlertPoster(Protocol):
    """The ``chat.postMessage`` slice used to deliver an alert.

    Identical in shape to :class:`thoth.summary.SlackPoster` and
    :class:`thoth.slack_app.SlackClientLike`; the real Bolt ``WebClient`` and a test
    fake both satisfy it, so :class:`Alerter` never imports a Slack SDK.
    """

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post ``text`` to ``channel`` (the Slack ``chat.postMessage`` API)."""
        ...


class Alerter:
    """Format and post unattended error / divergence alerts to one Slack target.

    Construct with a resolved ``target`` (a channel or DM id) and an injected
    :class:`AlertPoster`; both are ``None``-safe -- a missing target or poster turns
    every method into a logged no-op so the alert path can never crash the caller. The
    clock is injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        target: str | None,
        poster: AlertPoster | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Store the resolved target, the delivery seam, and the clock.

        Args:
            target: The Slack channel / DM id to post alerts to, or ``None`` when no
                alert target is configured (every method then no-ops).
            poster: The injected ``chat.postMessage`` seam, or ``None`` (no-op).
            clock: A source of the current :class:`~datetime.datetime` used only to
                stamp an alert; defaults to :func:`datetime.now` in UTC.
        """
        self._target = target
        self._poster = poster
        self._clock = clock if clock is not None else _utc_now

    @property
    def enabled(self) -> bool:
        """``True`` iff a target and a poster are both wired (alerts will be posted)."""
        return self._target is not None and self._poster is not None

    def post(self, text: str) -> bool:
        """Post ``text`` to the alert target, swallowing any transport error.

        Args:
            text: The pre-formatted ``mrkdwn`` alert body.

        Returns:
            ``True`` if a message was posted, ``False`` if it was a no-op (no target /
            poster) or the post raised (the error is logged, never re-raised).
        """
        if self._target is None or self._poster is None:
            _LOG.debug("alert suppressed (no target/poster configured): %s", text)
            return False
        try:
            self._poster.chat_postMessage(channel=self._target, text=text)
        except Exception:  # noqa: BLE001 - an alert post must never raise onward
            _LOG.exception("failed to post alert to Slack target %r", self._target)
            return False
        return True

    def alert_exception(self, where: str, exc: BaseException) -> bool:
        """Format and post an unhandled-exception alert from context ``where``.

        Args:
            where: A short human label for the failing context (e.g. ``"slack daemon"``
                or ``"cron: reindex"``).
            exc: The caught exception.

        Returns:
            Whatever :meth:`post` returns.
        """
        return self.post(self._format_exception(where, exc))

    def alert_unpushed_divergence(
        self, *, commits_ahead: int, since: datetime | None, detail: str = ""
    ) -> bool:
        """Post the "N commits unpushed -- vault conflict" divergence alert (issue #15).

        Raised when a vault commit landed locally but the push was refused by a rebase
        conflict, so the local branch is ahead of the remote and Obsidian holds a
        conflicting change that must be resolved by hand.

        Args:
            commits_ahead: How many local commits are unpushed (``git rev-list --count``
                of local-ahead-of-remote); a negative/unknown count is reported as
                "one or more".
            since: The author/commit time of the oldest unpushed commit, used to say
                "since T"; ``None`` when it could not be determined.
            detail: An optional short tail (e.g. the conflicting path), appended as-is.

        Returns:
            Whatever :meth:`post` returns.
        """
        return self.post(
            self._format_unpushed(
                commits_ahead=commits_ahead, since=since, detail=detail
            )
        )

    def alert_budget_exceeded(
        self, *, day: str, limit: int, breakdown: dict[str, int]
    ) -> bool:
        """Post the one-per-day "daily LLM budget reached" alert (issue #16).

        Emitted once, by the first model call that the :class:`thoth.budget.BudgetGuard`
        blocks on a given Europe/London day, so the operator learns the appliance has
        gone fail-safe (deferring captures, aborting reindex) rather than silently
        burning the cap. The per-day de-duplication lives in the guard's store; this
        method just formats and posts.

        Args:
            day: The Europe/London calendar day the cap was reached on (``YYYY-MM-DD``).
            limit: The configured combined daily call budget.
            breakdown: The per-counter call counts (e.g. ``{"anthropic": 198,
                "hindsight": 2}``) for the alert detail.

        Returns:
            Whatever :meth:`post` returns.
        """
        return self.post(self._format_budget(day=day, limit=limit, breakdown=breakdown))

    # ---- formatting (pure, total) ------------------------------------------------

    def _format_budget(self, *, day: str, limit: int, breakdown: dict[str, int]) -> str:
        """Render the daily-budget alert as a compact ``mrkdwn`` block."""
        stamp = self._stamp()
        detail = (
            ", ".join(f"{count} {kind}" for kind, count in sorted(breakdown.items()))
            or "no calls recorded"
        )
        return (
            f":money_with_wings: *Daily LLM budget reached* ({day}) at {stamp} - "
            f"the {limit}-call cap is spent ({detail}). thoth is now fail-safe: "
            f"captures are held raw and re-curated later, and reindex is deferred "
            f"until the next Europe/London day."
        )

    def _format_exception(self, where: str, exc: BaseException) -> str:
        """Render an unhandled-exception alert as a compact ``mrkdwn`` block."""
        stamp = self._stamp()
        kind = type(exc).__name__
        summary = _tail(str(exc).strip() or kind, _MAX_DETAIL_CHARS)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tb = _tail(tb.strip(), _MAX_DETAIL_CHARS)
        return (
            f":rotating_light: *thoth alert* ({where}) at {stamp}\n"
            f"*{kind}*: {summary}\n"
            f"```\n{tb}\n```"
        )

    def _format_unpushed(
        self, *, commits_ahead: int, since: datetime | None, detail: str
    ) -> str:
        """Render the unpushed-divergence alert as ``mrkdwn``."""
        stamp = self._stamp()
        n = str(commits_ahead) if commits_ahead >= 0 else "one or more"
        plural = "" if commits_ahead == 1 else "s"
        when = f" since {_iso(since)}" if since is not None else ""
        tail = f"\n{_tail(detail.strip(), _MAX_DETAIL_CHARS)}" if detail.strip() else ""
        return (
            f":warning: *Vault conflict* at {stamp} - {n} commit{plural} unpushed"
            f"{when}. Resolve the conflict in Obsidian (pull, fix, push) so the "
            f"appliance can sync again.{tail}"
        )

    def _stamp(self) -> str:
        """Format the current time (from the injected clock) for an alert line."""
        return _iso(self._clock())


def make_alerter(
    config: Config,
    *,
    poster_factory: Callable[[Config], AlertPoster] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Alerter:
    """Build an :class:`Alerter` from ``config``, resolving the target + the poster.

    The target is :meth:`thoth.config.Config.alert_target` (``SLACK_ALERT_CHANNEL`` or
    the first allow-listed user DM). The poster is built **only when a target resolves
    and a bot token is present**, via ``poster_factory`` (defaults to a real Slack
    ``WebClient`` builder that imports ``slack_sdk`` lazily). With no target -- or no
    bot token -- the returned alerter is a no-op (``enabled`` is ``False``), so a box
    without Slack configured neither crashes nor posts.

    Args:
        config: The frozen runtime configuration.
        poster_factory: Builds an :class:`AlertPoster` from ``config``; injectable so a
            test never needs the Slack SDK. Defaults to :func:`_make_web_client`.
        clock: Injectable current-time source forwarded to the :class:`Alerter`.

    Returns:
        A wired (or deliberately no-op) :class:`Alerter`.
    """
    target = config.alert_target()
    if target is None or config.slack_bot_token is None:
        return Alerter(target=target, poster=None, clock=clock)
    factory = poster_factory if poster_factory is not None else _make_web_client
    poster = factory(config)
    return Alerter(target=target, poster=poster, clock=clock)


def _make_web_client(config: Config) -> AlertPoster:
    """Build a Slack ``WebClient`` from ``config.slack_bot_token`` (lazy import).

    ``slack_sdk`` ships with ``slack_bolt`` (a runtime-only optional dependency absent
    in CI), so it is imported here, never at module top level.
    """
    bot_token, _ = config.require_slack()
    from slack_sdk import WebClient

    return WebClient(token=bot_token)


def _utc_now() -> datetime:
    """Return the current UTC time (the default alert clock)."""
    return datetime.now(_dt.UTC)


def _iso(when: datetime) -> str:
    """Format ``when`` as a compact ``YYYY-MM-DD HH:MMZ``-style string."""
    if when.tzinfo is None:
        return when.strftime("%Y-%m-%d %H:%M")
    return when.strftime("%Y-%m-%d %H:%M %Z").strip()


def _tail(text: str, limit: int) -> str:
    """Return at most the last ``limit`` chars of ``text`` (keeping the error line)."""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]
