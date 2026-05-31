"""Command-line entry point for ``thoth`` (``python -m thoth`` / the console script).

This is the single dispatch surface the deploy artifacts invoke (SPEC section 4 table,
section 13 Phase 3-4): the ``pkm-slack`` / ``thoth-slack`` systemd unit runs ``thoth
slack``; Claude Code's MCP config runs ``thoth mcp``; the 06:30 cron runs ``thoth
reindex`` (``--full-rebuild`` on recovery); the 07:00 / Mon-07:00 cron runs ``thoth
summary daily`` / ``thoth summary weekly``; and the Mon-08:00 cron runs ``thoth lint``
(SPEC section 11). ``thoth init`` seeds a fresh or wiped vault with the packaged spine
(``index.md`` / ``SCHEMA.md`` / ``log.md``) and Bases dashboards (idempotent). Each
subcommand loads the configuration once via
:func:`thoth.config.load_config` and constructs the collaborator graph, then delegates
to the already-built Phase 0-4 entrypoint (:func:`thoth.slack_app.run`,
:func:`thoth.mcp_server.run`, :meth:`thoth.reindex_from_vault.Reindexer.run`,
:class:`thoth.summary.SummaryEngine`, :class:`thoth.lint.LintEngine`).

Import safety: only the standard library plus :mod:`thoth.config` is imported at module
top level. Every subcommand handler imports its heavy collaborators (and the lazily
imported optional clients behind them) **inside** the handler, so importing this module
-- and parsing ``--version`` / ``--help`` -- never needs ``anthropic`` / ``slack_bolt``
/ ``mcp`` to be installed. The handlers are split out as small, individually testable
functions so a test can substitute a fake for the entrypoint that would otherwise block
(the Slack/MCP daemons) or spawn a subprocess.
"""

from __future__ import annotations

import logging
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from . import __version__
from .config import Config, load_config

__all__ = ["main", "build_parser"]

logger = logging.getLogger("thoth")


def build_parser() -> ArgumentParser:
    """Build the ``thoth`` argument parser with one subcommand per Phase-3 entrypoint.

    Subcommands: ``init`` (seed the vault spine + dashboards, idempotent,
    ``--force`` to overwrite), ``slack`` (the capture/retrieve daemon), ``mcp`` (the
    stdio MCP server), ``reindex`` (nightly incremental, ``--full-rebuild`` for
    recovery), ``summary`` (``daily`` / ``weekly`` Slack digest), and ``lint`` (the
    13-check vault maintenance scan, ``--no-log`` to suppress the log entry).
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
        dest="command", metavar="{init,slack,mcp,reindex,summary,lint}"
    )

    init = sub.add_parser("init", help="seed the vault spine + dashboards (idempotent)")
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing spine/dashboard files",
    )

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

    lint = sub.add_parser("lint", help="scan the vault for the 13 maintenance issues")
    lint.add_argument(
        "--no-log",
        action="store_true",
        help="print the report but do not append a log.md entry",
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
    _configure_logging(config)
    _dispatch(command, namespace, config)


def _configure_logging(config: Config) -> None:
    """Configure root logging once at daemon start, honouring ``THOTH_LOG_LEVEL``.

    The appliance was silent on the happy path (issue #52): the per-operation success
    lines emitted by ingest/query/research/intent only surface once the root logger has
    a handler. This calls :func:`logging.basicConfig` with the configured level (default
    ``INFO``) so a long-running daemon (``thoth slack``/``mcp``) and the cron entrypoints
    print concise operator-readable progress. An unknown level name falls back to
    ``INFO`` rather than raising, so a typo in ``THOTH_LOG_LEVEL`` never blocks boot.
    """
    level = logging.getLevelName(config.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("thoth %s starting (log level %s)", __version__, config.log_level)


def _dispatch(command: str, namespace: Namespace, config: Config) -> None:
    """Route a parsed ``command`` to its handler with the loaded ``config``."""
    handlers: dict[str, Callable[[Namespace, Config], None]] = {
        "init": run_init,
        "slack": run_slack,
        "mcp": run_mcp,
        "reindex": run_reindex,
        "summary": run_summary,
        "lint": run_lint,
    }
    handlers[command](namespace, config)


def run_init(namespace: Namespace, config: Config) -> None:
    """Seed the vault spine + dashboards (``thoth init [--force]``).

    Builds a real :class:`~thoth.vault.Vault` and calls
    :meth:`~thoth.vault.Vault.seed`, which writes the packaged spine (``index.md`` /
    ``SCHEMA.md`` / ``log.md``) and Bases dashboards and creates the empty content
    folders. Idempotent: existing spine files are left untouched unless ``--force`` is
    passed. A one-line created/skipped summary is printed. The heavy import is local to
    the handler so importing this module never needs the vault surface.

    Args:
        namespace: The parsed args (carries ``--force``).
        config: The frozen runtime config (used to build the vault).
    """
    from .vault import Vault

    vault = Vault(config)
    result = vault.seed(force=bool(namespace.force))
    print(f"init: {len(result.created)} written, {len(result.skipped)} skipped")
    for name in result.created:
        print(f"  + {name}")


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
    pass, forwarding ``--full-rebuild``. A successful run records the ``reindex``
    liveness marker for the daily heartbeat, and a crash is reported to the
    errors-to-Slack target before being re-raised so the cron log still shows the
    failure (issue #15). The resulting counts are printed for the cron log.
    """
    from .alerts import make_alerter
    from .budget import make_budget_guard
    from .hindsight import Hindsight
    from .reindex_from_vault import Reindexer
    from .state import MarkerStore
    from .vault import Vault

    with _cron_alerting("cron: reindex", config):
        vault = Vault(config)
        # The daily cost guard (issue #16) caps the reindex retain burst; an
        # accidental --full-rebuild of a large vault stops at the cap (deferring the
        # rest to the next day) instead of spending unbounded Gemini extraction. It
        # alerts once per day.
        guard = make_budget_guard(config, alerter=make_alerter(config))
        hindsight = Hindsight(config, guard=guard)
        reindexer = Reindexer(
            config, vault, hindsight, markers=MarkerStore(config.state_db_path)
        )
        result = reindexer.run(full_rebuild=bool(namespace.full_rebuild))
        print(
            f"reindex: changed={result.changed} skipped={result.skipped} "
            f"pruned={result.pruned} live={result.live_pages} "
            f"full_rebuild={result.full_rebuild} aborted={result.aborted}"
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
    from .state import MarkerStore
    from .summary import SummaryEngine

    with _cron_alerting("cron: summary", config):
        vault = _make_vault(config)
        # The daily digest reads the liveness markers for its heartbeat (issue #15).
        engine = SummaryEngine(config, vault, markers=MarkerStore(config.state_db_path))
        digest = (
            engine.weekly_digest()
            if namespace.kind == "weekly"
            else engine.daily_digest()
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


def run_lint(namespace: Namespace, config: Config) -> None:
    """Scan the vault and print the grouped lint report (``thoth lint [--no-log]``).

    Builds a real :class:`~thoth.vault.Vault` and a
    :class:`~thoth.lint.LintEngine`, runs the 13-check pass (SPEC section 11), prints
    :meth:`~thoth.lint.LintReport.render` (the findings grouped by severity), and --
    unless ``--no-log`` is set -- appends exactly one ``log.md`` entry via
    :meth:`~thoth.lint.LintEngine.record` (check 13, "report + log"). A trailing
    ``lint: N issue(s) found`` line is printed for the Mon-08:00 cron log. All heavy
    imports are local to the handler so importing this module never needs the linter.

    Args:
        namespace: The parsed args (carries ``--no-log``).
        config: The frozen runtime config (used to build the vault).
    """
    from .lint import LintEngine
    from .vault import Vault

    vault = Vault(config)
    engine = LintEngine(config, vault)
    report = engine.run()
    print(report.render())
    if not namespace.no_log:
        engine.record(report)
    print(f"lint: {report.total} issue(s) found")


# ---- unattended observability (issue #15) ------------------------------------------


@contextmanager
def _cron_alerting(where: str, config: Config) -> Iterator[None]:
    """Report a cron-entrypoint crash to the errors-to-Slack target, then re-raise.

    A one-shot cron job that dies only writes to its ``/var/log`` file, which nobody
    watches on an isolated VPS (issue #15). This wraps the job body so an unhandled
    exception is posted to the alert target (:class:`thoth.alerts.Alerter`, best-effort)
    before being re-raised -- so the cron log still records the non-zero exit, and a
    human gets a Slack message. Building the alerter is itself guarded: a failure to
    even construct it must not mask the original error.

    Args:
        where: A short label for the failing entrypoint (e.g. ``"cron: reindex"``).
        config: The frozen runtime config (resolves the alert target + bot token).

    Yields:
        ``None``; the caller runs its job body inside the ``with`` block.
    """
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - report ANY crash, then re-raise
        try:
            from .alerts import make_alerter

            make_alerter(config).alert_exception(where, exc)
        except Exception:  # noqa: BLE001 - alerting must never mask the real error
            pass
        raise


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
    from .alerts import make_alerter
    from .budget import make_budget_guard
    from .extract import Extractor
    from .git_sync import GitSync
    from .hindsight import Hindsight
    from .ingest import Ingestor
    from .llm import LLM
    from .query import QueryEngine
    from .research import ResearchEngine
    from .state import MarkerStore
    from .vault import Vault

    vault = Vault(config)
    # The daily cost guard (issue #16): one shared cap over the Anthropic calls (via the
    # LLM) and the Gemini fact-extraction (via Hindsight retain), persisted in state.db
    # and keyed by the London day. It alerts once per day through the errors-to-Slack
    # target. A non-positive THOTH_DAILY_LLM_BUDGET disables it.
    guard = make_budget_guard(config, alerter=make_alerter(config))
    llm = LLM(config, guard=guard)
    extractor = Extractor(config)
    hindsight = Hindsight(config, guard=guard)
    git = GitSync(config)
    # Liveness markers so a successful capture/push records its time for the daily
    # heartbeat (issue #15); the same disposable state.db backs the dedupe table.
    markers = MarkerStore(config.state_db_path)
    # Pass SCHEMA.md as the curate-call system_extra so curated pages are filed to the
    # live per-type schema; without it the curate model files blind (this wiring used to
    # drop schema_md, leaving the vault empty when paired with a schema-less prompt).
    ingestor = Ingestor(
        config,
        vault,
        llm,
        extractor,
        hindsight,
        git,
        schema_md=vault.schema_md(),
        markers=markers,
    )
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
