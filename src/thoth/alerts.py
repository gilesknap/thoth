"""Errors-to-Slack, the unattended appliance's only failure signal (issue #15).

thoth runs unattended on an isolated VPS, so a failure nobody sees is a silent failure.
This is the errors-to-Slack surface (SPEC section 10), posting through the same
injectable ``chat.postMessage`` seam the rest of the app uses. It is wired into:

* the Slack daemon's top-level handler, so an unhandled exception is reported before the
  process exits and systemd restarts it;
* the cron entrypoints, so a reindex or summary crash surfaces in Slack rather than
  dying into a log file nobody reads;
* the unpushed-divergence alert, raised when a vault commit hits a rebase conflict and
  the push is refused. It reports how many commits are unpushed and since when, with the
  count computed from git.

Three constraints hold, matching the rest of the app:

* The target comes from configuration and is never a hard-coded id. With none set the
  alerter no-ops rather than raising, because an alert path must not crash its caller.
* Every post is best-effort and swallows transport errors, because reporting a failure
  must never raise a new one out of an exception handler.
* ``slack_sdk`` is never imported at module level, since it is absent in CI, so the real
  client is built lazily only when a target is configured.

Only the standard library plus ``thoth._time`` and :mod:`thoth.config` is imported here,
so this module is always import-safe under pytest collection.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from thoth._time import utc_now
from thoth.config import Config
from thoth.render import SlackPoster as AlertPoster

__all__ = ["AlertPoster", "Alerter", "make_alerter"]

_LOG = logging.getLogger("thoth.alerts")

# Cap how much of a traceback is posted so a runaway exception cannot post a
# multi-megabyte message. The tail is kept, since that carries the actual error line
_MAX_DETAIL_CHARS: int = 1500


class Alerter:
    """Formats and posts unattended error and divergence alerts to one target.

    Both the target and the poster are None-safe. A missing one turns every method into
    a logged no-op, so the alert path can never crash its caller. The clock is
    injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        target: str | None,
        poster: AlertPoster | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Stores the resolved target, the delivery seam, and the clock.

        Args:
            target: Channel or DM id to post to, or None to make every method no-op.
            poster: The injected ``chat.postMessage`` seam, or None to no-op.
            clock: Current-time source used only to stamp an alert, defaulting to
                UTC now.
        """
        self._target = target
        self._poster = poster
        self._clock = clock if clock is not None else utc_now

    @property
    def enabled(self) -> bool:
        """True when a target and a poster are both wired, so alerts will post."""
        return self._target is not None and self._poster is not None

    def post(self, text: str) -> bool:
        """Posts text to the alert target, swallowing any transport error.

        Args:
            text: The pre-formatted alert body.

        Returns:
            True when a message was posted. False when it was a no-op or the post
            raised, in which case the error is logged and never re-raised.
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
        """Formats and posts an unhandled-exception alert from one context.

        Args:
            where: Short label for the failing context, such as "cron: reindex".
            exc: The caught exception.

        Returns:
            Whatever :meth:`post` returns.
        """
        return self.post(self._format_exception(where, exc))

    def alert_unpushed_divergence(
        self, *, commits_ahead: int, since: datetime | None, detail: str = ""
    ) -> bool:
        """Posts the "N commits unpushed" vault-conflict alert (issue #15).

        Raised when a commit landed locally but the push was refused by a rebase
        conflict, so the branch is ahead of the remote and Obsidian holds a conflicting
        change that must be resolved by hand.

        Args:
            commits_ahead: Unpushed local commits. A negative count reads as "one or
                more".
            since: Time of the oldest unpushed commit, or None when unknown.
            detail: Optional short tail such as the conflicting path, appended as-is.

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
        """Posts the one-per-day "daily LLM budget reached" alert (issue #16).

        Emitted once, by the first call the guard blocks on a given London day, so the
        operator learns the appliance has gone fail-safe rather than silently burning
        the cap. The per-day de-duplication lives in the guard's store, and this only
        formats and posts.

        Args:
            day: The London calendar day the cap was reached on.
            limit: The configured combined daily call budget.
            breakdown: Per-counter call counts, for the alert detail.

        Returns:
            Whatever :meth:`post` returns.
        """
        return self.post(self._format_budget(day=day, limit=limit, breakdown=breakdown))

    # ---- formatting (pure, total) ------------------------------------------------

    def _format_budget(self, *, day: str, limit: int, breakdown: dict[str, int]) -> str:
        """Renders the daily-budget alert as a compact block."""
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
        """Renders an unhandled-exception alert as a compact block."""
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
        """Renders the unpushed-divergence alert."""
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
        """Formats the current time from the injected clock for an alert line."""
        return _iso(self._clock())


def make_alerter(
    config: Config,
    *,
    poster_factory: Callable[[Config], AlertPoster] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Alerter:
    """Builds an alerter from config, resolving the target and the poster.

    The poster is built only when a target resolves and a bot token is present. With
    neither, the returned alerter is a deliberate no-op, so a box without Slack
    configured neither crashes nor posts.

    Args:
        config: The frozen runtime configuration.
        poster_factory: Builds the poster from config, injectable so a test never
            needs the Slack SDK.
        clock: Current-time source forwarded to the alerter.

    Returns:
        A wired, or deliberately no-op, alerter.
    """
    target = config.alert_target()
    if target is None or config.slack_bot_token is None:
        return Alerter(target=target, poster=None, clock=clock)
    factory = poster_factory if poster_factory is not None else _make_web_client
    poster = factory(config)
    return Alerter(target=target, poster=poster, clock=clock)


def _make_web_client(config: Config) -> AlertPoster:
    """Builds a Slack client from the configured bot token, importing lazily.

    ``slack_sdk`` ships with ``slack_bolt``, a runtime-only dependency absent in CI, so
    it is imported here and never at module level.
    """
    bot_token, _ = config.require_slack()
    from slack_sdk import WebClient

    return WebClient(token=bot_token)


@contextmanager
def _cron_alerting(where: str, config: Config) -> Iterator[None]:
    """Reports a cron crash to the errors-to-Slack target, then re-raises.

    A one-shot cron job that dies only writes to a log file nobody watches on an
    isolated VPS (issue #15). Wrapping the body means the exception is posted before
    being re-raised, so the cron log still records the non-zero exit and a human gets a
    message. Building the alerter is itself guarded, because failing to construct it
    must not mask the original error.

    Args:
        where: Short label for the failing entrypoint.
        config: Frozen runtime config, resolving the target and bot token.

    Yields:
        None, with the caller running its job body inside the block.
    """
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - report ANY crash, then re-raise
        try:
            make_alerter(config).alert_exception(where, exc)
        except Exception:  # noqa: BLE001 - alerting must never mask the real error
            pass
        raise


def _iso(when: datetime) -> str:
    """Formats a time as ``YYYY-MM-DD HH:MM``, with the zone when one is set."""
    if when.tzinfo is None:
        return when.strftime("%Y-%m-%d %H:%M")
    return when.strftime("%Y-%m-%d %H:%M %Z").strip()


def _tail(text: str, limit: int) -> str:
    """Returns the last ``limit`` characters, marking a truncation with ``...``."""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]
