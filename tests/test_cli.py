"""Tests for the ``thoth`` command-line dispatch (:mod:`thoth.__main__`).

The four Phase-3 entrypoints are reachable as subcommands -- ``slack``, ``mcp``,
``reindex`` and ``summary`` -- each loading the config once and constructing the
collaborator graph before delegating. These tests exercise the parser and the dispatch
wiring against fakes: the blocking daemons (``slack`` / ``mcp``) are checked only at the
routing level (their ``run`` functions are monkeypatched so nothing blocks or imports
the optional clients), ``reindex`` is driven against a fake :class:`Reindexer`, and
``summary`` is driven end-to-end over a real seeded vault with a fake Slack poster (so
the 07:00 / Mon-07:00 digest path is proven to compose and post without the Slack SDK).
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


@pytest.mark.parametrize("command", ["slack", "mcp", "reindex", "summary"])
def test_build_parser_recognises_each_subcommand(command: str) -> None:
    """Each Phase-3 subcommand is recognised and sets ``command``."""
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

    for name in ("run_slack", "run_mcp", "run_reindex", "run_summary"):

        def _record(ns: Any, cfg: Config, _name: str = name) -> None:
            calls.append((_name, cfg))

        monkeypatch.setattr(__main__, name, _record)

    __main__.main(["slack"])
    __main__.main(["mcp"])
    __main__.main(["reindex"])
    __main__.main(["summary", "daily"])

    assert [name for name, _ in calls] == [
        "run_slack",
        "run_mcp",
        "run_reindex",
        "run_summary",
    ]
    assert all(cfg is stub_config for _, cfg in calls)


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
        "concepts",
        "comparisons",
        "queries",
        "actions",
        "media",
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
        "concepts",
        "comparisons",
        "queries",
        "actions",
        "media",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")
    config = load_config({"PKM_VAULT": str(root), "SLACK_SUMMARY_CHANNEL": "D_SUMMARY"})

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
        "concepts",
        "comparisons",
        "queries",
        "actions",
        "media",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")
    config = load_config({"PKM_VAULT": str(root)})  # no summary channel

    poster = _FakePoster()
    namespace = __main__.build_parser().parse_args(["summary", "daily"])
    with pytest.raises(ConfigError, match="SLACK_SUMMARY_CHANNEL"):
        __main__.run_summary(namespace, config, poster_factory=lambda cfg: poster)
