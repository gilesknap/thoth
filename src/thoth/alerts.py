"""Errors-to-Slack: the unattended appliance's only failure signal (issue #15).

thoth runs unattended on an isolated VPS, so a failure nobody sees is a silent failure.
This module is the **errors-to-Slack** surface (SPEC section 10 supervision): a small
:class:`Alerter` that formats an error and posts it to a dedicated Slack target through
the **same injectable** ``chat.postMessage`` seam the rest of the app uses
(:class:`thoth.summary.SlackPoster`). Three callers wire it in: the Slack daemon loop's
top-level handler (:func:`thoth.slack_app.run`), the cron entrypoints
(:func:`thoth.__main__.run_reindex` and ``run_summary``), and the unpushed-divergence
alert (:meth:`Alerter.alert_unpushed_divergence`), raised when a vault commit hits a
rebase conflict (``VaultConflictError`` or ``GitSyncError``) and the push is refused.
Each reports to Slack what would otherwise die into a log file nobody reads.

Design constraints, the same closed-surface rules as the rest of the app:

* Configuration resolves the alert **target**, never a hard-coded id:
  :meth:`thoth.config.Config.alert_target` returns ``SLACK_ALERT_CHANNEL``, or the first
  allow-listed user id as a DM. With neither set the alerter **no-ops** rather than
  raises, because an alert path must not crash the caller.
* Every post is best-effort and **swallows transport errors**, logging through the
  injected logger and returning ``False``, because reporting a failure must never raise
  a *new* failure out of an exception handler.
* ``slack_sdk`` and ``slack_bolt`` are **never** imported at module top level, since CI
  lacks them: :func:`make_alerter` builds the real ``WebClient`` lazily, and the
  testable :class:`Alerter` takes an injected poster.

Module level imports only the standard library, ``thoth._time`` and :mod:`thoth.config`,
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

# Cap the posted traceback, so a runaway exception cannot post a multi-megabyte Slack
# message. The tail holds the actual error line, so keep that end.
_MAX_DETAIL_CHARS: int = 1500


class Alerter:
    """Format and post unattended error and divergence alerts to one Slack target.

    Construct with a resolved ``target``, a channel or DM id, and an injected
    :class:`AlertPoster`. Both are ``None``-safe, so a missing target or poster turns
    every method into a logged no-op and the alert path can never crash the caller. The
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
            target: The Slack channel or DM id to post alerts to, or ``None`` when no
                alert target is configured, which makes every method no-op.
            poster: The injected ``chat.postMessage`` seam, or ``None`` for a no-op.
            clock: A source of the current :class:`~datetime.datetime`, used only to
                stamp an alert. Defaults to :func:`datetime.now` in UTC.
        """
        self._target = target
        self._poster = poster
        self._clock = clock if clock is not None else utc_now

    @property
    def enabled(self) -> bool:
        """``True`` only when a target and a poster are both wired, so alerts post."""
        return self._target is not None and self._poster is not None

    def post(self, text: str) -> bool:
        """Post ``text`` to the alert target, and swallow any transport error.

        Args:
            text: The pre-formatted ``mrkdwn`` alert body.

        Returns:
            ``True`` when a message was posted. ``False`` for a no-op, with no target or
            poster, or when the post raised, which is logged and never re-raised.
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
            where: A short human label for the failing context, such as
                ``"slack daemon"`` or ``"cron: reindex"``.
            exc: The caught exception.

        Returns:
            Whatever :meth:`post` returns.
        """
        return self.post(self._format_exception(where, exc))

    def alert_unpushed_divergence(
        self, *, commits_ahead: int, since: datetime | None, detail: str = ""
    ) -> bool:
        """Post the "N commits unpushed, vault conflict" divergence alert (issue #15).

        A vault commit landed locally but a rebase conflict refused the push, so the
        branch is ahead of the remote and Obsidian holds a conflicting change that needs
        resolving by hand.

        Args:
            commits_ahead: How many local commits are unpushed, from
                ``git rev-list --count`` of local-ahead-of-remote. A negative or unknown
                count reports as "one or more".
            since: The commit time of the oldest unpushed commit, used to say "since T",
                or ``None`` when it could not be determined.
            detail: An optional short tail, such as the conflicting path, appended as
                is.

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

        The first model call that :class:`thoth.budget.BudgetGuard` blocks on a given
        Europe/London day emits this once, so the operator learns the appliance has gone
        fail-safe, deferring captures and aborting reindex, rather than silently burnt
        the cap. The guard's store holds the per-day de-duplication, and this method
        only formats and posts.

        Args:
            day: The Europe/London calendar day the cap was reached on (``YYYY-MM-DD``).
            limit: The configured combined daily call budget.
            breakdown: The per-counter call counts for the alert detail, such as
                ``{"anthropic": 198, "hindsight": 2}``.

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
        """Format the current time, from the injected clock, for an alert line."""
        return _iso(self._clock())


def make_alerter(
    config: Config,
    *,
    poster_factory: Callable[[Config], AlertPoster] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Alerter:
    """Build an :class:`Alerter` from ``config``, resolving the target and the poster.

    :meth:`thoth.config.Config.alert_target` gives the target: ``SLACK_ALERT_CHANNEL``,
    or the first allow-listed user DM. ``poster_factory`` builds the poster **only when
    a target resolves and a bot token is present**, defaulting to a real Slack
    ``WebClient`` builder that imports ``slack_sdk`` lazily. Without either, the
    alerter is a no-op with ``enabled`` ``False``, so a box with no Slack configured
    neither crashes nor posts.

    Args:
        config: The frozen runtime configuration.
        poster_factory: Builds an :class:`AlertPoster` from ``config``, injectable so a
            test never needs the Slack SDK. Defaults to :func:`_make_web_client`.
        clock: Injectable current-time source forwarded to the :class:`Alerter`.

    Returns:
        A wired, or deliberately no-op, :class:`Alerter`.
    """
    target = config.alert_target()
    if target is None or config.slack_bot_token is None:
        return Alerter(target=target, poster=None, clock=clock)
    factory = poster_factory if poster_factory is not None else _make_web_client
    poster = factory(config)
    return Alerter(target=target, poster=poster, clock=clock)


def _make_web_client(config: Config) -> AlertPoster:
    """Build a Slack ``WebClient`` from ``config.slack_bot_token``, with a lazy import.

    ``slack_sdk`` ships with ``slack_bolt``, a runtime-only optional dependency that CI
    lacks, so the import sits here rather than at module top level.
    """
    bot_token, _ = config.require_slack()
    from slack_sdk import WebClient

    return WebClient(token=bot_token)


@contextmanager
def _cron_alerting(where: str, config: Config) -> Iterator[None]:
    """Report a cron-entrypoint crash to the errors-to-Slack target, then re-raise.

    A one-shot cron job that dies writes only to its ``/var/log`` file, which nobody
    watches on an isolated VPS (issue #15). This wraps the job body, so an unhandled
    exception reaches the alert target through :class:`Alerter`, best-effort, before it
    is re-raised: the cron log still records the non-zero exit, and a human still gets
    a Slack message. Building the alerter is itself guarded, because a failure to
    construct it must not mask the original error.

    Args:
        where: A short label for the failing entrypoint, such as ``"cron: reindex"``.
        config: The frozen runtime config, which resolves the alert target and bot
            token.

    Yields:
        ``None``. The caller runs its job body inside the ``with`` block.
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
    """Format ``when`` as a compact ``YYYY-MM-DD HH:MMZ`` string."""
    if when.tzinfo is None:
        return when.strftime("%Y-%m-%d %H:%M")
    return when.strftime("%Y-%m-%d %H:%M %Z").strip()


def _tail(text: str, limit: int) -> str:
    """Return at most the last ``limit`` chars of ``text``, keeping the error line."""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]
