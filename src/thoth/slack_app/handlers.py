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

# The safe routing verdict used when the gate is bypassed (no classifier wired): route
# to the vault-only query with no keywords, so the read path greps the raw text -- the
# safe fallback (issue #5 / #102).
_QUERY_FALLBACK_DECISION: IntentDecision = IntentDecision(
    intent="query", confidence="high"
)

# The polite refusal sent to a user who is not on the allow-list, if anything at all.
_REFUSAL_TEXT: str = "Sorry, you are not authorised to use this assistant."

# Appended to a confirmation when the intent gate (issue #5) routed *bare* free text to
# capture, so a misfile is recoverable in one reply: the user can just re-send it as a
# question. Explicit-prefix / URL / file captures never carry this -- they are
# unambiguous, deliberate captures.
_GATE_CAPTURE_HINT: str = (
    "_Filed as a note. If you meant to ask, just send it again as a question._"
)

# Rendered on the read paths (query/ask) when the daily LLM budget (issue #16) is spent;
# captures still file durably (curation is deferred), but an answer needs a live model
# call, so the user is told the cap is reached rather than left without a reply.
_BUDGET_REACHED_TEXT: str = (
    ":money_with_wings: Daily LLM budget reached - answering is paused until tomorrow "
    "(Europe/London). Anything you capture is still saved and will be processed later."
)

# The vault ``source`` value for Slack-originated captures.
_SOURCE: str = "slack"


def parse_allowed_users(raw: str | None) -> frozenset[str]:
    """Parse ``SLACK_ALLOWED_USERS`` into a set of bare Slack user ids.

    Accepts a comma- and/or whitespace-separated list. Each token is trimmed of the
    ``@`` and ``<@U...>`` mention wrappers Slack sometimes adds, so ``"<@U1>, @U2  U3"``
    yields ``{"U1", "U2", "U3"}``. ``None`` or a blank string yields an empty set
    (which, combined with :meth:`Handlers.is_allowed`, denies everyone -- fail-closed).

    Args:
        raw: The raw environment value, or ``None`` if the variable is unset.

    Returns:
        A frozenset of normalised user ids.
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
    """The slice of :class:`thoth.alerts.Alerter` the daemon + handlers use.

    Keeps :mod:`thoth.slack_app` decoupled from :mod:`thoth.alerts` (no hard import for
    the type) so a test can inject a tiny fake recording the alerts that were posted.
    """

    def alert_exception(self, where: str, exc: BaseException) -> bool:
        """Format and post an unhandled-exception alert from context ``where``."""
        ...

    def alert_unpushed_divergence(
        self, *, commits_ahead: int, since: datetime | None, detail: str = ...
    ) -> bool:
        """Post the "N commits unpushed -- vault conflict" divergence alert."""
        ...


@dataclass
class Handlers:
    """Pure Slack handler logic with all collaborators injected.

    Holds the constructed :class:`~thoth.ingest.Ingestor` and
    :class:`~thoth.query.QueryEngine`, the parsed allow-list, and the transient
    :class:`EventDedupe`. Every method is unit-testable with fakes -- no live socket
    and no ``slack_bolt`` import is required to exercise the routing/gating/rendering.
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
        """Return ``True`` iff ``user_id`` is on the allow-list (fail-closed)."""
        return bool(user_id) and user_id in self.allowed_users

    def handle_message(
        self,
        event: dict[str, Any],
        say: Callable[..., None],
        client: SlackClientLike | None = None,
    ) -> None:
        """Gate, route, and reply to a channel ``message`` event (issue #61).

        Ignores any message outside the dedicated capture channel
        (:attr:`capture_channel`) so the bot never reacts in other conversations it has
        been invited to; an empty :attr:`capture_channel` disables the gate (the daemon
        enforces the configuration at startup, so this only relaxes the test/library
        path). Ignores bot/own messages and edit/join subtypes so the daemon does not
        loop on its own replies. Each reply is posted **in the message's thread**
        (``thread_ts or ts``) and per-conversation state is keyed by that same thread
        (issue #61). Enforces the allow-list (replying with a polite refusal to a known
        but not-allowed sender) and the redelivery dedupe. Routing (SPEC section 6):

        * a **file upload** (a ``message`` with subtype ``file_share``) downloads and
          ingests every attached file via :meth:`_ingest_uploaded_files` -- this event
          carries the full file objects (download URL + name) and a usable ``channel``,
          unlike the bare ``file_shared`` event the appliance ignores;
        * a bare URL -- or text with a ``capture:``/``note:``/``save:`` prefix -- is an
          ingest (:meth:`thoth.ingest.Ingestor.ingest`);
        * any other **bare free text** is routed by the intent gate
          (:meth:`_route_free_text`, issue #5): an injected
          :class:`~thoth.intent.IntentClassifier` chooses capture / vault-query. With no
          classifier wired the safe fallback holds -- the vault-only
          :meth:`thoth.query.QueryEngine.answer`.

        A surfaced :class:`~thoth.git_sync.VaultConflictError` is rendered fail-loud
        rather than swallowed.

        Args:
            event: The Slack event payload.
            say: A callable that posts a reply string back to the channel; it accepts an
                optional ``thread_ts`` keyword so the reply can be threaded.
            client: The Slack web client (needed to download an uploaded file's bytes);
                ``None`` for the text-only paths, which never touch it.
        """
        if not _should_handle(event):
            return
        channel = _channel(event)
        if self.capture_channel and channel != self.capture_channel:
            # Not our dedicated capture channel: silently ignore (no refusal, no work).
            return
        thread = _conversation_key(event)
        responder = Responder(say, client=client, channel=channel, thread_ts=thread)
        user = str(event.get("user", ""))
        if not self.is_allowed(user):
            # Operator-readable refusal line (issue #52): names the rejected user id and
            # the allow-list size. A size of 0 means the allow-list is empty -- the
            # fail-closed deny-everyone case (SLACK_ALLOWED_USERS unset / not reaching
            # this process), the usual cause of an unexpected "not authorised".
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
        """Download and ingest the files on a ``file_share`` message (SPEC section 6).

        The ``message``/``file_share`` event carries the full ``files`` objects -- each
        with a private download URL and ``name`` -- and a usable ``channel`` to reply
        in. Each file is downloaded server-side to a temporary path (never base64) and
        the ingestor moves binaries into the vault via ``save_asset``. A missing URL or
        a download failure is surfaced fail-loud **per file** so the rest still ingest.

        A single Slack message that attaches **several images at once** is the natural
        unit of intent (issue #84): the user meant them as one thing, so an all-image
        batch is captured as ONE :class:`~thoth.ingest.Capture` -- the first image is
        the primary ``path``, the rest ride on ``extra_paths`` and are saved as extra
        assets under the same slug and embedded in the same curated page, giving the
        batch one shared summary + one tag set. A single-file message is unchanged, and
        a heterogeneous batch (mixed images with PDFs/text) is still ingested per file
        -- the per-page-type classification of mixed kinds is deferred (issue #84 open
        questions).

        A text caption typed alongside the upload (issue #130) is threaded onto the
        :class:`~thoth.ingest.Capture` as its ``text`` so it reaches the model
        *alongside* the file's own OCR/analysis -- the caption augments, it does not
        replace, the image content. A batch shares the one caption (one unit of
        intent, per #84). A capture-prefix (``note:``/``save:``) in the caption does
        not change routing for an upload: a file_share is always a capture, so the
        prefix is left verbatim in the caption text.
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
        """Capture a multi-image Slack message as ONE capture/page (issue #84).

        Downloads every image server-side (fail-loud per file, so one bad download does
        not sink the batch), then hands the ingestor a single
        :class:`~thoth.ingest.Capture` whose primary ``path`` is the first image and
        whose ``extra_paths`` carry the rest in upload order. The batch is curated once
        -- one summary, one tag set, every image embedded in the one page. A batch that
        survives with only a single downloadable file falls back to the normal
        single-file ingest.
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
        """Download one Slack file object to a temp path and ingest it (fail-loud)."""
        staged = _download_to_tmp(file_info, client, responder)
        if staged is None:
            return
        tmp_path, filename = staged
        capture = Capture(path=tmp_path, source=source, filename=filename, text=caption)
        self._do_ingest(capture, responder)

    # ---- internals ---------------------------------------------------------------

    def _route_free_text(self, text: str, source: str, responder: Responder) -> None:
        """Route bare free text through the intent gate (issue #5).

        Only reached for a message that hit none of the deterministic short-circuits
        (``capture:``/``note:``/``save:`` prefix, bare URL, shared file). The injected
        :class:`~thoth.intent.IntentClassifier` -- when wired -- chooses the engine:

        * ``capture`` files the text as a note, appending :data:`_GATE_CAPTURE_HINT`
          so a misfile is recoverable in one reply;
        * ``query`` (the safe fallback) runs the vault-only
          :meth:`thoth.query.QueryEngine.answer`.

        With no classifier wired the route is always ``query`` (the safe vault path).

        The gate's keywords (issue #102) ride along on the :class:`~thoth.intent.
        IntentDecision` and are passed to :meth:`_do_query` as ``search_terms`` to seed
        the lexical grep; capture ignores them (it has its own classify/curate
        enrichment). On the no-classifier ``query`` fallback there are no keywords, so
        the read path greps the raw text.
        """
        decision = self._free_text_route(text)
        route = decision.route
        keywords = list(decision.keywords)
        # Concise operator-readable line (issue #52): the engine bare free text was
        # routed to (capture / query), so a misroute is visible in the log.
        logger.info("slack routed free text to %s", route)
        if route == "capture":
            capture = Capture(text=text, source=source)
            self._do_ingest(capture, responder, hint=_GATE_CAPTURE_HINT)
        else:
            self._do_query(text, responder, search_terms=keywords)

    def _free_text_route(self, text: str) -> IntentDecision:
        """Pick the routing verdict for bare free text (issue #5 + #102 keywords).

        Returns the safe ``query`` decision (no keywords) when no classifier is wired,
        so the fallback in :meth:`_route_free_text` is the vault-only path. Otherwise it
        consults the gate and returns the full :class:`~thoth.intent.IntentDecision`,
        whose :attr:`~thoth.intent.IntentDecision.route` already collapses a low-
        confidence verdict to the safe ``query`` and whose ``keywords`` seed the read
        path's grep (issue #102). The classifier is itself total, so a model/parse
        failure also yields the safe default rather than raising.
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
        """Run an ingest and reply; render a conflict/error fail-loud, never crash.

        Posts an immediate ":hourglass_flowing_sand: Filing…" placeholder (issue #34,
        Slice B) so a multi-second capture is not a dead pause, then edits it in place
        with the final confirmation (or the conflict/error line). When the responder has
        no web client (the text-only/test paths) this degrades to a single reply.

        A vault conflict (the ingestor's :attr:`~thoth.ingest.IngestReport.conflict`, or
        a raised :class:`~thoth.git_sync.VaultConflictError`) means content was filed
        locally but the push was refused, so the local branch now diverges from the
        remote. Beyond the in-thread ``:warning:`` reply, an explicit
        unpushed-divergence alert is routed to the errors-to-Slack target (issue #15) --
        the daily channel the user actually watches -- with the commits-ahead count +
        oldest-unpushed time computed from git.

        ``hint`` is an optional extra line appended to the confirmation; the intent gate
        passes :data:`_GATE_CAPTURE_HINT` so a gate-routed capture is recoverable (issue
        #5). It is not appended to the early conflict/error replies above.
        """
        responder.progress(_INGEST_PLACEHOLDER)

        def on_phase(label: str) -> None:
            """Stream a per-phase line into the placeholder (#137, best-effort)."""
            responder.update(f"{_INGEST_PLACEHOLDER} — {label}")

        try:
            report = self.ingestor.ingest(capture, on_phase=on_phase)
        except VaultConflictError as exc:
            responder.finish(
                f":warning: *Vault conflict* - {exc}. Resolve in Obsidian, then retry."
            )
            self._alert_divergence(str(exc))
            return
        except IngestError as exc:
            responder.finish(f":x: Could not file that: {exc}")
            return
        if report.conflict:
            self._alert_divergence(report.message)
        message = render_ingest_report(report)
        if hint:
            message = f"{message}\n{hint}"
        responder.finish(message)

    def _alert_divergence(self, detail: str) -> None:
        """Route an unpushed-divergence alert to the errors-to-Slack target (issue #15).

        Best-effort and total: with no alerter wired it no-ops; the commits-ahead count
        and oldest-unpushed time are read from git via :meth:`~thoth.git_sync.GitSync.
        divergence` (which itself swallows git errors), so this never raises out of a
        conflict handler.
        """
        if self.alerter is None:
            return
        # Prefer the explicitly-injected GitSync; otherwise fall back to the one the
        # ingestor already holds (a real production Ingestor always has ``_git``). The
        # call is duck-typed so a test fake exposing ``divergence`` works without being
        # a GitSync subclass.
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
        """Run a vault-only query and reply; render an error fail-loud, never crash.

        Posts an immediate ":mag: Looking…" placeholder (issue #34, Slice B) then edits
        it in place with the rendered answer; degrades to a single reply on a
        client-less path. The ``Sources:`` block lists only the pages the model said it
        used (issue #34's ``USED:`` filter, parsed in :mod:`thoth.query`).
        ``search_terms`` are the intent gate's keywords (issue #102): they seed the grep
        while the prose is composed from ``text``; empty/``None`` greps ``text`` itself.
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
    """Construct the Slack :class:`Handlers` graph; return it with the bot token.

    Factored out of :func:`~thoth.slack_app.build_app` so the startup wiring -- the
    fail-fast required-config checks (both Slack tokens and, issue #61,
    ``SLACK_CAPTURE_CHANNEL``) and the collaborator construction -- is reachable and
    unit-testable **without** importing the optional ``slack_bolt`` dependency that
    :func:`~thoth.slack_app.build_app` needs for the ``App`` itself (absent in CI). The
    required-config checks run **first**, before any collaborator is built, so a missing
    token / capture channel raises :class:`~thoth.config.ConfigError` at startup rather
    than after side effects (e.g. opening the state DB).

    Returns:
        A ``(handlers, bot_token)`` pair: the wired :class:`Handlers` and the Slack bot
        token :func:`~thoth.slack_app.build_app` passes to the ``App``.
    """
    from thoth.alerts import make_alerter
    from thoth.budget import make_budget_guard
    from thoth.intent import DEFAULT_INTENT_MODEL
    from thoth.llm import LLM

    bot_token, _ = config.require_slack()
    # Fail fast at startup if the dedicated capture channel is unset (issue #61): the
    # daemon has no DM fallback, so without it there is nowhere to listen.
    capture_channel = config.require_slack_capture_channel()
    # The daily cost guard (issue #16) also caps the intent gate's cheap Haiku calls; it
    # shares the same state.db counters as the ingest/query graph and alerts once per
    # day via the same errors-to-Slack target the Handlers use.
    alerter = make_alerter(config)
    intent_guard = make_budget_guard(config, alerter=alerter)
    handlers = Handlers(
        config=config,
        ingestor=ingestor,
        query_engine=query_engine,
        # Resolve the allow-list from the dotenv-seeded config, NOT os.environ directly:
        # the value must work whether the daemon was started by systemd (which exports
        # the .env) or run by hand relying on ~/.thoth/.env (dotenv-only). Reading
        # os.environ alone would yield an empty -- fail-closed, deny-everyone -- list on
        # the manual path, unlike the rest of the Slack config (issue #61).
        allowed_users=parse_allowed_users(config.slack_allowed_users),
        # The one private channel the daemon listens/replies in; messages elsewhere are
        # ignored (issue #61).
        capture_channel=capture_channel,
        # Free-text intent gate (issue #5): one cheap Haiku call routes bare prose to
        # capture / vault-query instead of always defaulting to query. Its own lazy LLM
        # client (a different, cheaper model than the curate Sonnet); the model is
        # overridable without a redeploy via THOTH_INTENT_MODEL.
        intent_classifier=IntentClassifier(
            LLM(config, guard=intent_guard),
            model=config.intent_model or DEFAULT_INTENT_MODEL,
        ),
        # Durable redelivery dedupe so a Slack retry across a daemon restart is still
        # dropped (the in-memory cache alone is lost on restart, SPEC section 10).
        dedupe=EventDedupe(store=EventStore(config.state_db_path)),
        # Errors-to-Slack target + a GitSync for the unpushed-divergence alert (#15);
        # the ingestor already holds a GitSync, so a separate one here is only the
        # divergence probe and need not be the same instance.
        alerter=alerter,
        git=GitSync(config),
    )
    return handlers, bot_token
