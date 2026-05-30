"""The Slack Socket-Mode daemon and its pure, unit-testable handler logic.

This module is the appliance's primary capture/retrieve surface (SPEC sections 6, 7
and 10). It wires a Slack `Bolt <https://slack.dev/bolt-python>`_ Socket-Mode app to
two collaborators that are constructed elsewhere and injected here: an
:class:`~thoth.ingest.Ingestor` (capture) and a :class:`~thoth.query.QueryEngine`
(retrieve). The daemon listens for ``message.im`` and ``file_shared`` events, gates
them through an allow-list and a transient redelivery dedupe, routes free text to a
query and a bare URL / file to an ingest, and replies in Slack ``mrkdwn``.

Design constraints enforced here:

* ``slack_bolt`` is **never** imported at module top level (it is absent in CI). It is
  imported lazily, only inside :func:`build_app` and :func:`run`. Everything else --
  the allow-list parser, the ``mrkdwn`` renderers, the :class:`EventDedupe`, and the
  :class:`Handlers` logic -- is pure and unit-tested with fakes, so importing this
  module performs no heavy import and spins up no socket.
* This module **never builds an** ``obsidian://`` **link itself**. Links are built by
  the harness (``Vault.obsidian_uri`` via the query/ingest layers) and arrive already
  formed on :class:`~thoth.query.Citation` and :class:`~thoth.ingest.IngestReport`; the
  renderers here only format those unfabricable values. Per the SPEC Appendix, every
  citation also carries the plain vault-relative path and ``[[wikilink]]`` so a host
  that will not make the custom scheme clickable still shows a usable reference.
* File uploads are downloaded **server-side** to a temporary file and handed to the
  ingestor as :class:`~thoth.ingest.Capture` with a ``path`` -- never as base64
  (SPEC section 6 capture note). A non-allowed user is rejected before any download.
* :class:`EventDedupe` is a transient in-memory TTL set (SPEC section 10); it is the
  seam behind which the Phase 3 SQLite ``processed_events`` table will later sit.

Only the standard library plus ``thoth`` modules are imported at module level, so the
module is always import-safe under pytest collection.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from thoth.config import Config
from thoth.git_sync import VaultConflictError
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.query import Citation, QueryEngine, QueryError, QueryResult

DEDUPE_TTL_SECONDS: float = 3600.0
"""Prune processed-event ids older than one hour (SPEC section 10)."""

# A free-text message whose body, once stripped, begins with one of these prefixes is
# routed to ingest-as-text rather than query (an explicit "save this thought" signal).
_CAPTURE_PREFIXES: tuple[str, ...] = ("capture:", "note:", "save:")

# The polite refusal sent to a user who is not on the allow-list, if anything at all.
_REFUSAL_TEXT: str = "Sorry, you are not authorised to use this assistant."


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
    """Render one citation as ``mrkdwn``: link, plain path, and ``[[wikilink]]``.

    Emits ``<obsidian-uri|title>`` (the Slack link form), then the plain vault-relative
    path and the ``[[wikilink]]`` on the same line so the reference is still usable when
    a host will not make the custom scheme clickable (SPEC Appendix). The link target
    is taken verbatim from the harness-built :class:`~thoth.query.Citation`; this
    function never constructs an ``obsidian://`` URI itself.

    Args:
        citation: A harness-built citation handle.

    Returns:
        A single ``mrkdwn`` line for the citation.
    """
    label = citation.title or citation.path
    return f"<{citation.obsidian_uri}|{label}> - `{citation.path}` {citation.wikilink}"


def render_query_result(result: QueryResult) -> str:
    """Render a composed answer plus its citation list as a ``mrkdwn`` block.

    The answer prose comes first, followed by a ``Sources:`` list with one
    :func:`render_citation` line per cited page (SPEC Appendix worked example). When the
    answer has no citations a short "no sources" note is appended so the reply never
    silently implies a fabricated source.

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
    else:
        lines.append("")
        lines.append("_No vault sources cited._")
    return "\n".join(lines)


def render_ingest_report(report: IngestReport) -> str:
    """Render a one-to-two-line capture confirmation in ``mrkdwn``.

    Names what was filed (the page paths, or raw/asset paths when no curated page was
    written) and lists every harness-built ``obsidian://`` link and ``[[wikilink]]`` the
    report carries (SPEC step 8). A :attr:`~thoth.ingest.IngestReport.conflict` is
    surfaced fail-loud (SPEC section 10) with the conflicting path, never swallowed.

    Args:
        report: The structured ingest outcome.

    Returns:
        A concise ``mrkdwn`` confirmation (or conflict) string.
    """
    if report.conflict:
        detail = report.message or "a vault conflict blocked the sync"
        return f":warning: *Vault conflict* - {detail}. Content was filed locally."

    filed = report.page_paths or report.raw_paths or report.asset_paths
    if filed:
        head = "Filed " + ", ".join(f"`{path}`" for path in filed)
    else:
        head = "Nothing new to file"
    if not report.committed:
        head += " (not yet committed)"

    parts = [head]
    refs: list[str] = []
    for uri, wikilink in zip(report.obsidian_links, report.wikilinks, strict=False):
        refs.append(f"<{uri}|open> {wikilink}")
    # Surface any remaining links/wikilinks if the two lists are uneven.
    for uri in report.obsidian_links[len(report.wikilinks) :]:
        refs.append(f"<{uri}|open>")
    for wikilink in report.wikilinks[len(report.obsidian_links) :]:
        refs.append(wikilink)
    if refs:
        parts.append(" - ".join(refs))
    if report.message and not report.conflict:
        parts.append(report.message)
    return "\n".join(parts)


class EventDedupe:
    """In-memory TTL set of processed Slack event ids (transient, never a store).

    Slack redelivers events on a missed ack, so each handler drops a redelivery by
    asking :meth:`seen` once per event. Entries older than ``ttl_seconds`` are pruned
    (SPEC section 10). The clock is injectable for deterministic tests. This is the seam
    the Phase 3 SQLite ``processed_events`` table swaps in behind unchanged.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEDUPE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Build an empty dedupe set.

        Args:
            ttl_seconds: How long a recorded event id is remembered before pruning.
            clock: A monotonic-ish time source returning seconds; defaults to
                :func:`time.monotonic`.
        """
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._seen: dict[str, float] = {}

    def seen(self, event_id: str) -> bool:
        """Report whether ``event_id`` was already processed, recording it if new.

        Prunes expired entries first, then: if ``event_id`` is unknown it is recorded
        and ``False`` is returned (the caller should process it); if it is already known
        ``True`` is returned (the caller should drop the redelivery). An empty
        ``event_id`` is always treated as unseen and is never recorded (a missing id
        cannot be deduped).

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
        self._seen[event_id] = self._clock()
        return False

    def mark(self, event_id: str) -> None:
        """Record ``event_id`` as processed now (no-op for an empty id)."""
        if event_id:
            self._seen[event_id] = self._clock()

    def prune(self) -> None:
        """Drop every recorded id older than ``ttl_seconds`` from now."""
        cutoff = self._clock() - self._ttl
        self._seen = {
            event_id: ts for event_id, ts in self._seen.items() if ts >= cutoff
        }


class SlackClientLike(Protocol):
    """The slice of the Bolt web client used by the handlers."""

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> Any:
        """Post a message to a channel (Slack ``chat.postMessage``)."""
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
    dedupe: EventDedupe = field(default_factory=EventDedupe)

    def is_allowed(self, user_id: str) -> bool:
        """Return ``True`` iff ``user_id`` is on the allow-list (fail-closed)."""
        return bool(user_id) and user_id in self.allowed_users

    def handle_message(self, event: dict[str, Any], say: Callable[[str], None]) -> None:
        """Gate, route, and reply to a ``message.im`` event.

        Ignores bot/own messages and message subtypes (edits, joins) so the daemon does
        not loop on its own replies. Enforces the allow-list (replying with a polite
        refusal to a known sender that is not allowed) and the redelivery dedupe. Routes
        a bare URL -- or text with a ``capture:``/``note:``/``save:`` prefix -- to
        :meth:`thoth.ingest.Ingestor.ingest`, and any other free text to
        :meth:`thoth.query.QueryEngine.answer`, replying with the matching ``mrkdwn``
        renderer. A surfaced :class:`~thoth.git_sync.VaultConflictError` is rendered
        fail-loud rather than swallowed.

        Args:
            event: The Slack event payload.
            say: A callable that posts a reply string back to the channel.
        """
        if not self._should_handle(event):
            return
        user = str(event.get("user", ""))
        if not self.is_allowed(user):
            say(_REFUSAL_TEXT)
            return
        if self.dedupe.seen(self._event_key(event)):
            return

        text = str(event.get("text", "")).strip()
        if not text:
            return
        source = self._source_label()
        if self._is_capture_text(text):
            capture = Capture(text=self._strip_capture_prefix(text), source=source)
            self._do_ingest(capture, say)
        elif self._looks_like_url(text):
            capture = Capture(url=text, source=source)
            self._do_ingest(capture, say)
        else:
            self._do_query(text, say)

    def handle_file_shared(
        self,
        event: dict[str, Any],
        client: SlackClientLike,
        say: Callable[[str], None],
    ) -> None:
        """Gate, download server-side, and ingest a ``file_shared`` event.

        Enforces the allow-list (rejecting a non-allowed user *before* any download) and
        the redelivery dedupe, then downloads the file bytes to a temporary path via the
        injected client and hands the ingestor a :class:`~thoth.ingest.Capture` carrying
        that ``path`` -- never base64 (SPEC section 6). The temporary file is left for
        the ingestor to consume (it moves binaries into the vault via ``save_asset``).

        Args:
            event: The Slack ``file_shared`` event payload (or its enclosing message).
            client: The Slack web client used to look up and download the file.
            say: A callable that posts a reply string back to the channel.
        """
        if not self._should_handle(event):
            return
        user = str(event.get("user", "") or event.get("user_id", ""))
        if not self.is_allowed(user):
            say(_REFUSAL_TEXT)
            return
        if self.dedupe.seen(self._event_key(event)):
            return

        file_info = self._file_info(event, client)
        url = self._download_url(file_info)
        if not url:
            say(":warning: Could not find a downloadable URL for that file.")
            return
        filename = file_info.get("name")
        suffix = Path(filename).suffix if filename else ""
        data = self._download_bytes(client, url)
        with tempfile.NamedTemporaryFile(
            prefix="thoth-upload-", suffix=suffix, delete=False
        ) as handle:
            handle.write(data)
            tmp_path = Path(handle.name)
        capture = Capture(
            path=tmp_path,
            source=self._source_label(),
            filename=filename if isinstance(filename, str) else None,
        )
        self._do_ingest(capture, say)

    # ---- internals ---------------------------------------------------------------

    def _do_ingest(self, capture: Capture, say: Callable[[str], None]) -> None:
        """Run an ingest and reply; render a conflict/error fail-loud, never crash."""
        try:
            report = self.ingestor.ingest(capture)
        except VaultConflictError as exc:
            say(f":warning: *Vault conflict* - {exc}. Resolve in Obsidian, then retry.")
            return
        except IngestError as exc:
            say(f":x: Could not file that: {exc}")
            return
        say(render_ingest_report(report))

    def _do_query(self, text: str, say: Callable[[str], None]) -> None:
        """Run a query and reply; render an error fail-loud, never crash."""
        try:
            result = self.query_engine.answer(text)
        except QueryError as exc:
            say(f":x: Could not answer that: {exc}")
            return
        say(render_query_result(result))

    def _source_label(self) -> str:
        """The vault ``source`` value for Slack-originated captures."""
        return "slack"

    @staticmethod
    def _should_handle(event: dict[str, Any]) -> bool:
        """Drop bot messages, our own echoes, and every message *subtype*.

        Any subtype is dropped, including ``file_share``: a file upload arrives as both
        a ``message`` (subtype ``file_share``) *and* a separate ``file_shared`` event,
        and the two handlers carry different dedupe keys, so letting
        :meth:`handle_message` also act on the upload message would double-process a
        captioned upload (the caption would be ingested or queried while
        :meth:`handle_file_shared` ingests the file). The file is therefore handled on
        exactly one path -- the ``file_shared`` listener -- and an upload's caption is
        intentionally ignored here.
        """
        if event.get("bot_id"):
            return False
        # message_changed / message_deleted / channel_join / file_share etc. all carry
        # a subtype; a plain user DM has none. Drop them all so uploads are handled only
        # by handle_file_shared (no cross-handler double-processing).
        if event.get("subtype"):
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
    def _file_info(event: dict[str, Any], client: SlackClientLike) -> dict[str, Any]:
        """Resolve the file metadata dict for a ``file_shared`` event.

        Slack's ``file_shared`` event carries only a ``file_id``; the file's download
        URL lives behind ``files.info``. If the event already embeds a ``file`` object
        (some payload shapes do), that is used directly to save a round trip.
        """
        embedded = event.get("file")
        if isinstance(embedded, dict):
            return embedded
        file_id = event.get("file_id") or event.get("file", {})
        if isinstance(file_id, str) and file_id and hasattr(client, "files_info"):
            response = client.files_info(file=file_id)  # type: ignore[attr-defined]
            info = _response_value(response)
            file_obj = info.get("file") if isinstance(info, dict) else None
            if isinstance(file_obj, dict):
                return file_obj
        return {}

    @staticmethod
    def _download_url(file_info: dict[str, Any]) -> str | None:
        """Pick the private download URL Slack exposes on a file object."""
        for key in ("url_private_download", "url_private"):
            value = file_info.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _download_bytes(client: SlackClientLike, url: str) -> bytes:
        """Download file bytes via the client's authenticated transport.

        Bolt's ``WebClient`` exposes a token-bearing ``token`` attribute and an HTTP
        helper; tests inject a fake exposing :meth:`download` so no real network call is
        made. Raises :class:`SlackError` if the client cannot download.
        """
        if hasattr(client, "download"):
            return bytes(client.download(url))  # type: ignore[attr-defined]
        raise SlackError("Slack client cannot download file bytes")


def _response_value(response: Any) -> Any:
    """Return a Slack SDK response's payload as a mapping where possible.

    Bolt's ``SlackResponse`` is mapping-like and also carries a ``.data`` attribute;
    a fake may simply return a ``dict``. This normalises both to the underlying mapping.
    """
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data
    return response


def build_app(config: Config, ingestor: Ingestor, query_engine: QueryEngine) -> Any:
    """Lazily import ``slack_bolt``, build the App, and register the handlers.

    ``slack_bolt`` is imported **inside** this function so module import stays CI-safe.
    The returned app is fully wired (``message`` and ``file_shared`` listeners delegate
    to a :class:`Handlers` built from the injected collaborators and the allow-list read
    from ``SLACK_ALLOWED_USERS``) but is **not** started -- :func:`run` does that.

    Args:
        config: The frozen runtime config (provides the Slack bot token).
        ingestor: The constructed ingest pipeline.
        query_engine: The constructed retrieval engine.

    Returns:
        The configured ``slack_bolt.App`` instance (typed ``Any`` to avoid a top-level
        import of the optional dependency).
    """
    from slack_bolt import App

    bot_token, _ = config.require_slack()
    handlers = Handlers(
        config=config,
        ingestor=ingestor,
        query_engine=query_engine,
        allowed_users=parse_allowed_users(os.environ.get("SLACK_ALLOWED_USERS")),
    )
    app = App(token=bot_token)

    @app.event("message")
    def _on_message(event: dict[str, Any], say: Callable[[str], None]) -> None:
        handlers.handle_message(event, say)

    @app.event("file_shared")
    def _on_file_shared(
        event: dict[str, Any], client: Any, say: Callable[[str], None]
    ) -> None:
        handlers.handle_file_shared(event, client, say)

    return app


def run(config: Config, ingestor: Ingestor, query_engine: QueryEngine) -> None:
    """Build the app and block serving over Socket Mode (the daemon entry point).

    Lazily imports ``SocketModeHandler``, builds the app via :func:`build_app`, and
    calls ``handler.start()`` which blocks forever. This is the production entry point
    and is never unit-tested live (CI has no Slack socket); the testable logic all lives
    on :class:`Handlers`.

    Args:
        config: The frozen runtime config (provides both Slack tokens).
        ingestor: The constructed ingest pipeline.
        query_engine: The constructed retrieval engine.
    """
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    _, app_token = config.require_slack()
    app = build_app(config, ingestor, query_engine)
    SocketModeHandler(app, app_token).start()
