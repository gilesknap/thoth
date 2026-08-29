"""The allow-list parser, routing/gating :class:`Handlers`, and the daemon wiring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from thoth.budget import BudgetExceededError
from thoth.config import Config, _strip_user_token
from thoth.git_sync import GitSync, GitSyncError, VaultConflictError
from thoth.ingest import Capture, IngestError, Ingestor
from thoth.intent import IntentClassifier, IntentDecision
from thoth.query import QueryEngine, QueryError
from thoth.state import EventStore

from .dedupe import EventDedupe
from .events import (
    _capture_body,
    _channel,
    _conversation_key,
    _event_key,
    _looks_like_url,
    _should_handle,
)
from .files import _download_to_tmp, _is_image_file
from .rendering import render_ingest_report, render_query_result
from .responder import (
    _ASK_PLACEHOLDER,
    _INGEST_PLACEHOLDER,
    Responder,
    SlackClientLike,
)

logger = logging.getLogger("thoth.slack_app")

# The safe routing verdict when no classifier is wired. Route to the vault-only query
# with no keywords, so the read path greps the raw text (issues #5 and #102)
_QUERY_FALLBACK_DECISION: IntentDecision = IntentDecision(
    intent="query", confidence="high"
)

# The polite refusal sent to a user who is not on the allow-list
_REFUSAL_TEXT: str = "Sorry, you are not authorised to use this assistant."

# Appended when the intent gate (issue #5) routed bare free text to capture, so a
# misfile is recoverable in one reply by re-sending it as a question. Explicit prefix,
# URL and file captures never carry this, they are unambiguous
_GATE_CAPTURE_HINT: str = (
    "_Filed as a note. If you meant to ask, just send it again as a question._"
)

# Rendered on the read paths when the daily LLM budget (issue #16) is spent. Captures
# still file durably and defer curation, but an answer needs a live model call, so say
# the cap is reached rather than leave the user without a reply
_BUDGET_REACHED_TEXT: str = (
    ":money_with_wings: Daily LLM budget reached - answering is paused until tomorrow "
    "(Europe/London). Anything you capture is still saved and will be processed later."
)

# The vault source value for Slack-originated captures
_SOURCE: str = "slack"


def parse_allowed_users(raw: str | None) -> frozenset[str]:
    """Parses ``SLACK_ALLOWED_USERS`` into a set of bare Slack user ids.

    Accepts a comma or whitespace separated list. Each token is trimmed of the ``@`` and
    ``<@U...>`` mention wrappers Slack sometimes adds, so ``"<@U1>, @U2  U3"`` yields
    ``{"U1", "U2", "U3"}``.

    A blank value yields an empty set, which denies everyone. That is deliberate and
    fail-closed.

    Args:
        raw: The raw environment value, or None when unset.

    Returns:
        The normalised user ids.
    """
    if not raw:
        return frozenset()
    tokens: list[str] = []
    for piece in raw.replace(",", " ").split():
        token = _strip_user_token(piece)
        if token:
            tokens.append(token)
    return frozenset(tokens)


class AlerterLike(Protocol):
    """The slice of :class:`thoth.alerts.Alerter` the daemon and handlers use.

    Keeps :mod:`thoth.slack_app` decoupled from :mod:`thoth.alerts`, with no hard import
    for the type, so a test can inject a fake that records the alerts posted.
    """

    def alert_exception(self, where: str, exc: BaseException) -> bool:
        """Formats and posts an unhandled-exception alert."""
        ...

    def alert_unpushed_divergence(
        self, *, commits_ahead: int, since: datetime | None, detail: str = ...
    ) -> bool:
        """Posts the "N commits unpushed" vault-conflict divergence alert."""
        ...


@dataclass
class Handlers:
    """Pure Slack handler logic with all collaborators injected.

    Holds the ingestor, the query engine, the parsed allow-list and the transient
    dedupe. Every method is unit-testable with fakes, so exercising the routing, gating
    and rendering needs no live socket and no ``slack_bolt`` import.
    """

    config: Config
    ingestor: Ingestor
    query_engine: QueryEngine
    allowed_users: frozenset[str]
    intent_classifier: IntentClassifier | None = None
    dedupe: EventDedupe = field(default_factory=EventDedupe)
    alerter: AlerterLike | None = None
    git: GitSync | None = None
    capture_channel: str = ""

    def is_allowed(self, user_id: str) -> bool:
        """Reports whether a user is on the allow-list, failing closed."""
        return bool(user_id) and user_id in self.allowed_users

    def handle_message(
        self,
        event: dict[str, Any],
        say: Callable[..., None],
        client: SlackClientLike | None = None,
    ) -> None:
        """Gates, routes and replies to a channel message event (issue #61).

        Messages outside the dedicated capture channel are ignored, so the bot never
        reacts in other conversations it was invited to. An empty capture channel
        disables the gate, which only relaxes the test path because the daemon enforces
        the setting at startup.

        Bot and own messages and the edit and join subtypes are ignored, so the daemon
        does not loop on its own replies.

        Replies are posted in the message's thread and per-conversation state is keyed
        by that thread. The allow-list and the redelivery dedupe are enforced here.
        Routing follows SPEC section 6:

        * a **file upload**, a message with the ``file_share`` subtype, downloads and
          ingests every attached file. That event carries the full file objects and a
          usable channel, unlike the bare ``file_shared`` event the appliance ignores.
        * a **bare URL**, or text with a ``capture:``, ``note:`` or ``save:`` prefix, is
          an ingest.
        * any other **bare free text** goes through the intent gate (issue #5), where an
          injected classifier chooses capture or vault-query. With no classifier wired
          the safe vault-only answer holds.

        A surfaced :class:`~thoth.git_sync.VaultConflictError` is rendered fail-loud
        rather than swallowed.

        Args:
            event: The Slack event payload.
            say: Callable posting a reply, accepting an optional ``thread_ts``.
            client: Slack web client for downloading an upload's bytes, or None on
                the text-only paths, which never touch it.
        """
        if not _should_handle(event):
            return
        channel = _channel(event)
        if self.capture_channel and channel != self.capture_channel:
            # Not our dedicated capture channel, so ignore it with no refusal and no
            # work
            return
        thread = _conversation_key(event)
        responder = Responder(say, client=client, channel=channel, thread_ts=thread)
        user = str(event.get("user", ""))
        if not self.is_allowed(user):
            # Operator-readable refusal (issue #52) naming the rejected id and the
            # allow-list size. A size of 0 is the fail-closed deny-everyone case, where
            # SLACK_ALLOWED_USERS is unset or never reached this process, and is the
            # usual cause of an unexpected "not authorised"
            logger.info(
                "slack refused message from user %r (allow-list has %d id(s))",
                user,
                len(self.allowed_users),
            )
            responder.say(_REFUSAL_TEXT)
            return
        if self.dedupe.seen(_event_key(event)):
            return

        if event.get("subtype") == "file_share":
            self._ingest_uploaded_files(event, client, responder)
            return

        text = str(event.get("text", "")).strip()
        if not text:
            return
        body = _capture_body(text)
        if body is not None:
            capture = Capture(text=body, source=_SOURCE)
            self._do_ingest(capture, responder)
        elif _looks_like_url(text):
            capture = Capture(url=text, source=_SOURCE)
            self._do_ingest(capture, responder)
        else:
            self._route_free_text(text, _SOURCE, responder)

    def _ingest_uploaded_files(
        self,
        event: dict[str, Any],
        client: SlackClientLike | None,
        responder: Responder,
    ) -> None:
        """Downloads and ingests the files on a ``file_share`` message.

        This event carries the full file objects, each with a private download URL and
        name, plus a usable channel to reply in. Each file is downloaded server-side to
        a temporary path, never base64. A missing URL or a failed download is surfaced
        fail-loud per file, so the rest still ingest.

        A message attaching several images at once is one unit of intent (issue #84), so
        an all-image batch becomes one capture with one summary and one tag set. A
        heterogeneous batch is still ingested per file, because per-type classification
        of mixed kinds is deferred.

        A caption typed alongside the upload (issue #130) rides on the capture as its
        text, so it reaches the model alongside the file's own analysis. The caption
        augments the image content rather than replacing it, and a batch shares the one
        caption. A capture prefix in a caption is left verbatim, because a file_share is
        always a capture.
        """
        files = event.get("files")
        if not isinstance(files, list) or not files:
            responder.say(":warning: That upload carried no files I could read.")
            return
        source = _SOURCE
        caption = str(event.get("text", "")).strip() or None
        infos = [f for f in files if isinstance(f, dict)]
        if len(infos) > 1 and all(_is_image_file(f) for f in infos):
            self._ingest_image_batch(infos, client, source, responder, caption)
            return
        for file_info in infos:
            self._ingest_one_file(file_info, client, source, responder, caption)

    def _ingest_image_batch(
        self,
        infos: list[dict[str, Any]],
        client: SlackClientLike | None,
        source: str,
        responder: Responder,
        caption: str | None = None,
    ) -> None:
        """Captures a multi-image Slack message as one capture and page (issue #84).

        Every image is downloaded server-side, fail-loud per file so one bad download
        does not sink the batch. The first image becomes the primary path and the rest
        ride on ``extra_paths`` in upload order.

        The batch is curated once, giving one summary, one tag set and every image
        embedded in the one page. A batch left with a single downloadable file falls
        back to the normal single-file ingest.
        """
        downloaded: list[tuple[Path, str | None]] = []
        for file_info in infos:
            staged = _download_to_tmp(file_info, client, responder)
            if staged is not None:
                downloaded.append(staged)
        if not downloaded:
            return
        primary_path, primary_name = downloaded[0]
        capture = Capture(
            path=primary_path,
            source=source,
            filename=primary_name,
            extra_paths=tuple(path for path, _ in downloaded[1:]),
            text=caption,
        )
        self._do_ingest(capture, responder)

    def _ingest_one_file(
        self,
        file_info: dict[str, Any],
        client: SlackClientLike | None,
        source: str,
        responder: Responder,
        caption: str | None = None,
    ) -> None:
        """Downloads one Slack file object to a temp path and ingests it, fail-loud."""
        staged = _download_to_tmp(file_info, client, responder)
        if staged is None:
            return
        tmp_path, filename = staged
        capture = Capture(path=tmp_path, source=source, filename=filename, text=caption)
        self._do_ingest(capture, responder)

    # ---- internals ---------------------------------------------------------------

    def _route_free_text(self, text: str, source: str, responder: Responder) -> None:
        """Routes bare free text through the intent gate (issue #5).

        Only reached when a message hit none of the deterministic short-circuits: a
        capture prefix, a bare URL, or a shared file. The injected classifier, when
        wired, chooses the engine:

        * ``capture`` files the text as a note, appending :data:`_GATE_CAPTURE_HINT` so
          a misfile is recoverable in one reply;
        * ``query``, the safe fallback, runs the vault-only answer.

        With no classifier wired the route is always ``query``. The gate's keywords
        (issue #102) ride along on the decision and seed the read path's lexical grep.
        Capture ignores them, because it has its own classify and curate enrichment, and
        the no-classifier fallback has none, so the read path greps the raw text.
        """
        decision = self._free_text_route(text)
        route = decision.route
        keywords = list(decision.keywords)
        # Operator-readable line (issue #52) naming the engine bare free text was
        # routed to, so a misroute is visible in the log
        logger.info("slack routed free text to %s", route)
        if route == "capture":
            capture = Capture(text=text, source=source)
            self._do_ingest(capture, responder, hint=_GATE_CAPTURE_HINT)
        else:
            self._do_query(text, responder, search_terms=keywords)

    def _free_text_route(self, text: str) -> IntentDecision:
        """Picks the routing verdict for bare free text (issues #5 and #102).

        With no classifier wired the safe query decision holds, carrying no keywords, so
        the fallback is the vault-only path.

        Otherwise the gate is consulted. Its route already collapses a low-confidence
        verdict to the safe query, and its keywords seed the read path's grep. The
        classifier is total, so a model or parse failure also yields the safe default
        rather than raising.
        """
        if self.intent_classifier is None:
            return _QUERY_FALLBACK_DECISION
        return self.intent_classifier.classify(text)

    def _do_ingest(
        self,
        capture: Capture,
        responder: Responder,
        *,
        hint: str | None = None,
    ) -> None:
        """Runs an ingest and replies, rendering a conflict or error fail-loud.

        An immediate placeholder is posted (issue #34) so a multi-second capture is not
        a dead pause, then edited in place with the final line. A responder with no web
        client degrades to a single reply.

        A vault conflict means content was filed locally but the push was refused, so
        the branch now diverges. Beyond the in-thread warning, an explicit divergence
        alert goes to the errors-to-Slack target (issue #15), the channel the user
        actually watches, carrying the commits-ahead count and oldest-unpushed time from
        git.
        """
        responder.progress(_INGEST_PLACEHOLDER)

        def on_phase(label: str) -> None:
            """Streams a per-phase line into the placeholder, best-effort (#137)."""
            responder.update(f"{_INGEST_PLACEHOLDER} — {label}")

        try:
            report = self.ingestor.ingest(capture, on_phase=on_phase)
        except VaultConflictError as exc:
            logger.warning("capture conflict: %s", exc)
            responder.finish(
                f":warning: *Vault conflict* - {exc}. Resolve in Obsidian, then retry."
            )
            self._alert_divergence(str(exc))
            return
        except IngestError as exc:
            logger.warning("capture failed: %s", exc)
            responder.finish(f":x: Could not file that: {exc}")
            return
        if report.conflict:
            self._alert_divergence(report.message)
        message = render_ingest_report(report)
        if hint:
            message = f"{message}\n{hint}"
        responder.finish(message)

    def _alert_divergence(self, detail: str) -> None:
        """Routes an unpushed-divergence alert to the errors-to-Slack target (#15).

        Best-effort and total. With no alerter wired it no-ops, and the counts come from
        :meth:`~thoth.git_sync.GitSync.divergence`, which swallows git errors itself, so
        this never raises out of a conflict handler.
        """
        if self.alerter is None:
            return
        # Prefer the injected GitSync, else fall back to the one the ingestor holds, as
        # a real Ingestor always has _git. The call is duck-typed so a fake exposing
        # divergence works without subclassing GitSync
        git = self.git if self.git is not None else getattr(self.ingestor, "_git", None)
        ahead, since = -1, None
        if git is not None and hasattr(git, "divergence"):
            try:
                div = git.divergence()
            except (GitSyncError, OSError):  # pragma: no cover - divergence is total
                pass
            else:
                ahead, since = div.commits_ahead, div.since
        self.alerter.alert_unpushed_divergence(
            commits_ahead=ahead, since=since, detail=detail
        )

    def _do_query(
        self,
        text: str,
        responder: Responder,
        *,
        search_terms: list[str] | None = None,
    ) -> None:
        """Runs a vault-only query and replies, rendering an error fail-loud.

        An immediate placeholder is posted (issue #34) then edited in place with the
        rendered answer, degrading to a single reply on a client-less path. The sources
        block lists only the pages the model said it used.
        """
        responder.progress(_ASK_PLACEHOLDER)
        try:
            result = self.query_engine.answer(text, search_terms=search_terms)
        except BudgetExceededError:
            responder.finish(_BUDGET_REACHED_TEXT)
            return
        except QueryError as exc:
            responder.finish(f":x: Could not answer that: {exc}")
            return
        responder.finish(render_query_result(result))


def _build_handlers(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
) -> tuple[Handlers, str]:
    """Constructs the Slack handler graph and returns it with the bot token.

    Factored out of :func:`~thoth.slack_app.build_app` so the startup wiring is
    reachable and unit-testable without importing the optional ``slack_bolt``
    dependency, which is absent in CI. The required-config checks run first, before any
    collaborator is built, so a missing token or capture channel raises at startup
    rather than after a side effect such as opening the state DB.

    Returns:
        The wired handlers and the Slack bot token for the app.
    """
    from thoth.alerts import make_alerter
    from thoth.budget import make_budget_guard
    from thoth.intent import DEFAULT_INTENT_MODEL
    from thoth.llm import LLM

    bot_token, _ = config.require_slack()
    # Fail fast if the capture channel is unset (issue #61). The daemon has no DM
    # fallback, so without it there is nowhere to listen
    capture_channel = config.require_slack_capture_channel()
    # The daily cost guard (issue #16) also caps the intent gate's cheap calls. It
    # shares the state.db counters with the ingest and query graph and alerts once a day
    # through the same errors-to-Slack target
    alerter = make_alerter(config)
    intent_guard = make_budget_guard(config, alerter=alerter)
    handlers = Handlers(
        config=config,
        ingestor=ingestor,
        query_engine=query_engine,
        # Resolve the allow-list from the dotenv-seeded config rather than os.environ.
        # It must work whether systemd started the daemon, exporting the .env, or it was
        # run by hand relying on the dotenv alone. Reading os.environ would give an
        # empty deny-everyone list on the manual path (issue #61)
        allowed_users=parse_allowed_users(config.slack_allowed_users),
        # The one private channel the daemon listens and replies in, everything
        # elsewhere is ignored (issue #61)
        capture_channel=capture_channel,
        # Free-text intent gate (issue #5). One cheap call routes bare prose to capture
        # or vault-query instead of always defaulting to query. It holds its own lazy
        # client on a cheaper model than curate, overridable without a redeploy
        intent_classifier=IntentClassifier(
            LLM(config, guard=intent_guard),
            model=config.intent_model or DEFAULT_INTENT_MODEL,
        ),
        # Durable redelivery dedupe, so a Slack retry across a daemon restart is still
        # dropped. The in-memory cache alone is lost on restart (SPEC section 10)
        dedupe=EventDedupe(store=EventStore(config.state_db_path)),
        # Errors-to-Slack target plus a GitSync for the divergence alert (#15). The
        # ingestor holds one already, so this one is only the divergence probe and need
        # not be the same instance
        alerter=alerter,
        git=GitSync(config),
    )
    return handlers, bot_token
