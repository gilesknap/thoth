"""Shared MCP server contract: constants, errors, and the tool injection bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thoth.config import Config
from thoth.git_sync import GitSync
from thoth.ingest import Ingestor
from thoth.query import QueryEngine
from thoth.vault import Vault

SERVER_NAME: str = "thoth"
"""The MCP server name advertised to the host (``FastMCP(SERVER_NAME)``)."""

DEFAULT_MCP_HOST: str = "127.0.0.1"
"""Default HTTP bind address: loopback only (issue #103).

Network exposure is delegated to a cloudflared tunnel and Cloudflare Access in front of
it (ADR 0011), never a raw ``0.0.0.0`` socket. Override with ``--host`` only when you
understand the consequence.
"""

DEFAULT_MCP_PORT: int = 8765
"""Default HTTP listen port for ``thoth mcp --transport http`` (issue #103)."""

TOOL_NAMES: tuple[str, ...] = (
    "pkm_ingest",
    "pkm_search",
    "pkm_todos",
    "pkm_recent",
    "pkm_write_page",
    "pkm_read_page",
    "pkm_edit_page",
)
"""The exact tools :func:`build_server` registers (one per ``pkm_*`` function)."""


class McpServerError(Exception):
    """Raised for an MCP wiring failure (for example a missing collaborator)."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured outcome of a ``pkm_*`` tool, rendered by the MCP host.

    Attributes:
        ok: False when a typed collaborator error was caught, since a tool never raises
        text: A Markdown reply for a chat host to display, link plus path plus wikilink
        data: A structured echo of the paths and flags, for programmatic callers
    """

    ok: bool
    text: str
    data: dict[str, Any]


@dataclass
class ToolContext:
    """The single injection bundle the ``pkm_*`` tools delegate through.

    Holds the frozen config and the already-constructed collaborators. The tool
    functions take this explicitly, and the FastMCP wrappers in :func:`build_server`
    close over one instance, so each tool is a pure testable delegation with no global
    state.

    Attributes:
        config: The frozen runtime configuration
        vault: The path-confined read/write facade, the only disk surface
        ingestor: The constructed ingest pipeline, behind ``pkm_ingest``
        query_engine: The vault-only retrieval engine, behind ``pkm_search``
        git: The two-way sync that commits each write tool's path (issue #85)
    """

    config: Config
    vault: Vault
    ingestor: Ingestor
    query_engine: QueryEngine
    git: GitSync


def _reject_outside(path: str) -> ToolResult:
    """Builds the rejection for a path that resolves outside the vault root."""
    return ToolResult(
        ok=False,
        text=f"Path is outside the vault and was rejected: `{path}`",
        data={"rejected": "path_confinement", "path": path},
    )
