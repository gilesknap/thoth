"""Tests for :mod:`thoth.config`."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from thoth.config import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_DAILY_LLM_BUDGET,
    DEFAULT_LOG_LEVEL,
    DEFAULT_OBSIDIAN_VAULT_NAME,
    Config,
    ConfigError,
    load_config,
)

# python-dotenv is added as a runtime dep by the integrate step; it may be absent on a
# bare checkout. The .env-seeding tests need it, so skip them gracefully if missing.
HAVE_DOTENV = importlib.util.find_spec("dotenv") is not None
requires_dotenv = pytest.mark.skipif(
    not HAVE_DOTENV, reason="python-dotenv not installed"
)

# Obviously-fake placeholders only (gitleaks scans the commit).
FAKE_TOKEN = "test-token"
FAKE_SHORT = "x" * 8


def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point HOME (and USERPROFILE) at ``home`` so ~ expansion is deterministic."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_happy_path_minimal() -> None:
    """A single PKM_VAULT yields defaults everywhere and None for all tokens."""
    cfg = load_config({"PKM_VAULT": "/opt/pkm-vault"})
    assert isinstance(cfg, Config)
    assert cfg.vault_path == Path("/opt/pkm-vault")
    assert cfg.vault_name == DEFAULT_OBSIDIAN_VAULT_NAME == "pkm-vault"
    assert cfg.anthropic_model == DEFAULT_ANTHROPIC_MODEL == "claude-sonnet-4-6"
    assert str(cfg.thoth_home).endswith(".thoth")
    assert cfg.anthropic_api_key is None
    assert cfg.slack_bot_token is None
    assert cfg.slack_app_token is None
    assert cfg.slack_summary_channel is None
    assert cfg.slack_alert_channel is None
    assert cfg.slack_allowed_users is None
    assert cfg.exa_api_key is None
    assert cfg.firecrawl_api_key is None
    assert cfg.gemini_api_key is None
    assert cfg.daily_llm_budget == DEFAULT_DAILY_LLM_BUDGET == 200
    assert cfg.log_level == DEFAULT_LOG_LEVEL == "INFO"


def test_all_fields_populated() -> None:
    """Every var maps onto the matching attribute."""
    env = {
        "PKM_VAULT": "/opt/pkm-vault",
        "OBSIDIAN_VAULT_NAME": "my-vault",
        "THOTH_HOME": "/tmp/h",
        "ANTHROPIC_API_KEY": FAKE_TOKEN,
        "ANTHROPIC_MODEL": "claude-x-1",
        "SLACK_BOT_TOKEN": FAKE_SHORT,
        "SLACK_APP_TOKEN": FAKE_SHORT,
        "SLACK_SUMMARY_CHANNEL": "D0B61LKA3NV",
        "SLACK_ALERT_CHANNEL": "C-ALERTS",
        "SLACK_ALLOWED_USERS": "U1 U2",
        "EXA_API_KEY": FAKE_TOKEN,
        "FIRECRAWL_API_KEY": FAKE_TOKEN,
        "GEMINI_API_KEY": FAKE_TOKEN,
        "THOTH_DAILY_LLM_BUDGET": "50",
        "THOTH_LOG_LEVEL": "DEBUG",
    }
    cfg = load_config(env)
    assert cfg.vault_path == Path("/opt/pkm-vault")
    assert cfg.vault_name == "my-vault"
    assert cfg.thoth_home == Path("/tmp/h")
    assert cfg.anthropic_api_key == FAKE_TOKEN
    assert cfg.anthropic_model == "claude-x-1"
    assert cfg.slack_bot_token == FAKE_SHORT
    assert cfg.slack_app_token == FAKE_SHORT
    assert cfg.slack_summary_channel == "D0B61LKA3NV"
    assert cfg.slack_alert_channel == "C-ALERTS"
    assert cfg.slack_allowed_users == "U1 U2"
    assert cfg.exa_api_key == FAKE_TOKEN
    assert cfg.firecrawl_api_key == FAKE_TOKEN
    assert cfg.gemini_api_key == FAKE_TOKEN
    assert cfg.daily_llm_budget == 50
    assert cfg.log_level == "DEBUG"


def test_daily_llm_budget_rejects_non_integer() -> None:
    """A non-integer THOTH_DAILY_LLM_BUDGET is a clear ConfigError, not a default."""
    with pytest.raises(ConfigError, match="THOTH_DAILY_LLM_BUDGET"):
        load_config({"PKM_VAULT": "/x", "THOTH_DAILY_LLM_BUDGET": "lots"})


def test_daily_llm_budget_zero_disables(tmp_path: Path) -> None:
    """A zero/negative budget is honoured verbatim (it disables the guard)."""
    cfg = load_config({"PKM_VAULT": "/x", "THOTH_DAILY_LLM_BUDGET": "0"})
    assert cfg.daily_llm_budget == 0


def test_log_level_defaults_and_override() -> None:
    """THOTH_LOG_LEVEL defaults to INFO and an explicit value wins (issue #52)."""
    assert load_config({"PKM_VAULT": "/x"}).log_level == "INFO"
    override = load_config({"PKM_VAULT": "/x", "THOTH_LOG_LEVEL": "DEBUG"})
    assert override.log_level == "DEBUG"


def test_log_level_empty_falls_back_to_default() -> None:
    """An empty THOTH_LOG_LEVEL falls back to the documented default (issue #52)."""
    cfg = load_config({"PKM_VAULT": "/x", "THOTH_LOG_LEVEL": ""})
    assert cfg.log_level == DEFAULT_LOG_LEVEL == "INFO"


def test_require_slack_summary_channel() -> None:
    """require_slack_summary_channel returns the id or raises when unset."""
    cfg = load_config({"PKM_VAULT": "/x", "SLACK_SUMMARY_CHANNEL": "D123"})
    assert cfg.require_slack_summary_channel() == "D123"

    cfg_missing = load_config({"PKM_VAULT": "/x"})
    with pytest.raises(ConfigError, match="SLACK_SUMMARY_CHANNEL"):
        cfg_missing.require_slack_summary_channel()


def test_alert_target_resolution() -> None:
    """alert_target: SLACK_ALERT_CHANNEL, else first allow-listed DM, else None."""
    explicit = load_config(
        {
            "PKM_VAULT": "/x",
            "SLACK_ALERT_CHANNEL": "C-ALERTS",
            "SLACK_ALLOWED_USERS": "U1 U2",
        }
    )
    assert explicit.alert_target() == "C-ALERTS"

    fallback = load_config(
        {"PKM_VAULT": "/x", "SLACK_ALLOWED_USERS": "<@U9|giles>, U8"}
    )
    assert fallback.alert_target() == "U9"

    none_cfg = load_config({"PKM_VAULT": "/x"})
    assert none_cfg.alert_target() is None


def test_missing_required_raises() -> None:
    """An empty env raises ConfigError naming PKM_VAULT."""
    with pytest.raises(ConfigError, match="PKM_VAULT"):
        load_config({})


def test_config_error_lists_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigError mentions every missing required var (guards future growth)."""
    monkeypatch.setattr(
        "thoth.config.REQUIRED_VARS", ("PKM_VAULT", "SOME_OTHER_REQUIRED")
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config({})
    message = str(exc_info.value)
    assert "PKM_VAULT" in message
    assert "SOME_OTHER_REQUIRED" in message


def test_no_mutation_of_input_and_environ() -> None:
    """load_config mutates neither the input mapping nor os.environ."""
    env = {"PKM_VAULT": "/opt/pkm-vault"}
    env_snapshot = dict(env)
    environ_keys_before = set(os.environ)
    environ_snapshot = dict(os.environ)
    load_config(env)
    assert env == env_snapshot
    assert set(os.environ) == environ_keys_before
    assert dict(os.environ) == environ_snapshot


@requires_dotenv
def test_env_file_seeding(tmp_path: Path) -> None:
    """A .env file fills gaps the env mapping leaves empty."""
    env_file = tmp_path / ".env"
    env_file.write_text("PKM_VAULT=/opt/pkm-vault\nEXA_API_KEY=test-token\n")
    cfg = load_config({}, env_file=env_file)
    assert cfg.vault_path == Path("/opt/pkm-vault")
    assert cfg.exa_api_key == "test-token"


@requires_dotenv
def test_environ_precedence_over_env_file(tmp_path: Path) -> None:
    """An explicit env mapping value wins over the .env file value."""
    env_file = tmp_path / ".env"
    env_file.write_text("PKM_VAULT=/file\n")
    cfg = load_config({"PKM_VAULT": "/over"}, env_file=env_file)
    assert cfg.vault_path == Path("/over")


@requires_dotenv
def test_use_dotenv_false_ignores_default_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With use_dotenv=False the default <THOTH_HOME>/.env is not read."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXA_API_KEY=test-token\n")
    cfg = load_config(
        {"PKM_VAULT": "/x", "THOTH_HOME": str(tmp_path)},
        use_dotenv=False,
    )
    assert cfg.exa_api_key is None


@requires_dotenv
def test_default_env_file_is_read_from_thoth_home(tmp_path: Path) -> None:
    """When use_dotenv and <THOTH_HOME>/.env exists, it seeds config."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXA_API_KEY=test-token\n")
    cfg = load_config({"PKM_VAULT": "/x", "THOTH_HOME": str(tmp_path)})
    assert cfg.exa_api_key == "test-token"


def test_missing_env_file_is_silent(tmp_path: Path) -> None:
    """A non-existent explicit env_file does not raise; falls through to env."""
    cfg = load_config({"PKM_VAULT": "/opt/pkm-vault"}, env_file=tmp_path / "nope.env")
    assert cfg.vault_path == Path("/opt/pkm-vault")
    assert cfg.exa_api_key is None


def test_path_resolution_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PKM_VAULT='~/vault' resolves against the monkeypatched HOME."""
    _set_home(monkeypatch, tmp_path)
    cfg = load_config({"PKM_VAULT": "~/vault"})
    assert cfg.vault_path == (tmp_path / "vault").resolve()
    assert cfg.vault_path.is_absolute()


def test_path_resolution_relative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative PKM_VAULT resolves against cwd to an absolute path."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config({"PKM_VAULT": "vault/sub"})
    assert cfg.vault_path == (tmp_path / "vault" / "sub").resolve()
    assert cfg.vault_path.is_absolute()


def test_path_resolution_collapses_dotdot() -> None:
    """resolve() collapses '..' segments in the vault path."""
    cfg = load_config({"PKM_VAULT": "/opt/pkm-vault/../other"})
    assert cfg.vault_path == Path("/opt/other")


def test_thoth_home_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With HOME monkeypatched and THOTH_HOME unset, default is ~/.thoth.

    DEFAULT_THOTH_HOME is computed at import time, so we patch the module constant to
    reflect the monkeypatched HOME (matching what a fresh import would produce).
    """
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr("thoth.config.DEFAULT_THOTH_HOME", tmp_path / ".thoth")
    cfg = load_config({"PKM_VAULT": "/x"})
    assert cfg.thoth_home == tmp_path / ".thoth"


def test_thoth_home_explicit_overrides_default(tmp_path: Path) -> None:
    """An explicit THOTH_HOME overrides the default."""
    cfg = load_config({"PKM_VAULT": "/x", "THOTH_HOME": str(tmp_path / "custom")})
    assert cfg.thoth_home == (tmp_path / "custom").resolve()


def test_state_db_and_env_file_paths() -> None:
    """Derived helpers point at the gitignored ~/.thoth locations."""
    cfg = load_config({"PKM_VAULT": "/x", "THOTH_HOME": "/tmp/h"})
    assert cfg.state_db_path == Path("/tmp/h") / "state.db"
    assert cfg.env_file_path == Path("/tmp/h") / ".env"


def test_require_anthropic_returns_key() -> None:
    """require_anthropic returns the key when it is set."""
    cfg = load_config({"PKM_VAULT": "/x", "ANTHROPIC_API_KEY": FAKE_TOKEN})
    assert cfg.require_anthropic() == FAKE_TOKEN


def test_require_anthropic_raises_when_unset() -> None:
    """require_anthropic raises ConfigError naming the var when unset."""
    cfg = load_config({"PKM_VAULT": "/x"})
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        cfg.require_anthropic()


def test_require_slack_returns_pair() -> None:
    """require_slack returns (bot, app) when both are set."""
    cfg = load_config(
        {
            "PKM_VAULT": "/x",
            "SLACK_BOT_TOKEN": FAKE_SHORT,
            "SLACK_APP_TOKEN": FAKE_SHORT,
        }
    )
    assert cfg.require_slack() == (FAKE_SHORT, FAKE_SHORT)


@pytest.mark.parametrize(
    "extra",
    [
        {"SLACK_BOT_TOKEN": FAKE_SHORT},
        {"SLACK_APP_TOKEN": FAKE_SHORT},
        {},
    ],
    ids=["only-bot", "only-app", "neither"],
)
def test_require_slack_raises_when_incomplete(extra: dict[str, str]) -> None:
    """require_slack raises ConfigError when either token is missing."""
    cfg = load_config({"PKM_VAULT": "/x", **extra})
    with pytest.raises(ConfigError):
        cfg.require_slack()


def test_obsidian_uri_matches_spec_example() -> None:
    """obsidian_uri matches the SPEC Appendix worked example (slash -> %2F)."""
    cfg = load_config({"PKM_VAULT": "/x"})
    assert (
        cfg.obsidian_uri("entities/exa-search.md")
        == "obsidian://open?vault=pkm-vault&file=entities%2Fexa-search.md"
    )


def test_obsidian_uri_encodes_spaces_and_vault_name() -> None:
    """Spaces and reserved chars are encoded; the vault name is quoted too."""
    cfg = load_config({"PKM_VAULT": "/x", "OBSIDIAN_VAULT_NAME": "my vault"})
    uri = cfg.obsidian_uri("concepts/distributed systems.md")
    assert uri == (
        "obsidian://open?vault=my%20vault&file=concepts%2Fdistributed%20systems.md"
    )


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "/entities/exa-search.md", ""],
    ids=["absolute", "leading-slash", "empty"],
)
def test_obsidian_uri_rejects_absolute_and_empty(bad_path: str) -> None:
    """obsidian_uri rejects absolute / leading-slash paths and empty input."""
    cfg = load_config({"PKM_VAULT": "/x"})
    with pytest.raises(ValueError):
        cfg.obsidian_uri(bad_path)


def test_config_is_frozen() -> None:
    """Config is immutable: setting an attribute raises FrozenInstanceError."""
    import dataclasses

    cfg = load_config({"PKM_VAULT": "/x"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.vault_path = Path("/elsewhere")  # type: ignore[misc]


def test_empty_optional_treated_as_unset() -> None:
    """An empty-string optional var is treated as unset (blank == absent)."""
    cfg = load_config({"PKM_VAULT": "/x", "EXA_API_KEY": ""})
    assert cfg.exa_api_key is None


def test_empty_optional_falls_back_to_default() -> None:
    """An empty-string ANTHROPIC_MODEL falls back to the documented default."""
    cfg = load_config({"PKM_VAULT": "/x", "ANTHROPIC_MODEL": ""})
    assert cfg.anthropic_model == DEFAULT_ANTHROPIC_MODEL
