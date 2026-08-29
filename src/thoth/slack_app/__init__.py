"""The Slack Socket-Mode daemon and its pure, unit-testable handler logic.

The appliance's primary capture and retrieve surface (SPEC sections 6, 7 and 10). It
wires a Bolt Socket-Mode app to collaborators constructed elsewhere and injected here:
an ingestor for capture and a query engine for the vault-only read path.

The daemon listens in one dedicated private channel and ignores every other conversation
(issue #61). Each top-level message starts a capture or query handled in its own thread,
and a reply inside that thread continues it, so per-conversation state is keyed by
thread rather than channel and two interleaved topics never clobber each other. Each
message is gated through an allow-list and a redelivery dedupe.

A bare URL or an uploaded file routes straight to ingest, and bare free text goes
through the intent gate (issue #5), falling back to the safe vault-only query when no
classifier is wired.

A slow request shows an immediate placeholder that is edited in place with the final
render (issue #34), so a multi-second capture is not a dead pause, and it degrades to a
single reply on a client-less path. Four constraints are enforced here:

* ``slack_bolt`` is never imported at module level, since it is absent in CI, and is
  pulled in lazily inside the two entry points. Everything else is pure and unit-tested
  with fakes, so importing this package spins up no socket.
* This package never builds a deep link itself. Links arrive already formed from the
  harness, and the renderers only format those unfabricable values. Every reference goes
  through the one shared helper as a title-only clickable link (issue #63), deliberately
  dropping the trailing path and the dead wikilink, which Slack cannot click.
* File uploads are downloaded server-side to a temporary file and handed over as a path
  capture, never as base64. A non-allowed user is rejected before any download.
* :class:`EventDedupe` is the redelivery seam (SPEC section 10), a fast in-memory TTL
  cache backed by a durable row in the state DB, so a redelivery straddling a restart is
  still recognised. The memory cache alone is lost on a restart the table survives.

Only the standard library, ``httpx`` and ``thoth`` modules are imported at module level,
so the package is always import-safe under pytest collection.
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
