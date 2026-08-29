"""Command-line entry point for ``thoth``, and the single dispatch surface.

Every deploy artifact comes through here (SPEC section 4). The systemd unit runs ``thoth
slack``, Claude Code's MCP config runs ``thoth mcp``, and the crons run ``thoth
reindex``, ``thoth summary`` and ``thoth lint`` (SPEC section 11). ``thoth init`` seeds
a fresh vault with the packaged spine and dashboards, idempotently.

Each subcommand loads the configuration once, constructs the collaborator graph, and
delegates to the already-built entrypoint.

Import safety matters here. Only the standard library plus :mod:`thoth.config` and the
import-light :mod:`thoth.cli_parser` are imported at module level, and every handler
imports its heavy collaborators inside itself. So importing this module, and parsing
``--version`` or ``--help``, never needs ``anthropic``, ``slack_bolt`` or ``mcp``
installed.

The handlers are split into small testable functions, so a test can substitute a fake
for an entrypoint that would otherwise block or spawn a subprocess.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from . import __version__
from .cli_parser import build_parser
from .config import Config, load_config

__all__ = ["main", "build_parser"]

logger = logging.getLogger("thoth")


def main(args: Sequence[str] | None = None) -> None:
    """Parses ``args`` and dispatches to the matching subcommand handler.

    With no subcommand it prints help and returns, because a bare ``thoth`` invocation
    is not an error. ``--version`` is handled by argparse before dispatch.

    Args:
        args: The argument vector, defaulting to ``sys.argv[1:]``.
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
    """Configures root logging once at start, honouring ``THOTH_LOG_LEVEL``.

    The appliance was silent on the happy path (issue #52), because the per-operation
    success lines only surface once the root logger has a handler. Configuring it here
    means the daemons and the cron entrypoints print operator-readable progress. An
    unknown level name falls back to ``INFO`` rather than raising, so a typo never
    blocks boot.
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
    """Routes a parsed command to its handler."""
    handlers: dict[str, Callable[[Namespace, Config], None]] = {
        "init": run_init,
        "vault-bootstrap": run_vault_bootstrap,
        "slack": run_slack,
        "mcp": run_mcp,
        "reindex": run_reindex,
        "summary": run_summary,
        "lint": run_lint,
        "capture": run_capture,
    }
    handlers[command](namespace, config)


def run_init(namespace: Namespace, config: Config) -> None:
    """Seeds the vault spine and dashboards (``thoth init [--force]``).

    Writes the packaged spine and dashboards and creates the empty content folders.
    Idempotent, so existing spine files are left untouched unless ``--force`` is passed.

    Args:
        namespace: The parsed args, carrying ``--force``.
        config: The frozen runtime config, used to build the vault.
    """
    from .vault import Vault

    vault = Vault(config)
    result = vault.seed(force=bool(namespace.force))
    print(f"init: {len(result.created)} written, {len(result.skipped)} skipped")
    for name in result.created:
        print(f"  + {name}")


def run_vault_bootstrap(namespace: Namespace, config: Config) -> None:
    """Clones the vault into an empty ``$PKM_VAULT`` (``thoth vault-bootstrap``).

    Runs the shipped ``bin/vault-bootstrap`` wrapper, which clones
    ``THOTH_VAULT_REPO_URL`` into the mount point when it is not yet a git repo. It is a
    no-op once the vault has a ``.git``, and when the repo URL is unset, which is the
    dev and test default. Wired as a Helm initContainer before each vault-mounting
    workload, so a fresh cluster's empty PVC is populated once on first start.

    Args:
        namespace: The parsed args, which carry no flags here.
        config: The frozen runtime config, resolving the vault root and child env.
    """
    from .git_sync import GitSync

    git = GitSync(config)
    result = git.bootstrap()
    print(f"vault-bootstrap: {result.stdout.strip() or 'done'}")


def run_slack(namespace: Namespace, config: Config) -> None:
    """Constructs the ingest and query graph and starts the Slack daemon.

    Builds the same graph as :func:`thoth.mcp_server.run` and hands it to
    :func:`thoth.slack_app.run`, which blocks serving Socket Mode.
    """
    from . import slack_app

    graph = _build_graph(config)
    slack_app.run(
        config,
        graph.ingestor,
        graph.query_engine,
    )


def run_mcp(namespace: Namespace, config: Config) -> None:
    """Builds the MCP context and serves over the chosen transport (``thoth mcp``).

    Delegates to :func:`thoth.mcp_server.run`, which wires its own graph and serves the
    ``pkm_*`` tools, blocking. The default stdio transport is the spawn-as-child model
    Claude Code uses. The http transport serves bearer-authenticated streamable HTTP, on
    loopback by default, and fails fast when ``THOTH_MCP_API_KEYS`` is unset (issue
    #103).
    """
    from . import mcp_server

    mcp_server.run(
        config,
        transport=namespace.transport,
        host=namespace.host,
        port=namespace.port,
    )


def run_reindex(namespace: Namespace, config: Config) -> None:
    """Reindexes Hindsight from the vault (``thoth reindex``).

    Runs one pass over a real vault and Hindsight, forwarding ``--full-rebuild``. The
    budget guard takes the ``--budget`` transient override (issue #95). None uses
    ``THOTH_DAILY_LLM_BUDGET``, a positive value caps this run, and 0 disables the cap
    so a deliberate full rebuild can run to completion.

    A successful run records the reindex liveness marker for the daily heartbeat. A
    crash is reported to the errors-to-Slack target before being re-raised, so the cron
    log still shows the failure (issue #15).
    """
    from .alerts import _cron_alerting, make_alerter
    from .budget import make_budget_guard
    from .hindsight import Hindsight
    from .reindex_from_vault import Reindexer
    from .state import MarkerStore
    from .vault import Vault

    with _cron_alerting("cron: reindex", config):
        vault = Vault(config)
        # The daily cost guard (issue #16) caps the reindex retain burst, so an
        # accidental --full-rebuild of a large vault stops at the cap and defers the
        # rest rather than spending unbounded extraction. --budget is a transient
        # per-run override (issue #95), where 0 disables the cap entirely
        guard = make_budget_guard(
            config, alerter=make_alerter(config), limit=namespace.budget
        )
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
    """Composes and posts the daily or weekly Slack digest.

    Composes the requested digest over a real vault and resolves the target channel from
    config, never a hard-coded id.

    Args:
        namespace: The parsed args, carrying ``kind`` and ``--skip-when-empty``.
        config: The frozen runtime configuration.
        poster_factory: Builds the poster from config, defaulting to a real Slack
            client builder. Injectable so a test can post without the Slack SDK.
    """
    from .alerts import _cron_alerting, _make_web_client
    from .state import MarkerStore
    from .summary import SummaryEngine
    from .vault import Vault

    with _cron_alerting("cron: summary", config):
        vault = Vault(config)
        # The daily digest reads the liveness markers for its heartbeat (issue #15)
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
    """Scans the vault and prints the grouped lint report (``thoth lint``).

    Runs the check pass (SPEC section 11) and prints the findings grouped by severity.
    Unless ``--no-log`` is set, exactly one ``log.md`` entry is appended. A trailing
    issue count is printed for the cron log.

    Args:
        namespace: The parsed args, carrying ``--no-log``.
        config: The frozen runtime config, used to build the vault.
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


def run_capture(namespace: Namespace, config: Config) -> None:
    """Backfills files and folders into the vault (``thoth capture``).

    This is the CLI capture path (issue #80). A thin walker yields one capture per
    eligible file, honouring the include and exclude globs, the always-skipped machinery
    and spine, and the overall limit. Each capture goes through the existing ingest
    pipeline with commits deferred, and git is driven in batches:

    * The budget guard takes the ``--budget`` transient override, where ``None`` uses
      the configured cap, a positive value caps this run, and ``0`` disables it as the
      unlimited-import escape hatch. The same guard is injected into the ingest graph,
      so it covers analyse, classify, curate and the retain pass.
    * ``--dry-run`` lists what would be filed and writes and commits nothing, with no
      LLM call and no vault pull.
    * Otherwise the vault is pulled once up front, each capture is ingested with the
      commit deferred, and a commit runs every ``--batch-size`` files plus a final
      flush, rather than one commit per file. A conflict on a batch commit is surfaced
      loudly and stops the run, with the content already filed locally and never a force
      push.

    Per-file failures are isolated. A file whose ingest fails is logged, counted and
    skipped, and the run carries on, because pass 0b already made it durable in
    ``inbox/``. A batch-commit conflict still stops the run, since a diverged remote
    affects every file rather than one.

    Idempotency leans entirely on the existing SHA-256 machinery, so a second run over
    an unchanged tree re-derives the same digests, the raw layer skips, and no page is
    duplicated.

    Args:
        namespace: The parsed args, carrying ``paths`` and the capture flags.
        config: The frozen runtime configuration.
    """
    from .alerts import make_alerter
    from .budget import make_budget_guard
    from .capture_walk import walk_captures
    from .cli_capture import _CaptureCounts, _commit_capture_batch, _ingest_one
    from .git_sync import GitSync
    from .inbox_drain import drain_captures
    from .ingest import Capture
    from .vault import Vault

    # With no path argument, drain the inbox holds (issue #105). Each hold is re-filed
    # from its stored body through the same pipeline, honouring its stamped intent
    # (issue #95), then removed once filed. With paths, walk the tree instead (#80)
    drain_mode = not namespace.paths
    limit = namespace.limit
    vault = Vault(config)

    # The stream shared by the dry-run and real paths. A drain hold carries its own
    # path, so it can be removed once filed, plus its stamped intent so the sweep
    # re-files as originally requested. A walked file has no hold and uses the run-wide
    # flag, and an explicit --as-is forces low-touch for every item even on a drain
    def capture_stream() -> Iterator[tuple[str, Capture, str | None, bool]]:
        if drain_mode:
            for hold in drain_captures(vault):
                yield hold.rel, hold.capture, hold.rel, namespace.as_is or hold.as_is
        else:
            for capture in walk_captures(
                namespace.paths,
                include=namespace.include,
                exclude=namespace.exclude,
                limit=limit,
            ):
                yield capture.filename or "(capture)", capture, None, namespace.as_is

    if namespace.dry_run:
        planned = 0
        for target, capture, hold_rel, as_is in capture_stream():
            planned += 1
            if hold_rel is not None:
                mode = "as-is" if as_is else "curate"
                print(f"capture (dry-run): would re-file {target} ({mode})")
            else:
                kind = "text" if capture.text is not None else "file"
                print(f"capture (dry-run): would file {kind} {capture.filename}")
        print(f"capture: dry-run, {planned} item(s) would be filed (no writes)")
        return

    guard = make_budget_guard(
        config, alerter=make_alerter(config), limit=namespace.budget
    )
    graph = _build_graph(config, guard=guard)
    git = GitSync(config)
    # Pull once up front so every batched write lands on current state. The per-call
    # orient is skipped, so we do not pull per file
    git.pull()

    batch_size = max(1, namespace.batch_size)
    counts = _CaptureCounts()
    since_commit = 0
    total = 0

    for target, capture, hold_rel, as_is in capture_stream():
        if limit is not None and total >= limit:
            break
        total += 1
        _ingest_one(
            graph,
            vault,
            capture,
            target=target,
            hold_rel=hold_rel,
            as_is=as_is,
            index=total,
            counts=counts,
        )
        since_commit += 1
        if since_commit >= batch_size:
            _commit_capture_batch(git, since_commit)
            since_commit = 0
    if since_commit:
        _commit_capture_batch(git, since_commit)
    print(
        f"capture: {total} item(s) processed -- filed={counts.filed} "
        f"unchanged={counts.unchanged} skipped={counts.skipped} "
        f"deferred={counts.deferred} failed={counts.failed}"
    )
    if counts.failed:
        print(
            f"capture: {counts.failed} file(s) failed to curate and are held in inbox/ "
            "(durable) -- re-run to retry them."
        )


# ---- collaborator construction (heavy imports kept inside) -------------------------


@dataclass
class _Graph:
    """The constructed ingest and query collaborator graph for the Slack daemon."""

    ingestor: Any
    query_engine: Any


def _build_graph(config: Config, *, guard: Any | None = None) -> _Graph:
    """Wires the full ingest and query collaborator graph from ``config``.

    Construction is delegated to :func:`thoth.wiring.build_collaborators`, the shape
    shared with :func:`thoth.mcp_server.run`, adding the alerting budget guard and the
    liveness markers.
    """
    from .alerts import make_alerter
    from .budget import make_budget_guard
    from .state import MarkerStore
    from .wiring import build_collaborators

    # The daily cost guard (issue #16). One shared cap over the Anthropic calls and the
    # fact-extraction behind Hindsight retain, persisted in state.db and keyed by the
    # London day. It alerts once a day, a non-positive budget disables it, and a caller
    # may inject a guard carrying a transient --budget override (issue #80)
    if guard is None:
        guard = make_budget_guard(config, alerter=make_alerter(config))
    # Liveness markers so a successful capture or push records its time for the daily
    # heartbeat (issue #15). The same disposable state.db backs the dedupe table
    built = build_collaborators(
        config, guard=guard, markers=MarkerStore(config.state_db_path)
    )
    return _Graph(ingestor=built.ingestor, query_engine=built.query_engine)


if __name__ == "__main__":
    main()
