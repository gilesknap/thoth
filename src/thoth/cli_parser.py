"""The ``thoth`` argument parser, split out of :mod:`thoth.__main__`.

Import safety: only the standard library plus the package version is imported here,
so building the parser -- and therefore ``--version`` / ``--help`` -- never needs the
heavy optional clients (``anthropic`` / ``slack_bolt`` / ``mcp``) to be installed.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from . import __version__

__all__ = ["build_parser"]


def build_parser() -> ArgumentParser:
    """Build the ``thoth`` argument parser with one subcommand per Phase-3 entrypoint.

    Subcommands: ``init`` (seed the vault spine + dashboards, idempotent,
    ``--force`` to overwrite), ``slack`` (the capture/retrieve daemon), ``mcp`` (the
    MCP server -- ``--transport stdio`` by default, ``--transport http`` for the
    bearer-authenticated network surface on ``--host``/``--port``, issue #103),
    ``reindex`` (nightly incremental, ``--full-rebuild`` for
    recovery, ``--budget`` for a transient cap override, issue #95), ``summary``
    (``daily`` / ``weekly`` Slack digest), ``lint`` (the
    13-check vault maintenance scan, ``--no-log`` to suppress the log entry), and
    ``capture`` (backfill files/folders through the ingest pipeline -- ``--as-is`` for
    a low-touch import, ``--budget`` for a transient cap override, plus
    ``--dry-run``/``--limit``/``--batch-size``/``--include``/``--exclude``, issue #80).
    ``-v/--version`` prints the version and exits.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = ArgumentParser(prog="thoth", description="thoth PKM appliance CLI")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="{init,vault-bootstrap,slack,mcp,reindex,summary,lint,capture}",
    )

    init = sub.add_parser("init", help="seed the vault spine + dashboards (idempotent)")
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing spine/dashboard files",
    )

    sub.add_parser(
        "vault-bootstrap",
        help="clone the vault repo into an empty $PKM_VAULT "
        "(no-op if already a git repo)",
    )

    sub.add_parser("slack", help="run the Slack Socket-Mode capture/retrieve daemon")

    mcp = sub.add_parser("mcp", help="serve the pkm_* tools over MCP (stdio or HTTP)")
    mcp.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio (default, spawn-as-child for Claude Code) or http (network "
        "streamable-HTTP, bearer-authenticated; THOTH_MCP_API_KEYS required) (#103)",
    )
    # Defaults mirror thoth.mcp_server.DEFAULT_MCP_HOST/PORT; kept as literals here so
    # parsing --help never imports the (heavy, mcp-dependent) server module. Loopback by
    # default by design: network exposure is delegated to cloudflared + Cloudflare
    # Access (ADR 0011), never a raw 0.0.0.0 socket.
    mcp.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (http transport only); loopback by default -- expose "
        "via cloudflared + Cloudflare Access, never bind 0.0.0.0 directly (#103)",
    )
    mcp.add_argument(
        "--port",
        type=int,
        default=8765,
        help="HTTP listen port (http transport only)",
    )

    reindex = sub.add_parser("reindex", help="reindex Hindsight from the vault")
    reindex.add_argument(
        "--full-rebuild",
        action="store_true",
        help="wipe the bank and re-retain every live page (recovery)",
    )
    reindex.add_argument(
        "--budget",
        type=int,
        default=None,
        help="override THOTH_DAILY_LLM_BUDGET for THIS run only (transient); "
        "0 = unlimited for this reindex (issue #95)",
    )

    summary = sub.add_parser("summary", help="compose + post a Slack digest")
    summary.add_argument(
        "kind",
        choices=("daily", "weekly"),
        help="which digest to compose and post",
    )
    summary.add_argument(
        "--skip-when-empty",
        action="store_true",
        help="do not post when there is nothing to report",
    )

    lint = sub.add_parser("lint", help="scan the vault for the 13 maintenance issues")
    lint.add_argument(
        "--no-log",
        action="store_true",
        help="print the report but do not append a log.md entry",
    )

    capture = sub.add_parser(
        "capture",
        help="backfill files/folders into the vault through the ingest pipeline",
    )
    capture.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[],
        help="one or more files or directories to capture; with NO path, drain the "
        "inbox holds (re-curate each inbox/hold-* from its stored body)",
    )
    capture.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be filed; write nothing, commit nothing, no LLM call",
    )
    capture.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most N walked items (a trial run)",
    )
    capture.add_argument(
        "--as-is",
        action="store_true",
        help="low-touch import: classify-for-routing but SKIP the curate pass; file "
        "the original body verbatim and index it (ADR 0010)",
    )
    capture.add_argument(
        "--budget",
        type=int,
        default=None,
        help="override THOTH_DAILY_LLM_BUDGET for THIS run only (transient); "
        "0 = unlimited for this import",
    )
    capture.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="commit+push every N ingested files plus a final flush (default 25)",
    )
    capture.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="only capture files whose vault-relative path matches (repeatable)",
    )
    capture.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip files whose path matches, in addition to the always-skipped "
        ".obsidian/.git/_bases/spine (repeatable)",
    )

    return parser
