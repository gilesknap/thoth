"""FastMCP construction and the ``thoth mcp`` entry point (mcp imported lazily)."""

from __future__ import annotations

from typing import Any

from thoth.config import Config

from .context import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    SERVER_NAME,
    ToolContext,
    ToolResult,
)
from .http import _run_http
from .tools_ingest import pkm_ingest
from .tools_pages import pkm_edit_page, pkm_read_page, pkm_write_page
from .tools_query import pkm_recent, pkm_search, pkm_todos


def build_server(ctx: ToolContext) -> Any:
    """Lazily imports FastMCP, builds the server, and registers the tools.

    The ``mcp`` package is imported inside this function so module import stays CI-safe.
    One tool is registered per name, each forwarding to the matching pure function bound
    to the context, so the registered callables carry the same keyword arguments. The
    server is returned but not started, which :func:`run` does.

    Args:
        ctx: The injected collaborator bundle every tool delegates through.

    Returns:
        The configured server, typed loosely to avoid a top-level import of the
        optional dependency.
    """
    from mcp.server.fastmcp import FastMCP

    server: Any = FastMCP(SERVER_NAME)

    @server.tool(name="pkm_ingest")
    def _ingest(
        text: str | None = None, url: str | None = None, path: str | None = None
    ) -> ToolResult:
        """Capture text, a URL, or a server-resolvable in-vault path into the vault."""
        return pkm_ingest(ctx, text=text, url=url, path=path)

    @server.tool(name="pkm_search")
    def _search(
        query: str,
        max_pages: int = 5,
        search_keywords: list[str] | None = None,
    ) -> ToolResult:
        """Run a fast, vault-only lookup and return the answer with citations.

        Pass `search_keywords` to seed the vault's lexical search: extract them
        from the user's request, de-pluralise to the singular (dogs -> dog),
        drop stop words (list, me, the, about, what, show), and expand obvious
        synonyms (dog -> Labradoodle, pet). The grep matches on whole words, so
        an un-singularised plural will miss singular page content. Omit it only
        when the query is already a single bare keyword.

        When relaying the result, preserve each source's clickable
        `obsidian://open?...` link verbatim -- present citations as those links,
        never flattened to bare `path/to/page.md` text.
        """
        return pkm_search(
            ctx, query=query, max_pages=max_pages, search_keywords=search_keywords
        )

    @server.tool(name="pkm_todos")
    def _todos(include_done: bool = False) -> ToolResult:
        """List open (and optionally done) actions from the vault frontmatter."""
        return pkm_todos(ctx, include_done=include_done)

    @server.tool(name="pkm_recent")
    def _recent(days: int = 7, limit: int = 20) -> ToolResult:
        """List recently created/updated curated pages from their frontmatter dates.

        When relaying the result, preserve each page's clickable
        `obsidian://open?...` link verbatim -- never flatten it to a bare path.
        """
        return pkm_recent(ctx, days=days, limit=limit)

    @server.tool(name="pkm_write_page")
    def _write_page(
        folder: str, slug: str, frontmatter: dict[str, Any], body: str
    ) -> ToolResult:
        """Write a page through the validated vault surface (the escape hatch)."""
        return pkm_write_page(
            ctx, folder=folder, slug=slug, frontmatter=frontmatter, body=body
        )

    @server.tool(name="pkm_read_page")
    def _read_page(path: str) -> ToolResult:
        """Read a page's raw frontmatter + body verbatim (by path or bare slug).

        Use this before editing an existing page so you read -> modify -> write back
        safely: the returned `frontmatter` and `body` round-trip into pkm_write_page
        or pkm_edit_page. `path` may be a full vault path (notes/foo.md) or a bare
        slug (foo) that resolves to a unique page.
        """
        return pkm_read_page(ctx, path=path)

    @server.tool(name="pkm_edit_page")
    def _edit_page(path: str, old_string: str, new_string: str) -> ToolResult:
        """Make a targeted, unique-substring replace on a page body, then commit.

        Prefer this over pkm_write_page to change an existing page (for example to
        add one link): `old_string` must occur exactly once in the body. The edit is
        written back through the validated surface, so it is committed and pushed
        like any write. `path` may be a full vault path or a bare unique slug.
        """
        return pkm_edit_page(
            ctx, path=path, old_string=old_string, new_string=new_string
        )

    return server


def run(
    config: Config,
    ctx: ToolContext | None = None,
    *,
    transport: str = "stdio",
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
) -> None:
    """Wires a context when needed and serves over the chosen transport.

    The production entry point for ``thoth mcp``. With no context it wires the full
    collaborator graph, the same shape the Slack daemon uses, then builds and runs the
    server. The transport selects how the server is exposed (issue #103):

    * ``stdio``, the default, is the spawn-as-a-child model Claude Code uses locally.
      The host and port are ignored and no socket is bound.
    * ``http`` binds the streamable-HTTP transport, on loopback by default, with network
      exposure delegated to cloudflared and Cloudflare Access (ADR 0011). Bearer auth is
      mandatory: the server fails fast at startup when no keys are set, never binding an
      unauthenticated socket, and also enforces the Cf-Access JWT when those settings
      are present.

    The collaborator construction and the lazy ``mcp`` import happen only here, so
    importing this module stays light. This is never unit-tested live, because CI has no
    stdio and no ``mcp`` package.

    Args:
        config: The frozen runtime config.
        ctx: An already-wired context for tests or embedding, built from config when
            None.
        transport: Either stdio or http.
        host: HTTP bind address, ignored for stdio.
        port: HTTP listen port, ignored for stdio.

    Raises:
        ValueError: if the transport is neither stdio nor http.
        ConfigError: over HTTP, when no bearer keys are set, refusing to bind an
            unauthenticated socket.
    """
    if transport not in ("stdio", "http"):
        raise ValueError(
            f"unknown MCP transport {transport!r}; expected 'stdio' or 'http'"
        )
    # Fail fast before wiring the graph or binding a socket, because an HTTP transport
    # with no bearer keys must never start (#103)
    if transport == "http":
        config.require_mcp_api_keys()

    if ctx is None:
        from thoth.budget import make_budget_guard
        from thoth.wiring import build_collaborators

        # The daily cost guard (issue #16), one shared cap persisted in state.db. MCP
        # has no Slack target, so it blocks silently while the cap still defers spend.
        # No markers either, since the heartbeat watches the capture path, not MCP
        built = build_collaborators(config, guard=make_budget_guard(config))
        ctx = ToolContext(
            config=config,
            vault=built.vault,
            ingestor=built.ingestor,
            query_engine=built.query_engine,
            git=built.git,
        )

    server = build_server(ctx)
    if transport == "http":
        _run_http(server, config, host=host, port=port)
    else:
        server.run(transport="stdio")
