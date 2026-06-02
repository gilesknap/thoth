"""The Slack Socket-Mode daemon and its pure, unit-testable handler logic.

This module is the appliance's primary capture/retrieve surface (SPEC sections 6, 7
and 10). It wires a Slack `Bolt <https://slack.dev/bolt-python>`_ Socket-Mode app to
collaborators that are constructed elsewhere and injected here: an
:class:`~thoth.ingest.Ingestor` (capture), a :class:`~thoth.query.QueryEngine` (fast
vault-only retrieve), and -- when wired -- a :class:`~thoth.research.ResearchEngine`
(the blended web+vault Q&A that backs the default free-text path, SPEC section 7.1). The
daemon listens in **one dedicated private channel** (``SLACK_CAPTURE_CHANNEL``, you plus
the bot) and ignores every other conversation (issue #61): each top-level message starts
a new capture/ask handled **in its own thread** (the bot replies under the originating
message's ``ts``), and a reply *inside* that thread continues it -- so per-conversation
state is keyed by **thread**, not channel, and two interleaved topics never clobber each
other's pending save. A file upload arrives as a ``message`` with subtype ``file_share``
carrying the full file objects. The daemon gates each message through an allow-list and
a transient redelivery dedupe, routes a bare URL / uploaded file to an ingest and sends
**bare free text** through a lightweight intent gate
(:class:`~thoth.intent.IntentClassifier`, issue #5) that picks capture / vault-query /
blended ask -- falling back to the pre-gate default (blended ask, or vault-only query
when no research engine is injected) when no classifier is wired. It offers to save a
blended answer as a ``notes/`` page on a follow-up "y" *in the thread*, and replies in
Slack ``mrkdwn``. A slow request shows an immediate placeholder
(":hourglass_flowing_sand: Filing…" / ":mag: Looking…") that is edited in place with the
final render via ``chat.update`` (issue #34, Slice B) so a multi-second capture is not a
dead pause; this degrades to a single ``say`` on a client-less path.

This is a pure cutover from the old DM (``message.im``) surface and supersedes the Slack
Assistant pane (issue #34, Slice C): the manifest subscribes ``message.groups`` with the
``groups:history`` / ``groups:read`` scopes, and the ``assistant_*`` events are gone.

Design constraints enforced here:

* ``slack_bolt`` is **never** imported at module top level (it is absent in CI). It is
  imported lazily, only inside :func:`build_app` and :func:`run`. Everything else --
  the allow-list parser, the ``mrkdwn`` renderers, the :class:`EventDedupe`, and the
  :class:`Handlers` logic -- is pure and unit-tested with fakes, so importing this
  module performs no heavy import and spins up no socket.
* This module **never builds an** ``obsidian://`` **link itself**. Links are built by
  the harness (``Vault.obsidian_uri`` via the query/ingest layers) and arrive already
  formed on :class:`~thoth.query.Citation` and :class:`~thoth.ingest.IngestReport`; the
  renderers here only format those unfabricable values. Every Slack reference is
  rendered through the one shared :func:`thoth.render.render_vault_ref` helper as a
  title-only clickable ``<obsidian-uri|title>`` link (issue #63); the trailing path and
  the dead ``[[wikilink]]`` (un-clickable in Slack) are deliberately dropped.
* File uploads are downloaded **server-side** to a temporary file and handed to the
  ingestor as :class:`~thoth.ingest.Capture` with a ``path`` -- never as base64
  (SPEC section 6 capture note). A non-allowed user is rejected before any download.
* :class:`EventDedupe` is the redelivery seam (SPEC section 10): a fast in-memory TTL
  set front-cache backed by a **durable** ``processed_events`` row in
  :class:`thoth.state.EventStore` (``~/.thoth/state.db``) so a Slack redelivery that
  straddles a daemon restart is still recognised as already-processed. The in-memory
  set alone is lost on restart; the table survives it.

Only the standard library plus ``thoth`` modules are imported at module level, so the
module is always import-safe under pytest collection.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from thoth.budget import BudgetExceededError
from thoth.config import Config
from thoth.git_sync import GitSync, GitSyncError, VaultConflictError
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.intent import IntentClassifier, IntentDecision
from thoth.query import Citation, QueryEngine, QueryError, QueryResult
from thoth.render import render_vault_ref
from thoth.research import (
    AskResult,
    ResearchEngine,
    ResearchError,
    force_web_requested,
)
from thoth.state import EventStore

logger = logging.getLogger(__name__)

DEDUPE_TTL_SECONDS: float = 3600.0
"""Prune processed-event ids older than one hour (SPEC section 10)."""

PENDING_SAVE_TTL_SECONDS: float = 1800.0
"""How long an unanswered save offer stays live (~30 min, SPEC section 10)."""

# A free-text message whose body, once stripped, begins with one of these prefixes is
# routed to ingest-as-text rather than query (an explicit "save this thought" signal).
_CAPTURE_PREFIXES: tuple[str, ...] = ("capture:", "note:", "save:")

# Image file extensions (no dot) that mark a Slack upload as an image, so a multi-file
# message of only images is captured as ONE page (issue #84). Mirrors the ingest
# pipeline's image kinds; a mimetype check is tried first in
# :meth:`Handlers._is_image_file`.
_IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})

# Affirmative replies that confirm a pending "save this answer to the vault?" offer.
_CONFIRM_WORDS: frozenset[str] = frozenset(
    {"y", "yes", "save", "save it", "ok", "okay"}
)

# The safe routing verdict used when the gate is bypassed (no classifier wired, or a
# deterministic ``research:`` escape hatch): route to the blended ask with no keywords,
# so the read path greps the raw text -- the pre-gate behaviour (issue #5 / #102).
_ASK_FALLBACK_DECISION: IntentDecision = IntentDecision(intent="ask", confidence="high")

# The polite refusal sent to a user who is not on the allow-list, if anything at all.
_REFUSAL_TEXT: str = "Sorry, you are not authorised to use this assistant."

# The offer-to-save line appended to a blended answer (SPEC section 7.1 step 4). The
# confirmation must land *in the thread* the answer was posted to (issue #61): a
# channel-level "y" is a fresh top-level message keyed to its own thread, so it will not
# confirm (recoverable -- the user just re-sends "y" as a reply -- never data-loss).
_SAVE_OFFER_TEXT: str = (
    "_Save this answer to the vault? Reply *y* in this thread to file it._"
)

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


class SlackError(Exception):
    """Base error for the Slack surface (raised by the daemon factory wiring)."""


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
        token = _strip_user_wrapper(piece)
        if token:
            tokens.append(token)
    return frozenset(tokens)


def _strip_user_wrapper(token: str) -> str:
    """Strip ``<@...>`` and a leading ``@`` from one allow-list / mention token."""
    token = token.strip()
    if token.startswith("<@") and token.endswith(">"):
        token = token[2:-1]
    if token.startswith("@"):
        token = token[1:]
    # Slack mention markup may carry a display label after a pipe: <@U1|name>.
    token = token.split("|", 1)[0]
    return token.strip()


def render_citation(citation: Citation) -> str:
    """Render one citation as the concise shared Slack reference (issue #53).

    Delegates to :func:`thoth.render.render_vault_ref`, emitting a title-only clickable
    ``<obsidian-uri|title>`` link over the harness-built ``obsidian://`` link, with no
    trailing path (issue #63). The link target is taken verbatim from the
    :class:`~thoth.query.Citation`; this function never constructs an ``obsidian://``
    URI itself, and the dead ``[[wikilink]]`` is no longer shown (it is un-clickable in
    Slack).

    Args:
        citation: A harness-built citation handle.

    Returns:
        A single ``mrkdwn`` line for the citation.
    """
    return render_vault_ref(
        obsidian_uri=citation.obsidian_uri,
        title=citation.title or citation.path,
        path=citation.path,
    )


def render_query_result(result: QueryResult) -> str:
    """Render a composed answer plus its citation list as a ``mrkdwn`` block.

    The answer prose comes first, followed by a ``Sources:`` list with one
    :func:`render_citation` line per cited page (SPEC Appendix worked example). The
    cited set is the pages the model said it actually used (issue #34's ``USED:`` line,
    parsed in :mod:`thoth.query`), so the list reflects what the answer drew on rather
    than the whole retrieval candidate set. When the answer has no citations the prose
    stands alone -- no trailing note is added (issue #53).

    Args:
        result: The query result to render.

    Returns:
        A ``mrkdwn`` string ready for ``chat.postMessage``.
    """
    lines = [result.answer.strip()]
    if result.citations:
        lines.append("")
        lines.append("*Sources:*")
        lines.extend(f"- {render_citation(c)}" for c in result.citations)
    return "\n".join(lines)


def render_ask_result(result: AskResult, *, offer_save: bool = True) -> str:
    """Render a blended web+vault Q&A answer as a ``mrkdwn`` block (SPEC section 7.1).

    The prose answer comes first, then a ``Sources:`` list combining the harness-built
    vault citations (:func:`render_citation`) and the web URLs the model actually read.
    When ``offer_save`` is set (and the answer is non-empty), the offer-to-save line is
    appended so the user can file the answer as a ``notes/`` page with a one-word reply.
    Both vault and web citations use the one concise shared shape (issue #63): a
    title-only clickable ``<obsidian-uri|title>`` link (web citations use the URL as the
    link target).

    Args:
        result: The blended answer to render.
        offer_save: Whether to append the "save this answer?" offer line.

    Returns:
        A ``mrkdwn`` string ready for ``chat.postMessage``.
    """
    lines = [result.answer.strip()]
    if result.vault_citations or result.web_citations:
        lines.append("")
        lines.append("*Sources:*")
        lines.extend(f"- {render_citation(c)}" for c in result.vault_citations)
        for web in result.web_citations:
            ref = render_vault_ref(
                obsidian_uri=web.url, title=web.title or web.url, path=web.url
            )
            lines.append(f"- {ref}")
    if offer_save:
        lines.append("")
        lines.append(_SAVE_OFFER_TEXT)
    return "\n".join(lines)


def render_ingest_report(report: IngestReport) -> str:
    """Render a one-to-two-line capture confirmation in ``mrkdwn``.

    Names what was filed and renders one concise shared reference per curated page
    (issue #63): a ``Filed N page(s):`` header followed by a title-only clickable
    ``<obsidian-uri|title>`` line per page (no trailing path). When no curated page was
    written the header names the raw/asset paths directly. A
    :attr:`~thoth.ingest.IngestReport.conflict` is surfaced fail-loud (SPEC section 10)
    with the conflicting path, never swallowed. A
    :attr:`~thoth.ingest.IngestReport.deferred` capture (raw persisted but the LLM was
    unavailable for curation) is surfaced as a partial-success note naming the held raw
    page, so the user knows the item is safe and will be re-curated (SPEC section 6).

    Args:
        report: The structured ingest outcome.

    Returns:
        A concise ``mrkdwn`` confirmation (or conflict / deferred) string.
    """
    if report.conflict:
        detail = report.message or "a vault conflict blocked the sync"
        return f":warning: *Vault conflict* - {detail}. Content was filed locally."

    if report.deferred:
        held = report.raw_paths or report.asset_paths
        where = ", ".join(f"`{path}`" for path in held) or "the inbox"
        note = report.message or "curation deferred -- LLM unavailable"
        return f":hourglass_flowing_sand: Saved raw to {where}. {note}"

    parts: list[str] = []
    if report.page_paths:
        count = len(report.page_paths)
        head = f"Filed {count} page(s):"
        if not report.committed:
            head += " (not yet committed)"
        parts.append(head)
        # One title-only <uri|title> ref per curated page (issue #63: no trailing path).
        # ``titles`` runs parallel to page_paths / obsidian_links (the slug-derived
        # title is filled in upstream when missing).
        for path, uri, title in zip(
            report.page_paths,
            report.obsidian_links,
            report.titles,
            strict=False,
        ):
            parts.append(render_vault_ref(obsidian_uri=uri, title=title, path=path))
    else:
        filed = report.raw_paths or report.asset_paths
        if filed:
            head = "Filed " + ", ".join(f"`{path}`" for path in filed)
        else:
            head = "Nothing new to file"
        if not report.committed:
            head += " (not yet committed)"
        parts.append(head)

    if report.message and not report.conflict:
        parts.append(report.message)
    return "\n".join(parts)


class EventDedupe:
    """TTL dedupe of processed Slack event ids: in-memory cache over a durable store.

    Slack redelivers events on a missed ack, so each handler drops a redelivery by
    asking :meth:`seen` once per event. Entries older than ``ttl_seconds`` are pruned
    (SPEC section 10). The in-memory dict is a **fast front cache**; when a
    :class:`thoth.state.EventStore` is injected it is the **durable** backing
    (``processed_events`` in ``~/.thoth/state.db``), so a redelivery that straddles a
    daemon restart -- where the in-memory cache is gone -- is still recognised as
    already-processed by a *fresh* ``EventDedupe`` built over the same state DB. With no
    store injected the behaviour is the legacy transient-only set (used where no daemon
    persistence is wanted). The clock is injectable for deterministic tests.

    Both layers must use the **same clock** for the TTL to agree; the store defaults to
    wall-clock :func:`time.time` (a recorded timestamp must survive a restart, which a
    monotonic clock would reset), so this class also defaults to :func:`time.time` (not
    :func:`time.monotonic`).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEDUPE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
        store: EventStore | None = None,
    ) -> None:
        """Build a dedupe over an optional durable store.

        Args:
            ttl_seconds: How long a recorded event id is remembered before pruning.
            clock: A wall-clock time source returning seconds; defaults to
                :func:`time.time` so recorded timestamps survive a process restart and
                agree with the store's own clock.
            store: The durable :class:`thoth.state.EventStore` backing
                ``processed_events``; when ``None`` the dedupe is in-memory only (the
                legacy transient behaviour). Pass the same clock to both for the TTL to
                agree across the cache and the store.
        """
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.time
        self._store = store
        self._seen: dict[str, float] = {}

    def seen(self, event_id: str) -> bool:
        """Report whether ``event_id`` was already processed, recording it if new.

        Prunes expired cache entries first, then checks the **fast front cache**: a hit
        there is an immediate ``True`` (drop the redelivery). On a cache miss the
        durable :class:`~thoth.state.EventStore` is consulted (its own atomic
        insert-or-ignore is the source of truth across restarts); whatever it reports is
        cached and returned. With no store, a cache miss records the id in the cache and
        returns ``False``. An empty ``event_id`` is always unseen and never recorded (a
        missing id cannot be deduped).

        Args:
            event_id: The Slack event id (or client message id).

        Returns:
            ``True`` if this id was seen before, else ``False``.
        """
        self.prune()
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        if self._store is not None:
            already = self._store.seen(event_id, ttl_seconds=self._ttl)
            self._seen[event_id] = self._clock()
            return already
        self._seen[event_id] = self._clock()
        return False

    def mark(self, event_id: str) -> None:
        """Record ``event_id`` as processed now in the cache and the durable store."""
        if not event_id:
            return
        self._seen[event_id] = self._clock()
        if self._store is not None:
            self._store.mark(event_id, ttl_seconds=self._ttl)

    def prune(self) -> None:
        """Drop every cache entry older than ``ttl_seconds`` (the store self-prunes)."""
        cutoff = self._clock() - self._ttl
        self._seen = {
            event_id: ts for event_id, ts in self._seen.items() if ts >= cutoff
        }


class PendingSaves:
    """Transient per-thread buffer of the last blended answer awaiting a save reply.

    The blended-ask path (SPEC section 7.1) ends by offering to save the answer; a
    follow-up "y" *in the same thread* confirms. It holds the ``(question, AskResult)``
    for the most recent answer **per conversation thread** (keyed by ``thread_ts or
    ts``, issue #61) until it is confirmed, superseded, or expires (the ``ttl_seconds``
    window, ~30 min, SPEC section 10). Keying by thread -- not channel -- is the
    load-bearing change of issue #61: in a single shared channel a channel key would be
    effectively global, so two interleaved topics clobber each other's pending save and
    a "y" would apply to whatever the most recent answer in the channel was. It is
    **transient working memory only**, never a store -- the in-memory seam the Phase-3
    SQLite ``conversations`` table would later sit behind. The clock is injectable for
    deterministic tests.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PENDING_SAVE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Build an empty pending-save buffer.

        Args:
            ttl_seconds: How long an unanswered save offer is remembered.
            clock: A monotonic-ish time source returning seconds; defaults to
                :func:`time.monotonic`.
        """
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._pending: dict[str, tuple[float, str, AskResult]] = {}

    def remember(self, key: str, question: str, result: AskResult) -> None:
        """Record the latest savable answer for thread ``key`` (no-op for empty id)."""
        if key:
            self._pending[key] = (self._clock(), question, result)

    def take(self, key: str) -> tuple[str, AskResult] | None:
        """Pop and return the live ``(question, result)`` for thread ``key``, if any.

        Prunes expired entries first, then removes and returns the pending answer for
        the conversation thread ``key`` (so a single "y" saves it exactly once); returns
        ``None`` when there is no live offer.
        """
        self.prune()
        entry = self._pending.pop(key, None)
        if entry is None:
            return None
        _, question, result = entry
        return question, result

    def prune(self) -> None:
        """Drop every pending offer older than ``ttl_seconds`` from now."""
        cutoff = self._clock() - self._ttl
        self._pending = {
            key: entry for key, entry in self._pending.items() if entry[0] >= cutoff
        }


class SlackClientLike(Protocol):
    """The slice of the Bolt web client used by the handlers."""

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post a message to a channel (Slack ``chat.postMessage``)."""
        ...

    def chat_update(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, ts: str, text: str, **kwargs: Any
    ) -> Any:
        """Edit a previously-posted message in place (Slack ``chat.update``)."""
        ...


# Placeholder lines shown the instant a slow request is received (issue #34, Slice B),
# so a multi-second capture/answer is not a dead pause. They are edited in place (via
# chat.update) with the final render once the work completes.
_INGEST_PLACEHOLDER: str = ":hourglass_flowing_sand: Filing…"
_ASK_PLACEHOLDER: str = ":mag: Looking…"


class Responder:
    """The reply seam for one message: an immediate placeholder, then a final edit.

    A multi-second capture/answer (a ``git pull`` -> classify -> extract -> curate ->
    Hindsight retain+probe -> commit+push chain, easily 5-15s) shows nothing until done
    if the handler only ``say()``s once at the end. This object (issue #34, Slice B)
    posts an immediate placeholder via the Slack web client, remembers its message
    ``ts``, and edits that same message in place with the final render (``chat.update``)
    -- so the user sees "Filing…" within ~1s and it resolves to the report, with no
    second message.

    Every reply is posted **in the conversation thread** (issue #61): the placeholder
    carries ``thread_ts`` and the bare-``say`` fallback threads its reply too, so a
    reply lands under the originating top-level message, not at channel top level. The
    in-place edit (``chat.update``) targets the placeholder's own ``ts`` and so stays in
    the thread automatically.

    It degrades cleanly: when no web ``client`` or ``channel`` is available (the
    text-only/test paths that pass only a bare ``say``), :meth:`progress` posts nothing
    and :meth:`finish` falls back to a single ``say(text)`` (still threaded) -- the
    exact pre-#34 behaviour. So the placeholder+update is best-effort UX over the
    existing single-``say`` contract, never a hard dependency on the client.
    """

    def __init__(
        self,
        say: Callable[..., None],
        *,
        client: SlackClientLike | None = None,
        channel: str = "",
        thread_ts: str = "",
    ) -> None:
        """Build a responder over a ``say`` callable and an optional web client+channel.

        Args:
            say: The Bolt ``say`` callable that posts a reply to the conversation; it
                accepts an optional ``thread_ts`` keyword so a reply can be threaded.
            client: The Slack web client used to post + edit the placeholder; ``None``
                disables the placeholder (the single-``say`` fallback).
            channel: The conversation id the placeholder is posted to / edited in; an
                empty id also disables the placeholder.
            thread_ts: The thread root to post replies under (``thread_ts or ts`` of the
                originating message, issue #61). Empty means post at channel top level
                (the test/edge paths); production always supplies it.
        """
        self._say = say
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._ts: str | None = None

    def _emit(self, text: str) -> None:
        """Post a fresh reply via the bare ``say``, threading it when set."""
        if self._thread_ts:
            self._say(text, thread_ts=self._thread_ts)
        else:
            self._say(text)

    def _thread_kwargs(self) -> dict[str, str]:
        """The ``thread_ts`` kwargs for a client post, or ``{}`` at top level."""
        return {"thread_ts": self._thread_ts} if self._thread_ts else {}

    def say(self, text: str) -> None:
        """Post ``text`` as a plain threaded reply (early conflict/error/refusal)."""
        self._emit(text)

    def progress(self, placeholder: str) -> None:
        """Post an immediate placeholder (best-effort); remember its ts for the edit.

        Posts into the conversation thread (``thread_ts``) so the working signal appears
        under the originating message. With no client/channel, or if the post fails for
        any reason, this no-ops and a later :meth:`finish` falls back to a single
        ``say`` -- the placeholder must never be able to swallow the real reply.
        """
        if self._client is None or not self._channel:
            return
        try:
            response = self._client.chat_postMessage(
                channel=self._channel, text=placeholder, **self._thread_kwargs()
            )
        except Exception:  # noqa: BLE001 - placeholder is best-effort UX, never fatal
            return
        ts = response.get("ts") if isinstance(response, dict) else None
        if isinstance(ts, str) and ts:
            self._ts = ts

    def finish(self, text: str) -> None:
        """Deliver the final reply: edit the placeholder in place, else a fresh ``say``.

        When a placeholder ts was captured the message is edited via ``chat.update`` (so
        the "Filing…" line becomes the report; the edit stays in-thread by targeting
        that ts). When there is no placeholder -- no client, the post failed, or a
        client-less path -- it falls back to a threaded ``say(text)``, the single-reply
        pre-#34 behaviour. A failed edit also falls back to ``say`` so the user always
        gets the reply.
        """
        if self._client is not None and self._channel and self._ts is not None:
            try:
                self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
            except Exception:  # noqa: BLE001 - fall back to a fresh post on any edit error
                self._emit(text)
            return
        self._emit(text)


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
    research: ResearchEngine | None = None
    intent_classifier: IntentClassifier | None = None
    dedupe: EventDedupe = field(default_factory=EventDedupe)
    pending_saves: PendingSaves = field(default_factory=PendingSaves)
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
        but not-allowed sender) and the redelivery dedupe. Routing (SPEC sections
        6, 7.1):

        * a **file upload** (a ``message`` with subtype ``file_share``) downloads and
          ingests every attached file via :meth:`_ingest_uploaded_files` -- this event
          carries the full file objects (download URL + name) and a usable ``channel``,
          unlike the bare ``file_shared`` event the appliance ignores;
        * a bare ``y``/``yes``/``save`` reply **in a thread** confirms that thread's
          pending "save this answer?" offer and files the last blended answer as a
          ``notes/`` page;
        * a bare URL -- or text with a ``capture:``/``note:``/``save:`` prefix -- is an
          ingest (:meth:`thoth.ingest.Ingestor.ingest`);
        * any other **bare free text** is routed by the intent gate
          (:meth:`_route_free_text`, issue #5): an injected
          :class:`~thoth.intent.IntentClassifier` chooses capture / vault-query /
          blended ask. With no classifier wired the pre-gate default holds -- the
          **blended** Q&A path (:meth:`thoth.research.ResearchEngine.ask`) when a
          research engine is wired, else the vault-only
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
        if not self._should_handle(event):
            return
        channel = self._channel(event)
        if self.capture_channel and channel != self.capture_channel:
            # Not our dedicated capture channel: silently ignore (no refusal, no work).
            return
        thread = self._conversation_key(event)
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
        if self.dedupe.seen(self._event_key(event)):
            return

        if event.get("subtype") == "file_share":
            self._ingest_uploaded_files(event, client, responder)
            return

        text = str(event.get("text", "")).strip()
        if not text:
            return
        source = self._source_label()
        if self._is_confirm_save(text) and self._try_confirm_save(thread, responder):
            return
        if self._is_capture_text(text):
            capture = Capture(text=self._strip_capture_prefix(text), source=source)
            self._do_ingest(capture, responder)
        elif self._looks_like_url(text):
            capture = Capture(url=text, source=source)
            self._do_ingest(capture, responder)
        else:
            self._route_free_text(text, thread, source, responder)

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
        questions). An upload's text caption is intentionally ignored -- the file is the
        capture.
        """
        files = event.get("files")
        if not isinstance(files, list) or not files:
            responder.say(":warning: That upload carried no files I could read.")
            return
        source = self._source_label()
        infos = [f for f in files if isinstance(f, dict)]
        if len(infos) > 1 and all(self._is_image_file(f) for f in infos):
            self._ingest_image_batch(infos, client, source, responder)
            return
        for file_info in infos:
            self._ingest_one_file(file_info, client, source, responder)

    def _ingest_image_batch(
        self,
        infos: list[dict[str, Any]],
        client: SlackClientLike | None,
        source: str,
        responder: Responder,
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
            staged = self._download_to_tmp(file_info, client, responder)
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
        )
        self._do_ingest(capture, responder)

    def _ingest_one_file(
        self,
        file_info: dict[str, Any],
        client: SlackClientLike | None,
        source: str,
        responder: Responder,
    ) -> None:
        """Download one Slack file object to a temp path and ingest it (fail-loud)."""
        staged = self._download_to_tmp(file_info, client, responder)
        if staged is None:
            return
        tmp_path, filename = staged
        capture = Capture(path=tmp_path, source=source, filename=filename)
        self._do_ingest(capture, responder)

    def _download_to_tmp(
        self,
        file_info: dict[str, Any],
        client: SlackClientLike | None,
        responder: Responder,
    ) -> tuple[Path, str | None] | None:
        """Download one Slack file object to a temp path (fail-loud), or ``None``.

        Returns the staged ``(path, filename)`` on success; warns and returns ``None``
        for a missing download URL or a failed download so a batch keeps the rest.
        """
        url = self._download_url(file_info)
        if not url:
            responder.say(":warning: Could not find a downloadable URL for that file.")
            return None
        filename = file_info.get("name")
        suffix = Path(filename).suffix if isinstance(filename, str) and filename else ""
        try:
            data = self._download_bytes(client, url)
        except SlackError as exc:
            responder.say(f":x: Could not download that file: {exc}")
            return None
        with tempfile.NamedTemporaryFile(
            prefix="thoth-upload-", suffix=suffix, delete=False
        ) as handle:
            handle.write(data)
            tmp_path = Path(handle.name)
        return tmp_path, filename if isinstance(filename, str) else None

    # ---- internals ---------------------------------------------------------------

    def _route_free_text(
        self, text: str, thread: str, source: str, responder: Responder
    ) -> None:
        """Route bare free text through the intent gate (issue #5).

        Only reached for a message that hit none of the deterministic short-circuits
        (pending-save affirmative, ``capture:``/``note:``/``save:`` prefix, bare URL,
        shared file). The injected :class:`~thoth.intent.IntentClassifier` -- when wired
        -- chooses the engine:

        * ``capture`` files the text as a note, appending :data:`_GATE_CAPTURE_HINT`
          so a misfile is recoverable in one reply;
        * ``query`` runs the vault-only :meth:`thoth.query.QueryEngine.answer`;
        * ``ask`` (the default, and the low-confidence fallback) takes the blended
          web+vault path when a research engine is wired, else the vault-only query.

        With no classifier wired the route is always ``ask``, so the pre-gate behaviour
        is preserved exactly: blended ask when research is wired, vault-only query
        otherwise.

        The gate's keywords (issue #102) ride along on the :class:`~thoth.intent.
        IntentDecision` and are passed to both read paths (:meth:`_do_query` /
        :meth:`_do_ask`) as ``search_terms`` to seed the lexical grep; capture ignores
        them (it has its own classify/curate enrichment). On the research-prefix / no-
        classifier ``ask`` fallback there are no keywords, so the read path greps the
        raw text -- unchanged behaviour.
        """
        decision = self._free_text_route(text)
        route = decision.route
        keywords = list(decision.keywords)
        # Concise operator-readable line (issue #52): the engine bare free text was
        # routed to (capture / query / ask), so a misroute is visible in the log.
        logger.info("slack routed free text to %s", route)
        if route == "capture":
            capture = Capture(text=text, source=source)
            self._do_ingest(capture, responder, hint=_GATE_CAPTURE_HINT)
        elif route == "query":
            self._do_query(text, responder, search_terms=keywords)
        elif self.research is not None:
            self._do_ask(text, thread, responder, search_terms=keywords)
        else:
            self._do_query(text, responder, search_terms=keywords)

    def _free_text_route(self, text: str) -> IntentDecision:
        """Pick the routing verdict for bare free text (issue #5 + #102 keywords).

        Returns the safe ``ask`` decision (no keywords) when no classifier is wired (the
        pre-gate default, so the research/query fallback in :meth:`_route_free_text` is
        unchanged) **and** when the text carries the explicit ``research:`` marker --
        that marker is a deterministic "ask the web" escape hatch (issue #5) and skips
        the gate to reach :meth:`thoth.research.ResearchEngine.ask`, which strips it and
        forces the web on; classifying it could misroute it to capture/query. Otherwise
        it consults the gate and returns the full :class:`~thoth.intent.IntentDecision`,
        whose :attr:`~thoth.intent.IntentDecision.route` already collapses a low-
        confidence verdict to the safe ``ask`` and whose ``keywords`` seed the read
        path's grep (issue #102). The classifier is itself total, so a model/parse
        failure also yields the safe default rather than raising.
        """
        if self.intent_classifier is None or force_web_requested(text):
            return _ASK_FALLBACK_DECISION
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
        try:
            report = self.ingestor.ingest(capture)
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
                ahead, since = -1, None
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

    def _do_ask(
        self,
        text: str,
        thread: str,
        responder: Responder,
        *,
        search_terms: list[str] | None = None,
    ) -> None:
        """Run the blended web+vault ask, reply with the offer-to-save, and remember it.

        Posts an immediate ":mag: Looking…" placeholder (issue #34, Slice B) then edits
        it in place with the rendered answer; degrades to a single reply on a
        client-less path.

        The (model-decided) web gate lives in :meth:`thoth.research.ResearchEngine.ask`;
        a leading ``research:`` marker / ``force_web`` forces the web on. On success the
        rendered answer carries the "save this answer?" offer and the
        ``(question, result)`` is stashed in :attr:`pending_saves` **keyed by the
        conversation thread** (issue #61) so a follow-up "y" in that thread files it. A
        :class:`~thoth.research.ResearchError` is rendered fail-loud. ``search_terms``
        are the intent gate's keywords (issue #102): they seed the vault candidate grep
        while the web gate and prose stay keyed off ``text``; empty/``None`` greps it.
        """
        assert self.research is not None  # routing guard guarantees this
        responder.progress(_ASK_PLACEHOLDER)
        try:
            result = self.research.ask(text, search_terms=search_terms)
        except BudgetExceededError:
            responder.finish(_BUDGET_REACHED_TEXT)
            return
        except ResearchError as exc:
            responder.finish(f":x: Could not answer that: {exc}")
            return
        offer = bool(result.answer.strip())
        self.pending_saves.remember(thread, text, result)
        responder.finish(render_ask_result(result, offer_save=offer))

    def _try_confirm_save(self, thread: str, responder: Responder) -> bool:
        """File the thread's pending answer as a ``notes/`` page on a "y" reply.

        ``thread`` is the conversation key (``thread_ts or ts``, issue #61), so a "y"
        only confirms the offer made *in its own thread* -- a "y" elsewhere finds
        nothing pending and falls through. Returns ``True`` once it has handled the
        confirmation (whether the save succeeded, was rejected, or there was nothing
        pending so the "y" falls through to normal routing -- in which case it returns
        ``False``). A vault rejection is rendered fail-loud. ``research`` is required to
        save; if it is not wired the reply falls through.

        The success reply uses the one concise shared reference (issue #53): an
        ``<obsidian-uri|title>: path`` line whose title is derived from the saved page's
        slug. When the ``obsidian://`` link cannot be built the plain ``Saved `path```
        fallback is used. It is posted in the thread via the responder.
        """
        if self.research is None:
            return False
        pending = self.pending_saves.take(thread)
        if pending is None:
            return False
        question, result = pending
        try:
            rel = self.research.save_answer(question, result)
        except ResearchError as exc:
            responder.say(f":x: Could not save that: {exc}")
            return True
        uri = self._vault_uri(rel)
        if uri:
            title = Path(rel).stem.replace("-", " ").title()
            responder.say(
                "Saved " + render_vault_ref(obsidian_uri=uri, title=title, path=rel)
            )
        else:
            responder.say(f"Saved `{rel}`")
        return True

    def _vault_uri(self, rel: str) -> str | None:
        """Build the ``obsidian://`` link for a saved page, or ``None`` on rejection."""
        try:
            return self.config.obsidian_uri(rel)
        except ValueError:
            return None

    def _source_label(self) -> str:
        """The vault ``source`` value for Slack-originated captures."""
        return "slack"

    @staticmethod
    def _channel(event: dict[str, Any]) -> str:
        """Return the event's channel id (where a reply is posted; the gate target)."""
        value = event.get("channel")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _conversation_key(event: dict[str, Any]) -> str:
        """Return the conversation thread key for the event: ``thread_ts or ts``.

        The per-conversation state key (issue #61), and the ``thread_ts`` the bot
        replies under: a reply *inside* a thread carries the thread root's ``thread_ts``
        (so a follow-up / save "y" keys to the same conversation as the top-level one),
        while a top-level message has only its own ``ts`` (which becomes the thread root
        once the bot replies under it). No fallback to the bare channel -- that would
        reintroduce the cross-topic collision issue #61 exists to remove.
        """
        thread_ts = event.get("thread_ts")
        if isinstance(thread_ts, str) and thread_ts:
            return thread_ts
        ts = event.get("ts")
        return ts if isinstance(ts, str) else ""

    @staticmethod
    def _is_confirm_save(text: str) -> bool:
        """Return ``True`` iff the whole message is a save-confirmation word."""
        return text.strip().lower() in _CONFIRM_WORDS

    @staticmethod
    def _should_handle(event: dict[str, Any]) -> bool:
        """Drop bot messages and echoes, and every subtype EXCEPT ``file_share``.

        A plain top-level message and an in-thread reply both have no subtype, so both
        are handled. The bot's own in-thread replies carry ``bot_id`` and are dropped
        here, so the daemon never loops on them. Channel subtypes -- edits/deletes
        (``message_changed`` / ``message_deleted``), joins/leaves (``channel_join`` when
        the bot is invited), and the thread-also-to-channel rebroadcast
        (``thread_broadcast``) -- are all dropped. The one subtype kept is
        ``file_share``: a channel file upload arrives as a ``message`` with that subtype
        carrying the **full** ``files`` objects (download URL + name) and a usable
        ``channel`` -- that is the event :meth:`handle_message` ingests an upload from.
        Slack also emits a separate ``file_shared`` event, but it embeds only a
        ``{"id": ...}`` stub (no URL, no conversation ``channel`` to reply in), so the
        appliance ignores it (see :func:`build_app`) and there is no cross-handler
        double-processing.
        """
        if event.get("bot_id"):
            return False
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return False
        return True

    @staticmethod
    def _event_key(event: dict[str, Any]) -> str:
        """Pick the most stable redelivery key Slack offers for this event."""
        for key in ("event_id", "client_msg_id", "file_id", "ts"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        """Return ``True`` iff the whole message is a single ``http(s)`` URL."""
        if " " in text or "\n" in text:
            return False
        return text.startswith("http://") or text.startswith("https://")

    @staticmethod
    def _is_capture_text(text: str) -> bool:
        """Return ``True`` iff the text carries an explicit capture prefix."""
        lowered = text.lower()
        return any(lowered.startswith(prefix) for prefix in _CAPTURE_PREFIXES)

    @staticmethod
    def _strip_capture_prefix(text: str) -> str:
        """Strip the leading ``capture:``/``note:``/``save:`` marker from text."""
        lowered = text.lower()
        for prefix in _CAPTURE_PREFIXES:
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    @staticmethod
    def _is_image_file(file_info: dict[str, Any]) -> bool:
        """Report whether a Slack file object is an image (issue #84 batch gate).

        Used to decide whether a multi-file upload is a homogeneous image batch
        (captured as one page). Prefers Slack's own ``mimetype`` (``image/...``), then
        falls back to the filename extension so a file object without a mimetype still
        routes; both mirror the image extensions the ingest pipeline recognises.
        """
        mimetype = file_info.get("mimetype")
        if isinstance(mimetype, str) and mimetype.lower().startswith("image/"):
            return True
        name = file_info.get("name")
        if isinstance(name, str) and "." in name:
            return name.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS
        return False

    @staticmethod
    def _download_url(file_info: dict[str, Any]) -> str | None:
        """Pick the private download URL Slack exposes on a file object."""
        for key in ("url_private_download", "url_private"):
            value = file_info.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _download_bytes(client: SlackClientLike | None, url: str) -> bytes:
        """Download private Slack file bytes via an authenticated request.

        A test injects a fake client exposing :meth:`download` (used directly, no
        network). The real ``slack_sdk.WebClient`` has **no** download helper, so the
        bytes are fetched with an authenticated ``GET`` to the file's private URL using
        the client's bot ``token`` (``Authorization: Bearer ...``) -- the only way to
        read a ``url_private``/``url_private_download`` link. Raises :class:`SlackError`
        when there is no usable download path or the URL is not an ``https`` Slack URL.
        """
        downloader = getattr(client, "download", None)
        if callable(downloader):
            data: Any = downloader(url)
            return bytes(data)
        token = getattr(client, "token", None)
        if not token:
            raise SlackError("Slack client has no token to download the file")
        if not url.startswith("https://"):
            raise SlackError(f"refusing to download a non-https file URL: {url!r}")
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return bytes(response.read())


def _build_handlers(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
    *,
    research: ResearchEngine | None = None,
) -> tuple[Handlers, str]:
    """Construct the Slack :class:`Handlers` graph; return it with the bot token.

    Factored out of :func:`build_app` so the startup wiring -- the fail-fast required-
    config checks (both Slack tokens and, issue #61, ``SLACK_CAPTURE_CHANNEL``) and the
    collaborator construction -- is reachable and unit-testable **without** importing
    the optional ``slack_bolt`` dependency that :func:`build_app` needs for the ``App``
    itself (absent in CI). The required-config checks run **first**, before any
    collaborator is
    built, so a missing token / capture channel raises
    :class:`~thoth.config.ConfigError` at startup rather than after side effects (e.g.
    opening the state DB).

    Returns:
        A ``(handlers, bot_token)`` pair: the wired :class:`Handlers` and the Slack bot
        token :func:`build_app` passes to the ``App``.
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
    # shares the same state.db counters as the ingest/query/research graph and alerts
    # once per day via the same errors-to-Slack target the Handlers use.
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
        research=research,
        # Free-text intent gate (issue #5): one cheap Haiku call routes bare prose to
        # capture / vault-query / blended ask instead of always defaulting to ask. Its
        # own lazy LLM client (a different, cheaper model than the ask/curate Sonnet);
        # the model is overridable without a redeploy via THOTH_INTENT_MODEL.
        intent_classifier=IntentClassifier(
            LLM(config, guard=intent_guard),
            model=os.environ.get("THOTH_INTENT_MODEL") or DEFAULT_INTENT_MODEL,
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


def build_app(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
    *,
    research: ResearchEngine | None = None,
) -> Any:
    """Lazily import ``slack_bolt``, build the App, and register the handlers.

    ``slack_bolt`` is imported **inside** this function so module import stays CI-safe.
    The :class:`Handlers` graph (and the fail-fast required-config checks, including the
    dedicated ``SLACK_CAPTURE_CHANNEL`` the daemon listens/replies in, issue #61) is
    built by :func:`_build_handlers` -- factored out so that wiring is testable without
    ``slack_bolt``. The returned app delegates the ``message`` listener (which also
    carries file uploads, as a ``file_share`` subtype) to those handlers. The bare
    ``file_shared`` event is bound to a no-op (it is a stub the appliance ignores -- see
    :meth:`Handlers._should_handle`). The app is **not** started -- :func:`run` does
    that. When ``research`` is provided, free-text questions take the blended web+vault
    path with the offer-to-save (SPEC section 7.1); otherwise they take the vault-only
    path.

    Args:
        config: The frozen runtime config (provides the Slack bot token + capture
            channel).
        ingestor: The constructed ingest pipeline.
        query_engine: The constructed retrieval engine.
        research: The optional blended-ask engine for the free-text path.

    Returns:
        The configured ``slack_bolt.App`` instance (typed ``Any`` to avoid a top-level
        import of the optional dependency).
    """
    from slack_bolt import App

    handlers, bot_token = _build_handlers(
        config, ingestor, query_engine, research=research
    )
    app = App(token=bot_token)

    @app.event("message")
    def _on_message(
        event: dict[str, Any], client: Any, say: Callable[..., None]
    ) -> None:
        handlers.handle_message(event, say, client=client)

    # Slack emits a separate ``file_shared`` event for every upload, but it embeds only
    # a ``{"id": ...}`` stub (no download URL) and no conversation ``channel`` to reply,
    # so uploads are ingested from the ``message``/``file_share`` event above instead.
    # This no-op listener exists solely so Bolt does not log each such event as an
    # unhandled request (Bolt auto-acks it).
    @app.event("file_shared")
    def _on_file_shared(event: dict[str, Any]) -> None:
        return None

    return app


def run(
    config: Config,
    ingestor: Ingestor,
    query_engine: QueryEngine,
    *,
    research: ResearchEngine | None = None,
) -> None:
    """Build the app and block serving over Socket Mode (the daemon entry point).

    Lazily imports ``SocketModeHandler``, builds the app via :func:`build_app`, and
    calls ``handler.start()`` which blocks forever. This is the production entry point
    (``thoth slack``) and is never unit-tested live (CI has no Slack socket); the
    testable logic all lives on :class:`Handlers`. ``research`` enables the blended
    free-text path (SPEC section 7.1).

    Unattended observability (issue #15): the blocking serve is wrapped by
    :func:`serve_with_alerting` so an **unhandled** daemon exception is reported to the
    errors-to-Slack target (:class:`thoth.alerts.Alerter`) before the process exits and
    systemd restarts it -- otherwise a crash loop would be silent. The alert is
    best-effort and the original exception is always re-raised so systemd still sees the
    non-zero exit.

    Args:
        config: The frozen runtime config (provides both Slack tokens).
        ingestor: The constructed ingest pipeline.
        query_engine: The constructed retrieval engine.
        research: The optional blended-ask engine for the free-text path.
    """
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from thoth.alerts import make_alerter

    _, app_token = config.require_slack()
    app = build_app(config, ingestor, query_engine, research=research)
    alerter = make_alerter(config)
    serve_with_alerting(
        lambda: SocketModeHandler(app, app_token).start(),
        alerter,
    )


def serve_with_alerting(serve: Callable[[], None], alerter: AlerterLike) -> None:
    """Run ``serve`` (a blocking daemon loop), alerting on an unhandled exception.

    The top-level supervision seam (issue #15), factored out of :func:`run` so it is
    unit-testable without a real Slack socket: it invokes ``serve`` and, if it raises,
    posts an unhandled-exception alert via ``alerter`` (best-effort -- the alert post
    swallows its own errors) and then **re-raises** the original exception so the
    process still exits non-zero and systemd restarts (and rate-limits) it.

    A clean shutdown is *not* an incident: ``KeyboardInterrupt`` / ``SystemExit`` (how
    ``systemctl stop`` and a deploy restart unwind the blocking loop) re-raise silently,
    so a routine stop/restart does not post an alert (which would train the operator to
    ignore them). Only genuine crashes -- any other exception -- alert.

    Args:
        serve: The blocking daemon entry (e.g. ``SocketModeHandler(...).start``).
        alerter: The errors-to-Slack alerter (a :class:`thoth.alerts.Alerter`).
    """
    try:
        serve()
    except (KeyboardInterrupt, SystemExit):
        # A clean stop (SIGTERM/Ctrl-C) is not a crash -- exit quietly, no alert.
        raise
    except BaseException as exc:  # noqa: BLE001 - report ANY real crash, then re-raise
        alerter.alert_exception("slack daemon", exc)
        raise
