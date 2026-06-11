"""FastMCP stdio server exposing the ``pkm_*`` tools over the closed vault surface.

This is the appliance's Model-Context-Protocol entry point (SPEC sections 2, 3 and 6).
It publishes seven tools -- :func:`pkm_ingest`, :func:`pkm_search`, :func:`pkm_todos`,
:func:`pkm_recent`, :func:`pkm_write_page`, :func:`pkm_read_page` and
:func:`pkm_edit_page` -- each of which is a *pure delegation* to an already-validated
Phase 0-3 collaborator:

* ``pkm_ingest``   -> :meth:`thoth.ingest.Ingestor.ingest`
* ``pkm_search``   -> :meth:`thoth.query.QueryEngine.answer`
* ``pkm_todos``    -> the canonical action scans on :class:`thoth.summary.SummaryEngine`
* ``pkm_recent``   -> :meth:`thoth.summary.SummaryEngine.recent_pages`
* ``pkm_write_page`` -> :meth:`thoth.vault.Vault.write_page`
* ``pkm_read_page`` -> :meth:`thoth.vault.Vault.read_page` (the verbatim read half of
  the read -> modify -> write-back round trip)
* ``pkm_edit_page`` -> a unique-substring body replace that writes back through
  :func:`pkm_write_page` (so the validation + commit apply, the targeted-edit primitive)

The closed-surface promise (SPEC section 3) is preserved here exactly as it is in
``slack_app.py``: the LLM driving this server gets no shell and no arbitrary file
access. Every path is confined by the :class:`~thoth.vault.Vault`, binary bytes never
travel as base64 (``pkm_ingest`` rejects a base64/data-URI argument and accepts only
text, a URL, or a server-resolvable in-vault path), and the ``obsidian://`` links in
every reply are harness-built (unfabricable) by the underlying collaborators, never
invented by the model.

The tool *bodies* take an explicit :class:`ToolContext` -- the single injection bundle
of collaborators -- so they are ordinary, fully testable functions: a test exercises
each with fakes (recording the delegated call) and a real :class:`~thoth.vault.Vault`
over a temporary vault, with no ``mcp`` package and no live stdio. ``mcp`` / ``FastMCP``
is imported **lazily** inside :func:`build_server` and :func:`run` only, so importing
this package is always CI-safe -- only the standard library and ``thoth.*`` are imported
at module level. Each ``pkm_*`` function catches the relevant typed errors and returns a
``ToolResult(ok=False, ...)`` rather than raising into the MCP runtime.
"""

from __future__ import annotations

from .context import DEFAULT_MCP_HOST as DEFAULT_MCP_HOST
from .context import DEFAULT_MCP_PORT as DEFAULT_MCP_PORT
from .context import (
    SERVER_NAME,
    TOOL_NAMES,
    McpServerError,
    ToolContext,
    ToolResult,
)
from .server import build_server, run
from .tools_ingest import pkm_ingest
from .tools_pages import pkm_edit_page, pkm_read_page, pkm_write_page
from .tools_query import pkm_recent, pkm_search, pkm_todos

__all__ = [
    "SERVER_NAME",
    "TOOL_NAMES",
    "ToolContext",
    "ToolResult",
    "McpServerError",
    "pkm_ingest",
    "pkm_search",
    "pkm_todos",
    "pkm_recent",
    "pkm_write_page",
    "pkm_read_page",
    "pkm_edit_page",
    "build_server",
    "run",
]
