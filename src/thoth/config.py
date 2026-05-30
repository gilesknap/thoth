"""Load and validate thoth's runtime configuration.

This module is the Phase-0 foundation that every later module imports. It is the
single source of truth for the environment-variable names, documented defaults, the
resolved vault path, the ``~/.thoth`` home locations, and the ``obsidian://`` deep-link
format. No other module re-reads :data:`os.environ` for these values; callers invoke
:func:`load_config` once (typically near process entry) and pass the frozen
:class:`Config` down. This keeps the closed-surface promise auditable in one file.

Configuration is read from environment variables, optionally seeded from a ``.env``
file via ``python-dotenv``. Seeding is non-mutating: file values only fill gaps that the
real environment does not already provide, and :data:`os.environ` is never written to.

The module imports only the standard library at top level; ``python-dotenv`` is imported
lazily inside :func:`_read_dotenv` so the module stays import-safe even when the
dependency is not resolved (for example during doctest collection on a bare checkout).

Documented defaults (the single source of truth):

* ``OBSIDIAN_VAULT_NAME`` defaults to :data:`DEFAULT_OBSIDIAN_VAULT_NAME`
  (``pkm-vault``).
* ``THOTH_HOME`` defaults to :data:`DEFAULT_THOTH_HOME` (``~/.thoth``).
* ``ANTHROPIC_MODEL`` defaults to :data:`DEFAULT_ANTHROPIC_MODEL`
  (``claude-sonnet-4-6``).

Only ``PKM_VAULT`` is hard-required in Phase 0 (see :data:`REQUIRED_VARS`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_OBSIDIAN_VAULT_NAME: str = "pkm-vault"
"""Default registered Obsidian vault name used in ``obsidian://`` links."""

DEFAULT_ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
"""Default Anthropic model id (the dated fallback id belongs to ``llm.py``)."""

DEFAULT_THOTH_HOME: Path = Path.home() / ".thoth"
"""Default ``~/.thoth`` home, computed at import time (tests monkeypatch ``HOME``)."""

REQUIRED_VARS: tuple[str, ...] = ("PKM_VAULT",)
"""Environment variables that must be present; only the vault path in Phase 0."""


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable, validated thoth runtime configuration."""

    vault_path: Path
    vault_name: str
    thoth_home: Path
    anthropic_api_key: str | None
    anthropic_model: str
    slack_bot_token: str | None
    slack_app_token: str | None
    slack_summary_channel: str | None
    exa_api_key: str | None
    firecrawl_api_key: str | None
    gemini_api_key: str | None

    @property
    def state_db_path(self) -> Path:
        """Absolute path to the transient state DB (``<thoth_home>/state.db``)."""
        return self.thoth_home / "state.db"

    @property
    def env_file_path(self) -> Path:
        """Absolute path to the secrets file (``<thoth_home>/.env``, chmod 600)."""
        return self.thoth_home / ".env"

    def require_anthropic(self) -> str:
        """Return the Anthropic API key or raise :class:`ConfigError` if unset."""
        if self.anthropic_api_key is None:
            raise ConfigError(
                "ANTHROPIC_API_KEY is required for this operation but is not set"
            )
        return self.anthropic_api_key

    def require_slack(self) -> tuple[str, str]:
        """Return ``(bot_token, app_token)`` or raise :class:`ConfigError`.

        Raises if either ``SLACK_BOT_TOKEN`` or ``SLACK_APP_TOKEN`` is unset.
        """
        missing = [
            name
            for name, value in (
                ("SLACK_BOT_TOKEN", self.slack_bot_token),
                ("SLACK_APP_TOKEN", self.slack_app_token),
            )
            if value is None
        ]
        if missing:
            raise ConfigError(
                "Slack requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN; "
                f"missing: {', '.join(missing)}"
            )
        # Both are non-None here; assert for the type checker.
        assert self.slack_bot_token is not None
        assert self.slack_app_token is not None
        return self.slack_bot_token, self.slack_app_token

    def require_slack_summary_channel(self) -> str:
        """Return the summary DM/channel id or raise :class:`ConfigError` if unset.

        The daily/weekly digest (SPEC section 9) is posted to this Slack channel by the
        ``thoth summary`` cron entrypoint. It lives in configuration
        (``SLACK_SUMMARY_CHANNEL``) rather than as a literal so the target is not baked
        into the code.
        """
        if self.slack_summary_channel is None:
            raise ConfigError(
                "SLACK_SUMMARY_CHANNEL is required to post a summary but is not set"
            )
        return self.slack_summary_channel

    def obsidian_uri(self, vault_relative_path: str) -> str:
        """Build an ``obsidian://open`` deep link for a vault-relative path.

        The path must be vault-relative (no leading ``/``); it is percent-encoded in
        full, path separators included, per the SPEC Appendix. The vault name is also
        encoded. This does not assert the path is inside the vault (the caller passes
        an already-validated relative path); disk-side confinement lives in
        ``vault.py``.

        Raises:
            ValueError: if ``vault_relative_path`` is empty or absolute.
        """
        if not vault_relative_path:
            raise ValueError("vault_relative_path must be a non-empty relative path")
        is_absolute = (
            vault_relative_path.startswith("/")
            or Path(vault_relative_path).is_absolute()
        )
        if is_absolute:
            raise ValueError(
                "vault_relative_path must be vault-relative, not absolute: "
                f"{vault_relative_path!r}"
            )
        vault = quote(self.vault_name, safe="")
        file = quote(vault_relative_path, safe="")
        return f"obsidian://open?vault={vault}&file={file}"


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
    use_dotenv: bool = True,
) -> Config:
    """Build a :class:`Config` from ``env`` (defaults to :data:`os.environ`).

    Resolution order (highest precedence first): the explicit ``env`` mapping (or the
    real :data:`os.environ`) wins, then values read from ``env_file`` (or
    ``<THOTH_HOME>/.env`` when ``use_dotenv`` is true and that file exists), then the
    documented defaults. The function never mutates :data:`os.environ`.

    Args:
        env: Mapping of environment variables. Defaults to :data:`os.environ`.
        env_file: Explicit ``.env`` path to seed from. When ``None`` and
            ``use_dotenv`` is true, ``<THOTH_HOME>/.env`` is used if it exists.
        use_dotenv: When false, no ``.env`` file is read even if present.

    Returns:
        A validated, frozen :class:`Config`.

    Raises:
        ConfigError: naming every missing variable in :data:`REQUIRED_VARS`.
    """
    real_env: Mapping[str, str] = os.environ if env is None else env

    # Resolve THOTH_HOME first; it determines the default .env location. The real
    # environment takes precedence here too, so a THOTH_HOME in env points the
    # default .env lookup at the right place.
    thoth_home_raw = _opt(real_env.get("THOTH_HOME"))
    thoth_home = (
        _resolve_path(thoth_home_raw)
        if thoth_home_raw is not None
        else DEFAULT_THOTH_HOME
    )

    file_values: dict[str, str] = {}
    if use_dotenv:
        if env_file is not None:
            file_values = _read_dotenv(Path(env_file))
        else:
            default_env_file = thoth_home / ".env"
            if default_env_file.is_file():
                file_values = _read_dotenv(default_env_file)

    def lookup(name: str) -> str | None:
        """Real env wins over .env file values; empty strings count as unset."""
        value = real_env.get(name)
        if value is None:
            value = file_values.get(name)
        return _opt(value)

    missing = [name for name in REQUIRED_VARS if lookup(name) is None]
    if missing:
        raise ConfigError(
            "Missing required configuration: " + ", ".join(sorted(missing))
        )

    vault_raw = lookup("PKM_VAULT")
    # vault_raw is non-None: PKM_VAULT is in REQUIRED_VARS and passed the check above.
    assert vault_raw is not None

    return Config(
        vault_path=_resolve_path(vault_raw),
        vault_name=lookup("OBSIDIAN_VAULT_NAME") or DEFAULT_OBSIDIAN_VAULT_NAME,
        thoth_home=thoth_home,
        anthropic_api_key=lookup("ANTHROPIC_API_KEY"),
        anthropic_model=lookup("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
        slack_bot_token=lookup("SLACK_BOT_TOKEN"),
        slack_app_token=lookup("SLACK_APP_TOKEN"),
        slack_summary_channel=lookup("SLACK_SUMMARY_CHANNEL"),
        exa_api_key=lookup("EXA_API_KEY"),
        firecrawl_api_key=lookup("FIRECRAWL_API_KEY"),
        gemini_api_key=lookup("GEMINI_API_KEY"),
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    """Return key/value pairs from a .env file, ``{}`` if it is absent.

    ``python-dotenv`` is imported here (not at module scope) to keep the module
    import-safe when the dependency is unresolved. Keys whose value is ``None`` (a
    bare ``KEY`` line) are dropped so the merge layer only ever holds ``str`` values.
    """
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _resolve_path(value: str) -> Path:
    """Expand ``~`` and env vars then resolve to an absolute path (no disk access)."""
    expanded = os.path.expanduser(os.path.expandvars(value))
    return Path(expanded).resolve()


def _opt(value: str | None) -> str | None:
    """Treat an empty string as unset (shell/.env habit: blank means absent)."""
    return value or None
