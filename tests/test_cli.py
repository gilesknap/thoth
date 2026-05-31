"""Tests for the ``thoth`` command-line dispatch (:mod:`thoth.__main__`).

The Phase-3/4 entrypoints are reachable as subcommands -- ``slack``, ``mcp``,
``reindex``, ``summary`` and ``lint`` -- each loading the config once and constructing
the collaborator graph before delegating. These tests exercise the parser and the
dispatch wiring against fakes: the blocking daemons (``slack`` / ``mcp``) are checked
only at the routing level (their ``run`` functions are monkeypatched so nothing blocks
or imports the optional clients), ``reindex`` is driven against a fake
:class:`Reindexer`, ``summary`` is driven end-to-end over a real seeded vault with a
fake Slack poster (so the 07:00 / Mon-07:00 digest path is proven to compose and post
without the Slack SDK), and ``lint`` is driven end-to-end over a real seeded tmp vault
(a deliberately-broken page) so the Mon-08:00 maintenance scan is proven to print the
grouped report and append exactly one ``log.md`` entry.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from thoth import __main__, __version__
from thoth.config import Config, ConfigError, load_config


def test_cli_version() -> None:
    """``python -m thoth --version`` prints the package version (subprocess)."""
    cmd = [sys.executable, "-m", "thoth", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__


# --- parser ------------------------------------------------------------------------


def test_build_parser_reindex_full_rebuild_flag() -> None:
    """``reindex --full-rebuild`` parses to the flag; bare ``reindex`` leaves it off."""
    parser = __main__.build_parser()
    assert parser.parse_args(["reindex", "--full-rebuild"]).full_rebuild is True
    assert parser.parse_args(["reindex"]).full_rebuild is False


def test_build_parser_summary_kind_choices() -> None:
    """``summary`` requires daily|weekly and rejects anything else."""
    parser = __main__.build_parser()
    assert parser.parse_args(["summary", "daily"]).kind == "daily"
    assert parser.parse_args(["summary", "weekly"]).kind == "weekly"
    with pytest.raises(SystemExit):
        parser.parse_args(["summary", "monthly"])


def test_build_parser_lint_no_log_flag() -> None:
    """``lint --no-log`` parses to the flag; bare ``lint`` defaults it off."""
    parser = __main__.build_parser()
    assert parser.parse_args(["lint", "--no-log"]).no_log is True
    assert parser.parse_args(["lint"]).no_log is False


@pytest.mark.parametrize(
    "command", ["init", "slack", "mcp", "reindex", "summary", "lint"]
)
def test_build_parser_recognises_each_subcommand(command: str) -> None:
    """Each Phase-3/4 subcommand is recognised and sets ``command``."""
    parser = __main__.build_parser()
    args = ["summary", "daily"] if command == "summary" else [command]
    assert parser.parse_args(args).command == command


# --- main dispatch (handlers monkeypatched; config load stubbed) -------------------


@pytest.fixture
def stub_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    """Stub ``load_config`` so ``main`` does not read the real environment."""
    config = load_config({"PKM_VAULT": "/x"})
    monkeypatch.setattr(__main__, "load_config", lambda: config)
    return config


def test_main_no_command_prints_help_and_does_not_load_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare ``thoth`` prints help and never constructs anything (not an error)."""

    def _boom() -> Config:
        raise AssertionError("load_config must not be called without a subcommand")

    monkeypatch.setattr(__main__, "load_config", _boom)
    __main__.main([])
    out = capsys.readouterr().out
    assert "usage" in out.lower()


def test_main_dispatches_each_command(
    monkeypatch: pytest.MonkeyPatch, stub_config: Config
) -> None:
    """Each subcommand routes to its handler with the loaded config."""
    calls: list[tuple[str, Config]] = []

    for name in (
        "run_init",
        "run_slack",
        "run_mcp",
        "run_reindex",
        "run_summary",
        "run_lint",
    ):

        def _record(ns: Any, cfg: Config, _name: str = name) -> None:
            calls.append((_name, cfg))

        monkeypatch.setattr(__main__, name, _record)

    __main__.main(["init"])
    __main__.main(["slack"])
    __main__.main(["mcp"])
    __main__.main(["reindex"])
    __main__.main(["summary", "daily"])
    __main__.main(["lint"])

    assert [name for name, _ in calls] == [
        "run_init",
        "run_slack",
        "run_mcp",
        "run_reindex",
        "run_summary",
        "run_lint",
    ]
    assert all(cfg is stub_config for _, cfg in calls)


# --- run_init (builds a Vault and seeds it) ----------------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_run_init_builds_vault_and_seeds(
    monkeypatch: pytest.MonkeyPatch, stub_config: Config, force: bool
) -> None:
    """``thoth init [--force]`` builds a Vault and calls seed with the right force."""
    seen: dict[str, Any] = {}

    class _FakeVault:
        def __init__(self, config: Config) -> None:
            seen["config"] = config

        def seed(self, *, force: bool) -> Any:
            seen["force"] = force
            from thoth.vault import SeedResult

            return SeedResult(created=("index.md",), skipped=())

    import thoth.vault as vault_module

    monkeypatch.setattr(vault_module, "Vault", _FakeVault)

    args = ["init", "--force"] if force else ["init"]
    namespace = __main__.build_parser().parse_args(args)
    __main__.run_init(namespace, stub_config)

    assert seen["config"] is stub_config
    assert seen["force"] is force


# --- run_slack / run_mcp (routing only; nothing blocks) ----------------------------


def test_run_slack_builds_graph_and_calls_slack_run(
    monkeypatch: pytest.MonkeyPatch, stub_config: Config
) -> None:
    """``thoth slack`` builds the graph and hands research to slack_app.run."""
    captured: dict[str, Any] = {}

    sentinel = object()
    monkeypatch.setattr(
        __main__,
        "_build_graph",
        lambda cfg: __main__._Graph(
            ingestor="ING", query_engine="QRY", research=sentinel
        ),
    )

    import thoth.slack_app as slack_app

    def _fake_run(cfg: Config, ingestor: Any, query_engine: Any, **kw: Any) -> None:
        captured["args"] = (cfg, ingestor, query_engine, kw)

    monkeypatch.setattr(slack_app, "run", _fake_run)

    namespace = __main__.build_parser().parse_args(["slack"])
    __main__.run_slack(namespace, stub_config)

    cfg, ingestor, query_engine, kw = captured["args"]
    assert ingestor == "ING"
    assert query_engine == "QRY"
    assert kw["research"] is sentinel


def test_build_graph_wires_schema_md_into_ingestor(tmp_path: Path) -> None:
    """``_build_graph`` must hand the vault's SCHEMA.md to the ingestor's curate call.

    The wiring used to drop ``schema_md`` (it stayed ``None``), so the curate model was
    never shown the schema; this asserts the regression cannot return. Collaborators
    construct lazily, so building the real graph needs no API keys or network.
    """
    vault_root = tmp_path / "pkm-vault"
    vault_root.mkdir()
    (vault_root / "SCHEMA.md").write_text(
        "# Vault Schema\nlive rules\n", encoding="utf-8"
    )
    config = load_config({"PKM_VAULT": str(vault_root)})

    graph = __main__._build_graph(config)

    assert graph.ingestor._schema_md == "# Vault Schema\nlive rules\n"


def test_run_mcp_calls_mcp_server_run(
    monkeypatch: pytest.MonkeyPatch, stub_config: Config
) -> None:
    """``thoth mcp`` delegates to mcp_server.run(config)."""
    seen: list[Config] = []
    import thoth.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "run", lambda cfg: seen.append(cfg))

    namespace = __main__.build_parser().parse_args(["mcp"])
    __main__.run_mcp(namespace, stub_config)
    assert seen == [stub_config]


# --- run_reindex (fake Reindexer) --------------------------------------------------


class _FakeReindexer:
    """Records construction + run(full_rebuild=...) and returns a canned result."""

    instances: list[_FakeReindexer] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.runs: list[bool] = []
        _FakeReindexer.instances.append(self)

    def run(self, *, full_rebuild: bool = False) -> Any:
        """Record the flag and return a result-shaped object."""
        self.runs.append(full_rebuild)
        from thoth.reindex_from_vault import ReindexResult

        return ReindexResult(
            changed=2,
            skipped=1,
            pruned=0,
            live_pages=3,
            full_rebuild=full_rebuild,
        )


@pytest.mark.parametrize("full", [False, True])
def test_run_reindex_runs_with_flag(
    monkeypatch: pytest.MonkeyPatch, full: bool
) -> None:
    """``thoth reindex [--full-rebuild]`` constructs a Reindexer and runs it."""
    _FakeReindexer.instances.clear()
    import thoth.reindex_from_vault as reindex_mod

    monkeypatch.setattr(reindex_mod, "Reindexer", _FakeReindexer)
    config = load_config({"PKM_VAULT": "/x"})

    args = ["reindex", "--full-rebuild"] if full else ["reindex"]
    namespace = __main__.build_parser().parse_args(args)
    __main__.run_reindex(namespace, config)

    assert len(_FakeReindexer.instances) == 1
    assert _FakeReindexer.instances[0].runs == [full]


class _CrashingReindexer:
    """A Reindexer whose run() raises, to exercise the cron errors-to-Slack path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def run(self, *, full_rebuild: bool = False) -> Any:
        """Always raise (a reindex crash)."""
        raise RuntimeError("reindex exploded")


class _RecordingAlerter:
    """Records alert_exception calls (the errors-to-Slack seam, issue #15)."""

    def __init__(self) -> None:
        self.exceptions: list[tuple[str, BaseException]] = []

    def alert_exception(self, where: str, exc: BaseException) -> bool:
        """Record the alert and report success."""
        self.exceptions.append((where, exc))
        return True


def test_run_reindex_crash_alerts_then_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reindex crash posts an errors-to-Slack alert and re-raises (issue #15).

    Acceptance: a cron-entrypoint exception produces a visible Slack notification; the
    original error still propagates so the cron log records the non-zero exit.
    """
    import thoth.alerts as alerts_mod
    import thoth.reindex_from_vault as reindex_mod

    monkeypatch.setattr(reindex_mod, "Reindexer", _CrashingReindexer)
    recorder = _RecordingAlerter()
    monkeypatch.setattr(alerts_mod, "make_alerter", lambda cfg: recorder)
    config = load_config(
        {"PKM_VAULT": str(tmp_path), "THOTH_HOME": str(tmp_path / "home")}
    )
    namespace = __main__.build_parser().parse_args(["reindex"])

    with pytest.raises(RuntimeError, match="reindex exploded"):
        __main__.run_reindex(namespace, config)
    assert len(recorder.exceptions) == 1
    where, exc = recorder.exceptions[0]
    assert where == "cron: reindex"
    assert isinstance(exc, RuntimeError)


# --- run_summary (real SummaryEngine over a seeded vault, fake poster) -------------


class _FakePoster:
    """A Slack poster fake recording every ``chat.postMessage`` (no SDK, no network)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Record the channel + text."""
        self.posts.append((channel, text))
        return {"ok": True}


def _seed_minimal_vault(root: Path) -> None:
    """Lay down folders + spine + one due action so the daily digest is non-empty."""
    for folder in (
        "entities",
        "notes",
        "memories",
        "actions",
        "inbox",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")
    (root / "actions" / "call-bank.md").write_text(
        "---\n"
        "title: Call the bank\n"
        "type: action\n"
        "created: 2026-05-30\n"
        "updated: 2026-05-30\n"
        "source: slack\n"
        "tags: [task]\n"
        "status: todo\n"
        "due_date: 2026-05-15\n"
        "---\n\n# Call the bank\n",
        encoding="utf-8",
    )


@pytest.fixture
def vault_config(tmp_path: Path) -> Config:
    """A Config over a seeded tmp vault that also carries a summary channel."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_minimal_vault(root)
    return load_config(
        {
            "PKM_VAULT": str(root),
            "SLACK_SUMMARY_CHANNEL": "D_SUMMARY",
            # Keep the liveness-marker state.db (read by the heartbeat) under tmp_path.
            "THOTH_HOME": str(tmp_path / "home"),
        }
    )


@pytest.mark.parametrize("kind", ["daily", "weekly"])
def test_run_summary_posts_digest_to_configured_channel(
    vault_config: Config, kind: str
) -> None:
    """``thoth summary {daily|weekly}`` composes and posts to SLACK_SUMMARY_CHANNEL."""
    poster = _FakePoster()
    namespace = __main__.build_parser().parse_args(["summary", kind])
    __main__.run_summary(namespace, vault_config, poster_factory=lambda cfg: poster)

    assert len(poster.posts) == 1
    channel, text = poster.posts[0]
    assert channel == "D_SUMMARY"
    # The composed digest reflects the seeded overdue action: the daily digest names
    # it inline; the weekly digest reports it in the actions-status counts.
    if kind == "daily":
        assert "Call the bank" in text
    else:
        assert "Overdue: 1" in text
    assert text.startswith(f"{kind.capitalize()} PKM Summary")


def test_run_summary_skip_when_empty_does_not_post(tmp_path: Path) -> None:
    """``--skip-when-empty`` suppresses the post when the daily digest is empty."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    # Folders + spine only -> nothing actionable -> empty daily digest.
    for folder in (
        "entities",
        "notes",
        "memories",
        "actions",
        "inbox",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")
    config = load_config(
        {
            "PKM_VAULT": str(root),
            "SLACK_SUMMARY_CHANNEL": "D_SUMMARY",
            "THOTH_HOME": str(tmp_path / "home"),
        }
    )

    poster = _FakePoster()
    namespace = __main__.build_parser().parse_args(
        ["summary", "daily", "--skip-when-empty"]
    )
    __main__.run_summary(namespace, config, poster_factory=lambda cfg: poster)
    assert poster.posts == []


def test_run_summary_requires_summary_channel(tmp_path: Path) -> None:
    """A missing SLACK_SUMMARY_CHANNEL raises ConfigError before posting."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    for folder in (
        "entities",
        "notes",
        "memories",
        "actions",
        "inbox",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")
    config = load_config(
        {"PKM_VAULT": str(root), "THOTH_HOME": str(tmp_path / "home")}
    )  # no summary channel

    poster = _FakePoster()
    namespace = __main__.build_parser().parse_args(["summary", "daily"])
    with pytest.raises(ConfigError, match="SLACK_SUMMARY_CHANNEL"):
        __main__.run_summary(namespace, config, poster_factory=lambda cfg: poster)


def test_run_summary_daily_includes_liveness_heartbeat(
    vault_config: Config,
) -> None:
    """The daily digest posted by the cron entrypoint carries the liveness heartbeat.

    Acceptance (issue #15): the daily summary reports last-success timestamps for
    capture/reindex/push -- here a push marker is seeded in the same state.db the
    entrypoint reads, and the posted digest names it.
    """
    from thoth.state import MARKER_PUSH, MarkerStore

    markers = MarkerStore(vault_config.state_db_path, clock=lambda: 1_700_000_000.0)
    markers.record(MARKER_PUSH)

    poster = _FakePoster()
    namespace = __main__.build_parser().parse_args(["summary", "daily"])
    __main__.run_summary(namespace, vault_config, poster_factory=lambda cfg: poster)

    assert len(poster.posts) == 1
    _, text = poster.posts[0]
    assert "still alive -- last " in text
    # The seeded push marker shows a concrete time; absent stages read "never".
    assert "push 20" in text
    assert "ingest never" in text


def test_run_summary_crash_alerts_then_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A summary crash posts an errors-to-Slack alert and re-raises (issue #15)."""
    import thoth.alerts as alerts_mod
    import thoth.summary as summary_mod

    class _CrashingEngine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def daily_digest(self) -> Any:
            raise RuntimeError("summary exploded")

    monkeypatch.setattr(summary_mod, "SummaryEngine", _CrashingEngine)
    recorder = _RecordingAlerter()
    monkeypatch.setattr(alerts_mod, "make_alerter", lambda cfg: recorder)
    root = tmp_path / "pkm-vault"
    root.mkdir()
    config = load_config(
        {
            "PKM_VAULT": str(root),
            "SLACK_SUMMARY_CHANNEL": "D_SUMMARY",
            "THOTH_HOME": str(tmp_path / "home"),
        }
    )
    namespace = __main__.build_parser().parse_args(["summary", "daily"])

    with pytest.raises(RuntimeError, match="summary exploded"):
        __main__.run_summary(
            namespace, config, poster_factory=lambda cfg: _FakePoster()
        )
    assert recorder.exceptions[0][0] == "cron: summary"


# --- run_lint (real LintEngine over a seeded tmp vault) ----------------------------
#
# These exercise the full handler -- a real Vault and a real LintEngine over a crafted
# tmp_path vault -- so no boundary needs stubbing (the linter is a pure markdown scan,
# no daemon, no network). LintEngine is imported lazily inside each test body, mirroring
# run_lint's own lazy import, so this test module stays import-safe under collection.


def _count_log_blocks(log_path: Path) -> int:
    """Count ``## [`` dated blocks in a ``log.md`` (one per logged action)."""
    return sum(
        1
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## [")
    )


def _printed_total(out: str) -> int:
    """Extract ``N`` from the handler's ``lint: N issue(s) found`` tail line.

    Reading the total back out of the captured output (rather than re-running a second
    :class:`LintEngine`) keeps the assertion internally consistent with the very run the
    handler logged, independent of the wall clock the handler used for its stale window.
    """
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("lint: ") and stripped.endswith(" issue(s) found"):
            return int(stripped[len("lint: ") : -len(" issue(s) found")])
    raise AssertionError(f"no 'lint: N issue(s) found' tail line in output:\n{out}")


def _seed_vault_with_one_broken_link(root: Path) -> None:
    """Seed a spine plus one knowledge page carrying a single broken wikilink.

    The page is filed in ``notes/`` with otherwise-valid common frontmatter and an
    outbound ``[[no-such-page]]`` whose target does not exist, so a deterministic linter
    flags at least one issue (a broken wikilink). The ``log.md`` starts with the SPEC
    seed block, so a fresh log already has exactly one ``## [`` block.
    """
    for folder in (
        "entities",
        "notes",
        "memories",
        "actions",
        "inbox",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "raw" / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(
        "---\n"
        "title: Home\n"
        "type: summary\n"
        "updated: 2026-05-30\n"
        "---\n\n"
        "# Home\n\n"
        "## Knowledge catalog\n\n"
        "### Entities\n\n"
        "### Notes\n\n"
        "### Memories\n",
        encoding="utf-8",
    )
    (root / "SCHEMA.md").write_text(
        "# Vault Schema\n\n## Tag Taxonomy\n- concept\n- note\n- entity\n",
        encoding="utf-8",
    )
    (root / "log.md").write_text(
        "# Vault Log\n\n## [2026-05-30] create | Vault initialized\n",
        encoding="utf-8",
    )
    (root / "notes" / "widget.md").write_text(
        "---\n"
        "title: Widget\n"
        "type: note\n"
        "created: 2026-05-30\n"
        "updated: 2026-05-30\n"
        "source: slack\n"
        "tags: [concept]\n"
        "---\n\n"
        "# Widget\n\n"
        "A note that links to [[no-such-page]] which does not exist.\n",
        encoding="utf-8",
    )


def _seed_clean_spine_only_vault(root: Path) -> None:
    """Seed only the empty folders + a clean spine (no curated pages, no findings)."""
    for folder in (
        "entities",
        "notes",
        "memories",
        "actions",
        "inbox",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "raw" / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(
        "---\n"
        "title: Home\n"
        "type: summary\n"
        "updated: 2026-05-30\n"
        "---\n\n"
        "# Home\n\n"
        "## Knowledge catalog\n\n"
        "### Entities\n\n"
        "### Notes\n\n"
        "### Memories\n",
        encoding="utf-8",
    )
    (root / "SCHEMA.md").write_text(
        "# Vault Schema\n\n## Tag Taxonomy\n- concept\n- note\n- entity\n",
        encoding="utf-8",
    )
    (root / "log.md").write_text(
        "# Vault Log\n\n## [2026-05-30] create | Vault initialized\n",
        encoding="utf-8",
    )


@pytest.fixture
def broken_vault_config(tmp_path: Path) -> Config:
    """A Config over a seeded tmp vault holding one deliberately-broken page."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_vault_with_one_broken_link(root)
    return load_config({"PKM_VAULT": str(root)})


def test_run_lint_prints_report_and_appends_one_log_block(
    broken_vault_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """``thoth lint`` prints the report and appends exactly one log.md block.

    Over a vault whose only knowledge page carries a broken ``[[no-such-page]]``
    wikilink, the run reports at least one issue, names the offending page in the
    printed report, prints the ``lint: N issue(s) found`` tail, and grows ``log.md`` by
    exactly one ``## [`` block whose count matches the reported total. The total is read
    back from the handler's own output so the assertion stays consistent with the very
    run it logged (independent of the wall clock the stale window used).
    """
    root = broken_vault_config.vault_path
    log_path = root / "log.md"
    before = _count_log_blocks(log_path)

    namespace = __main__.build_parser().parse_args(["lint"])
    __main__.run_lint(namespace, broken_vault_config)

    out = capsys.readouterr().out
    total = _printed_total(out)
    # The broken page is an orphan, has a broken wikilink, and is absent from the index,
    # so at least one issue is guaranteed regardless of the run date.
    assert total >= 1
    # The offending page path appears in the grouped report (every Finding carries it).
    assert "notes/widget.md" in out
    # The broken wikilink itself is named in the report.
    assert "no-such-page" in out

    after = _count_log_blocks(log_path)
    assert after == before + 1
    # The appended block carries the same total the report printed.
    log_text = log_path.read_text(encoding="utf-8")
    assert f"lint | {total} issues found" in log_text


def test_run_lint_no_log_leaves_log_byte_identical(
    broken_vault_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """``thoth lint --no-log`` prints the report but never touches ``log.md``."""
    root = broken_vault_config.vault_path
    log_path = root / "log.md"
    before_bytes = log_path.read_bytes()

    namespace = __main__.build_parser().parse_args(["lint", "--no-log"])
    __main__.run_lint(namespace, broken_vault_config)

    out = capsys.readouterr().out
    assert _printed_total(out) >= 1
    # Byte-identical: the suppressed log path appended nothing.
    assert log_path.read_bytes() == before_bytes


def test_run_lint_clean_vault_reports_zero_and_logs_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean vault prints ``0 issue(s) found`` and still logs one zero-issue block."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_clean_spine_only_vault(root)
    config = load_config({"PKM_VAULT": str(root)})

    log_path = root / "log.md"
    before = _count_log_blocks(log_path)

    namespace = __main__.build_parser().parse_args(["lint"])
    __main__.run_lint(namespace, config)

    out = capsys.readouterr().out
    assert _printed_total(out) == 0
    assert "lint: 0 issue(s) found" in out

    assert _count_log_blocks(log_path) == before + 1
    assert "lint | 0 issues found" in log_path.read_text(encoding="utf-8")


def test_run_lint_clean_vault_no_log_does_not_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean vault with ``--no-log`` reports zero and appends no log block."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_clean_spine_only_vault(root)
    config = load_config({"PKM_VAULT": str(root)})

    log_path = root / "log.md"
    before_bytes = log_path.read_bytes()

    namespace = __main__.build_parser().parse_args(["lint", "--no-log"])
    __main__.run_lint(namespace, config)

    out = capsys.readouterr().out
    assert "lint: 0 issue(s) found" in out
    assert log_path.read_bytes() == before_bytes


def test_main_routes_lint_through_dispatch(
    monkeypatch: pytest.MonkeyPatch, broken_vault_config: Config
) -> None:
    """``thoth lint`` reaches ``run_lint`` via ``main`` with the loaded config."""
    seen: list[tuple[bool, Config]] = []

    def _record(ns: Any, cfg: Config) -> None:
        seen.append((bool(ns.no_log), cfg))

    monkeypatch.setattr(__main__, "run_lint", _record)
    monkeypatch.setattr(__main__, "load_config", lambda: broken_vault_config)

    __main__.main(["lint", "--no-log"])
    assert seen == [(True, broken_vault_config)]
