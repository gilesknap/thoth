"""The Slack Socket-Mode daemon and its pure, unit-testable handler logic.

This package is the appliance's primary capture/retrieve surface (SPEC sections 6, 7
and 10). It wires a Slack `Bolt <https://slack.dev/bolt-python>`_ Socket-Mode app to
collaborators that are constructed elsewhere and injected here: an
:class:`~thoth.ingest.Ingestor` (capture) and a :class:`~thoth.query.QueryEngine` (fast
vault-only retrieve, which backs the free-text query path). The
daemon listens in **one dedicated private channel** (``SLACK_CAPTURE_CHANNEL``, you plus
the bot) and ignores every other conversation (issue #61): each top-level message starts
a new capture/query handled **in its own thread** (the bot replies under the originating
message's ``ts``), and a reply *inside* that thread continues it -- so per-conversation
state is keyed by **thread**, not channel, and two interleaved topics never clobber each
other. A file upload arrives as a ``message`` with subtype ``file_share``
carrying the full file objects. The daemon gates each message through an allow-list and
a transient redelivery dedupe, routes a bare URL / uploaded file to an ingest and sends
**bare free text** through a lightweight intent gate
(:class:`~thoth.intent.IntentClassifier`, issue #5) that picks capture / vault-query --
falling back to the safe vault-only query when no classifier is wired. It replies in
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
  package performs no heavy import and spins up no socket.
* This package **never builds an** ``obsidian://`` **link itself**. Links are built by
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

Only the standard library, ``httpx`` (a base dependency) and ``thoth`` modules are
imported at module level, so the package is always import-safe under pytest collection.
"""

from .daemon import build_app, run, serve_with_alerting
from .dedupe import DEDUPE_TTL_SECONDS, EventDedupe
from .files import SlackError
from .handlers import AlerterLike, Handlers, parse_allowed_users
from .handlers import _build_handlers as _build_handlers
from .rendering import render_citation, render_ingest_report, render_query_result
from .responder import Responder, SlackClientLike

__all__ = [
    "DEDUPE_TTL_SECONDS",
    "AlerterLike",
    "EventDedupe",
    "Handlers",
    "Responder",
    "SlackClientLike",
    "SlackError",
    "build_app",
    "parse_allowed_users",
    "render_citation",
    "render_ingest_report",
    "render_query_result",
    "run",
    "serve_with_alerting",
]
