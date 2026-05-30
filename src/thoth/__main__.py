"""Command-line entry point for ``thoth`` (``python -m thoth`` / the console script).

This is the single dispatch surface the deploy artifacts invoke (SPEC section 4 table,
section 13 Phase 3): the ``pkm-slack`` / ``thoth-slack`` systemd unit runs ``thoth
slack``; Claude Code's MCP config runs ``thoth mcp``; the 06:30 cron runs ``thoth
reindex`` (``--full-rebuild`` on recovery); and the 07:00 / Mon-07:00 cron runs ``thoth
summary daily`` / ``thoth summary weekly``. Each subcommand loads the configuration once
via :func:`thoth.config.load_config` and constructs the collaborator graph, then
delegates to the already-built Phase 0-3 entrypoint
(:func:`thoth.slack_app.run`, :func:`thoth.mcp_server.run`,
:meth:`thoth.reindex_from_vault.Reindexer.run`, :class:`thoth.summary.SummaryEngine`).

Import safety: only the standard library plus :mod:`thoth.config` is imported at module
top level. Every subcommand handler imports its heavy collaborators (and the lazily
imported optional clients behind them) **inside** the handler, so importing this module
-- and parsing ``--version`` / ``--help`` -- never needs ``anthropic`` / ``slack_bolt``
/ ``mcp`` to be installed. The handlers are split out as small, individually testable
functions so a test can substitute a fake for the entrypoint that would otherwise block
(the Slack/MCP daemons) or spawn a subprocess.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from typing import Any

from . import __version__
from .config import Config, load_config

__all__ = ["main", "build_parser"]


def build_parser() -> ArgumentParser:
    """Build the ``thoth`` argument parser with one subcommand per Phase-3 entrypoint.

    Subcommands: ``slack`` (the capture/retrieve daemon), ``mcp`` (the stdio MCP
    server), ``reindex`` (nightly incremental, ``--full-rebuild`` for recovery), and
    ``summary`` (``daily`` / ``weekly`` Slack digest). ``-v/--version`` prints the
    version and exits.

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
    sub = parser.add_subparsers(dest="command", metavar="{slack,mcp,reindex,summary}")

    sub.add_parser("slack", help="run the Slack Socket-Mode capture/retrieve daemon")
    sub.add_parser("mcp", help="serve the pkm_* tools over stdio MCP")

    reindex = sub.add_parser("reindex", help="reindex Hindsight from the vault")
    reindex.add_argument(
        "--full-rebuild",
        action="store_true",
        help="wipe the bank and re-retain every live page (recovery)",
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

    return parser


def main(args: Sequence[str] | None = None) -> None:
    """Parse ``args`` and dispatch to the matching subcommand handler.

    With no subcommand, prints help and returns (a bare ``thoth`` invocation is not an
    error). ``--version`` is handled by argparse before dispatch.

    Args:
        args: The argument vector (defaults to ``sys.argv[1:]``).
    """
    parser = build_parser()
    namespace = parser.parse_args(args)
    command = getattr(namespace, "command", None)
    if command is None:
        parser.print_help()
        return
    config = load_config()
    _dispatch(command, namespace, config)


def _dispatch(command: str, namespace: Namespace, config: Config) -> None:
    """Route a parsed ``command`` to its handler with the loaded ``config``."""
    handlers: dict[str, Callable[[Namespace, Config], None]] = {
        "slack": run_slack,
        "mcp": run_mcp,
        "reindex": run_reindex,
        "summary": run_summary,
    }
    handlers[command](namespace, config)


def run_slack(namespace: Namespace, config: Config) -> None:
    """Construct the ingest/query graph and start the Slack daemon (``thoth slack``).

    Builds the same collaborator graph as :func:`thoth.mcp_server.run` (so Slack
    free-text questions can blend the web via :class:`~thoth.research.ResearchEngine`)
    and hands it to :func:`thoth.slack_app.run`, which blocks serving Socket Mode. The
    heavy imports happen here, not at module load.
    """
    from . import slack_app

    graph = _build_graph(config)
    slack_app.run(
        config,
        graph.ingestor,
        graph.query_engine,
        research=graph.research,
    )


def run_mcp(namespace: Namespace, config: Config) -> None:
    """Build the MCP context and serve over stdio (``thoth mcp``).

    Delegates to :func:`thoth.mcp_server.run`, which wires its own collaborator graph
    from ``config`` and serves the ``pkm_*`` tools over stdio (blocking).
    """
    from . import mcp_server

    mcp_server.run(config)


def run_reindex(namespace: Namespace, config: Config) -> None:
    """Reindex Hindsight from the vault (``thoth reindex [--full-rebuild]``).

    Constructs a :class:`~thoth.reindex_from_vault.Reindexer` over a real
    :class:`~thoth.vault.Vault` and :class:`~thoth.hindsight.Hindsight` and runs one
    pass, forwarding ``--full-rebuild``. The resulting counts are printed for the cron
    log.
    """
    from .hindsight import Hindsight
    from .reindex_from_vault import Reindexer
    from .vault import Vault

    vault = Vault(config)
    hindsight = Hindsight(config)
    reindexer = Reindexer(config, vault, hindsight)
    result = reindexer.run(full_rebuild=bool(namespace.full_rebuild))
    print(
        f"reindex: changed={result.changed} skipped={result.skipped} "
        f"pruned={result.pruned} live={result.live_pages} "
        f"full_rebuild={result.full_rebuild}"
    )


def run_summary(
    namespace: Namespace,
    config: Config,
    *,
    poster_factory: Callable[[Config], Any] | None = None,
) -> None:
    """Compose and post the daily/weekly Slack digest (``thoth summary daily|weekly``).

    Builds a :class:`~thoth.summary.SummaryEngine` over a real vault, composes the
    requested digest, resolves the target channel from ``config`` (the
    ``SLACK_SUMMARY_CHANNEL`` var, never a hard-coded id), builds a real Slack
    ``WebClient`` from ``config.slack_bot_token``, and posts via
    :meth:`~thoth.summary.SummaryEngine.post`. ``poster_factory`` is injectable so a
    test can substitute a fake poster without the Slack SDK; in production it defaults
    to :func:`_make_web_client`.

    Args:
        namespace: The parsed args (``kind`` and ``--skip-when-empty``).
        config: The frozen runtime config.
        poster_factory: Builds a :class:`~thoth.summary.SlackPoster` from ``config``;
            defaults to a real Slack ``WebClient`` builder.
    """
    from .summary import SummaryEngine

    vault = _make_vault(config)
    engine = SummaryEngine(config, vault)
    digest = (
        engine.weekly_digest() if namespace.kind == "weekly" else engine.daily_digest()
    )
    channel = config.require_slack_summary_channel()
    factory = poster_factory if poster_factory is not None else _make_web_client
    poster = factory(config)
    posted = engine.post(
        poster,
        digest,
        channel=channel,
        skip_when_empty=bool(namespace.skip_when_empty),
    )
    print(
        f"summary {namespace.kind}: "
        f"{'posted' if posted else 'skipped (empty)'} to {channel}"
    )


# ---- collaborator construction (heavy imports kept inside) -------------------------


class _Graph:
    """The constructed ingest/query/research collaborator graph for the Slack daemon."""

    def __init__(
        self,
        ingestor: Any,
        query_engine: Any,
        research: Any,
    ) -> None:
        """Store the constructed collaborators."""
        self.ingestor = ingestor
        self.query_engine = query_engine
        self.research = research


def _build_graph(config: Config) -> _Graph:
    """Wire the full ingest/query/research collaborator graph from ``config``.

    Mirrors the graph :func:`thoth.mcp_server.run` builds (vault, llm, extractor,
    hindsight, git, ingestor, query engine, research engine) so the Slack daemon and the
    MCP server share one construction shape. All heavy imports are local to this
    function.
    """
    from .extract import Extractor
    from .git_sync import GitSync
    from .hindsight import Hindsight
    from .ingest import Ingestor
    from .llm import LLM
    from .query import QueryEngine
    from .research import ResearchEngine
    from .vault import Vault

    vault = Vault(config)
    llm = LLM(config)
    extractor = Extractor(config)
    hindsight = Hindsight(config)
    git = GitSync(config)
    ingestor = Ingestor(config, vault, llm, extractor, hindsight, git)
    query_engine = QueryEngine(config, vault, hindsight, llm)
    research = ResearchEngine(config, vault, query_engine, extractor, llm)
    return _Graph(ingestor=ingestor, query_engine=query_engine, research=research)


def _make_vault(config: Config) -> Any:
    """Build a real :class:`~thoth.vault.Vault` (import kept local)."""
    from .vault import Vault

    return Vault(config)


def _make_web_client(config: Config) -> Any:
    """Build a Slack ``WebClient`` from ``config.slack_bot_token`` (lazy import).

    ``slack_sdk`` ships with ``slack_bolt`` (a runtime-only optional dependency absent
    in CI), so it is imported here, never at module top level. The bot token is required
    for a summary post; :meth:`~thoth.config.Config.require_slack` raises a clear
    :class:`~thoth.config.ConfigError` if it is unset.
    """
    bot_token, _ = config.require_slack()
    from slack_sdk import WebClient

    return WebClient(token=bot_token)


if __name__ == "__main__":
    main()
