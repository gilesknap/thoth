"""Shared MCP server contract: constants, errors and the tool injection bundle."""

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

The HTTP transport binds loopback by design -- network exposure is delegated to a
cloudflared tunnel + Cloudflare Access in front of it (ADR 0011), never a raw
``0.0.0.0`` socket. Override with ``--host`` only when you understand the consequence.
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
    """Raised for an MCP wiring failure, such as a missing collaborator."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured outcome of a ``pkm_*`` tool, rendered by the MCP host.

    Attributes:
        ok: ``True`` on success, and ``False`` when a typed collaborator error was
            caught and surfaced, since the tool never raises into the MCP runtime.
        text: A Markdown reply a chat host can display, in the MCP style of
            ``[label](obsidian-uri)``, the plain vault path and the
            ``[[wikilink]]``.
        data: A structured echo, of paths, ``used_web`` and the like, for a programmatic
            caller wanting the fields rather than the rendered prose.
    """

    ok: bool
    text: str
    data: dict[str, Any]


@dataclass
class ToolContext:
    """The single injection bundle the ``pkm_*`` tools delegate through.

    Holds the frozen config and the already-constructed Phase 0-3 collaborators. The
    tool functions take it explicitly, the FastMCP wrappers in :func:`build_server`
    closing over one instance and forwarding the same arguments, so each tool is a pure,
    testable delegation with no global state.

    Attributes:
        config: The frozen runtime configuration.
        vault: The path-confined read and write vault facade, the only disk surface.
        ingestor: The constructed ingest pipeline, behind ``pkm_ingest``.
        query_engine: The vault-only retrieval engine, behind ``pkm_search``.
        git: The vault git two-way sync that commits and pushes the disk writes the
            write tools make, such as ``pkm_write_page``, staging exactly the path each
            wrote and mirroring ``pkm_ingest``'s commit discipline (issue #85).
    """

    config: Config
    vault: Vault
    ingestor: Ingestor
    query_engine: QueryEngine
    git: GitSync


def _reject_outside(path: str) -> ToolResult:
    """Build the path-confinement rejection for a path outside the vault root."""
    return ToolResult(
        ok=False,
        text=f"Path is outside the vault and was rejected: `{path}`",
        data={"rejected": "path_confinement", "path": path},
    )
