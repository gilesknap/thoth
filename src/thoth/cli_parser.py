"""The ``thoth`` argument parser, split out of :mod:`thoth.__main__`.

This module imports only the standard library plus the package version. Building the
parser, and therefore ``--version`` and ``--help``, never needs the heavy optional
clients ``anthropic``, ``slack_bolt`` or ``mcp`` to be installed.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from . import __version__

__all__ = ["build_parser"]


def build_parser() -> ArgumentParser:
    """Build the ``thoth`` argument parser with one subcommand per Phase-3 entrypoint.

    The subcommands are:

    * ``init`` seeds the vault spine and the dashboards. It is idempotent, and
      ``--force`` overwrites an existing file.
    * ``vault-bootstrap`` clones the vault repo into an empty ``$PKM_VAULT``.
    * ``slack`` runs the capture and retrieve daemon.
    * ``mcp`` runs the MCP server. ``--transport stdio`` is the default, and
      ``--transport http`` serves the bearer-authenticated network surface on
      ``--host`` and ``--port`` (issue #103).
    * ``reindex`` runs the nightly incremental reindex. ``--full-rebuild`` recovers the
      bank, and ``--budget`` overrides the cap for one run (issue #95).
    * ``summary`` composes and posts the ``daily`` or ``weekly`` Slack digest.
    * ``lint`` runs the 13-check vault maintenance scan. ``--no-log`` suppresses the log
      entry.
    * ``capture`` backfills files and folders through the ingest pipeline. ``--as-is``
      asks for a low-touch import, and ``--budget`` overrides the cap for one run. It
      also takes ``--dry-run``, ``--limit``, ``--batch-size``, ``--include`` and
      ``--exclude`` (issue #80).

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
    # These defaults mirror thoth.mcp_server.DEFAULT_MCP_HOST and DEFAULT_MCP_PORT. They
    # stay literals here so that parsing --help never imports the heavy, mcp-dependent
    # server module. The bind address is loopback by design. cloudflared and Cloudflare
    # Access provide the network exposure (ADR 0011), never a raw 0.0.0.0 socket.
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
