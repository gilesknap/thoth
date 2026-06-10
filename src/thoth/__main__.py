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

Import safety: only the standard library plus :mod:`thoth.config` and the import-light
:mod:`thoth.cli_parser` is imported at module top level. Every subcommand handler
imports its heavy collaborators (and the lazily
imported optional clients behind them) **inside** the handler, so importing this module
-- and parsing ``--version`` / ``--help`` -- never needs ``anthropic`` / ``slack_bolt``
/ ``mcp`` to be installed. The handlers are split out as small, individually testable
functions so a test can substitute a fake for the entrypoint that would otherwise block
(the Slack/MCP daemons) or spawn a subprocess.
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
    lines emitted by ingest/query/intent only surface once the root logger has
    a handler. This calls :func:`logging.basicConfig` with the configured level
    (default ``INFO``) so a long-running daemon (``thoth slack``/``mcp``) and the cron
    entrypoints print concise operator-readable progress. An unknown level name falls
    back to ``INFO`` rather than raising, so a typo never blocks boot.
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


def run_vault_bootstrap(namespace: Namespace, config: Config) -> None:
    """Clone the vault into an empty ``$PKM_VAULT`` (``thoth vault-bootstrap``).

    Builds a :class:`~thoth.git_sync.GitSync` over ``config`` and calls
    :meth:`~thoth.git_sync.GitSync.bootstrap`, which runs the shipped
    ``bin/vault-bootstrap`` wrapper: it clones the ``THOTH_VAULT_REPO_URL`` repo into
    the vault mount point when it is not yet a git repo, and is a no-op when the vault
    already has a ``.git`` (the steady state) or when ``THOTH_VAULT_REPO_URL`` is unset
    (the dev/test default). Wired as a Helm initContainer before each vault-mounting
    workload so a fresh cluster's empty vault PVC is populated once on first start. The
    git import is local to the handler so importing this module never needs it.

    Args:
        namespace: The parsed args (no flags for this subcommand).
        config: The frozen runtime config (resolves the vault root + child env).
    """
    from .git_sync import GitSync

    git = GitSync(config)
    result = git.bootstrap()
    print(f"vault-bootstrap: {result.stdout.strip() or 'done'}")


def run_slack(namespace: Namespace, config: Config) -> None:
    """Construct the ingest/query graph and start the Slack daemon (``thoth slack``).

    Builds the same collaborator graph as :func:`thoth.mcp_server.run` and hands it to
    :func:`thoth.slack_app.run`, which blocks serving Socket Mode. The heavy imports
    happen here, not at module load.
    """
    from . import slack_app

    graph = _build_graph(config)
    slack_app.run(
        config,
        graph.ingestor,
        graph.query_engine,
    )


def run_mcp(namespace: Namespace, config: Config) -> None:
    """Build the MCP context and serve over the chosen transport (``thoth mcp``).

    Delegates to :func:`thoth.mcp_server.run`, which wires its own collaborator graph
    from ``config`` and serves the ``pkm_*`` tools (blocking). ``--transport stdio``
    (the default) is the byte-for-byte-unchanged spawn-as-child model Claude Code uses;
    ``--transport http`` serves bearer-authenticated streamable-HTTP on
    ``--host``:``--port`` (loopback by default), failing fast if ``THOTH_MCP_API_KEYS``
    is unset (issue #103).
    """
    from . import mcp_server

    mcp_server.run(
        config,
        transport=namespace.transport,
        host=namespace.host,
        port=namespace.port,
    )


def run_reindex(namespace: Namespace, config: Config) -> None:
    """Reindex Hindsight from the vault (``reindex [--full-rebuild] [--budget N]``).

    Constructs a :class:`~thoth.reindex_from_vault.Reindexer` over a real
    :class:`~thoth.vault.Vault` and :class:`~thoth.hindsight.Hindsight` and runs one
    pass, forwarding ``--full-rebuild``. The budget guard is built with the
    ``--budget`` transient override (issue #95): ``None`` uses
    ``THOTH_DAILY_LLM_BUDGET``, a positive value caps THIS run, and ``0`` disables the
    cap so a deliberate full rebuild can run to completion. A successful run records
    the ``reindex``
    liveness marker for the daily heartbeat, and a crash is reported to the
    errors-to-Slack target before being re-raised so the cron log still shows the
    failure (issue #15). The resulting counts are printed for the cron log.
    """
    from .alerts import _cron_alerting, make_alerter
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
        # alerts once per day. ``--budget N`` is a transient per-run override (issue
        # #95): None uses THOTH_DAILY_LLM_BUDGET, a positive value caps THIS run, and 0
        # disables the cap so a deliberate full rebuild can run to completion.
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
    """Compose and post the daily/weekly Slack digest (``thoth summary daily|weekly``).

    Builds a :class:`~thoth.summary.SummaryEngine` over a real vault, composes the
    requested digest, resolves the target channel from ``config`` (the
    ``SLACK_SUMMARY_CHANNEL`` var, never a hard-coded id), builds a real Slack
    ``WebClient`` from ``config.slack_bot_token``, and posts via
    :meth:`~thoth.summary.SummaryEngine.post`. ``poster_factory`` is injectable so a
    test can substitute a fake poster without the Slack SDK; in production it defaults
    to :func:`thoth.alerts._make_web_client`.

    Args:
        namespace: The parsed args (``kind`` and ``--skip-when-empty``).
        config: The frozen runtime config.
        poster_factory: Builds a :class:`~thoth.summary.SlackPoster` from ``config``;
            defaults to a real Slack ``WebClient`` builder.
    """
    from .alerts import _cron_alerting, _make_web_client
    from .state import MarkerStore
    from .summary import SummaryEngine
    from .vault import Vault

    with _cron_alerting("cron: summary", config):
        vault = Vault(config)
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


def run_capture(namespace: Namespace, config: Config) -> None:
    """Backfill files/folders into the vault (``thoth capture <path>... [flags]``).

    The CLI capture path (issue #80): a thin walker
    (:func:`thoth.capture_walk.walk_captures`) yields one
    :class:`~thoth.ingest.Capture` per eligible file under each ``paths`` entry
    (Markdown/text -> a ``text`` capture; image/PDF/audio -> a ``path`` capture; every
    one ``source="import"``), honouring the ``--include``/``--exclude`` globs, the
    always-skipped machinery/spine, and the overall ``--limit``. Each capture is fed
    through the EXISTING :meth:`thoth.ingest.Ingestor.ingest` pipeline with commits
    deferred, and git is driven in batches:

    * The budget guard is built with the ``--budget`` transient override (issue #80):
      ``None`` uses ``THOTH_DAILY_LLM_BUDGET``; a positive value caps THIS run; ``0``
      disables the cap (the unlimited-import escape hatch). The same guard is injected
      into the ingest graph so it covers analyse/classify/curate and the retain pass.
    * ``--dry-run`` lists what would be filed and writes/commits NOTHING (no LLM call,
      no vault pull): the walker is iterated and each planned filing is printed.
    * Otherwise the vault is pulled ONCE up front, each capture is ingested with
      ``commit=False`` (and ``as_is=--as-is``), and ``GitSync.commit`` is called every
      ``--batch-size`` ingested files plus a final flush -- not one commit per file. A
      :class:`~thoth.git_sync.VaultConflictError` from a batch commit is surfaced loudly
      and stops the run (content is filed locally; never ``--force``).

    Per-file failures are isolated: a file whose ingest raises
    :class:`~thoth.ingest.IngestError` (an unparseable/invalid model file-plan, a
    rejected vault write) is logged, counted (``failed``), and skipped -- the run
    carries on. The failing item is already durable in ``inbox/`` (pass 0b runs before
    the failing classify/curate), so it is recoverable on a later run rather than
    aborting a large import on one bad file. (A batch-commit
    :class:`~thoth.git_sync.VaultConflictError` still stops the run -- a diverged remote
    affects every file, not one.)

    Idempotency leans entirely on the existing ``raw/``/``inbox/`` SHA-256 machinery: a
    second run over an unchanged tree re-derives the same slugs/digests and the raw
    layer skips, so no page is duplicated.

    Args:
        namespace: The parsed args (``paths`` and the capture flags).
        config: The frozen runtime config.
    """
    from .alerts import make_alerter
    from .budget import make_budget_guard
    from .capture_walk import walk_captures
    from .cli_capture import _CaptureCounts, _commit_capture_batch, _ingest_one
    from .git_sync import GitSync
    from .inbox_drain import drain_captures
    from .ingest import Capture
    from .vault import Vault

    # With NO path argument, drain the inbox holds (issue #105): re-file each
    # inbox/hold-* from its stored body through the SAME ingest pipeline -- honouring
    # the hold's stamped intent (curate vs --as-is, issue #95 task E) -- then remove the
    # superseded hold once it is filed. With paths, walk the file/folder tree (#80).
    drain_mode = not namespace.paths
    limit = namespace.limit
    vault = Vault(config)

    # Build the (target, capture, hold_rel, as_is) stream shared by the dry-run and
    # real paths: a drain hold carries its own path so a filed hold can be removed AND
    # its stamped intent (issue #95, task E) so the sweep re-files curate-vs-as-is as
    # originally requested; a walked file has no hold and uses the run-wide --as-is
    # flag. The explicit --as-is flag forces low-touch for every item even on a drain.
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
    # Pull ONCE up front so every batched write lands on current state; the per-call
    # orient is skipped (commit=False) so we do not pull per file.
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
    """The constructed ingest/query collaborator graph for the Slack daemon."""

    ingestor: Any
    query_engine: Any


def _build_graph(config: Config, *, guard: Any | None = None) -> _Graph:
    """Wire the full ingest/query collaborator graph from ``config``.

    Delegates the construction to :func:`thoth.wiring.build_collaborators` (the shape
    shared with :func:`thoth.mcp_server.run`), adding the Slack/CLI-side pieces: the
    alerting budget guard and the liveness markers. All heavy imports are local to
    this function.

    ``guard`` lets a caller inject an already-built :class:`~thoth.budget.BudgetGuard`
    so the same cap reaches both the LLM (classify/analyse/curate) and Hindsight
    (retain). The ``thoth capture`` handler passes one built with its ``--budget``
    transient override (issue #80); ``None`` (the default) builds the standard
    config-driven guard, so the Slack/MCP callers are unaffected.
    """
    from .alerts import make_alerter
    from .budget import make_budget_guard
    from .state import MarkerStore
    from .wiring import build_collaborators

    # The daily cost guard (issue #16): one shared cap over the Anthropic calls (via the
    # LLM) and the Gemini fact-extraction (via Hindsight retain), persisted in state.db
    # and keyed by the London day. It alerts once per day through the errors-to-Slack
    # target. A non-positive THOTH_DAILY_LLM_BUDGET disables it. A caller may inject a
    # guard carrying a transient --budget override (thoth capture, issue #80).
    if guard is None:
        guard = make_budget_guard(config, alerter=make_alerter(config))
    # Liveness markers so a successful capture/push records its time for the daily
    # heartbeat (issue #15); the same disposable state.db backs the dedupe table.
    built = build_collaborators(
        config, guard=guard, markers=MarkerStore(config.state_db_path)
    )
    return _Graph(ingestor=built.ingestor, query_engine=built.query_engine)


if __name__ == "__main__":
    main()
