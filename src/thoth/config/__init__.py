"""Loads and validates thoth's runtime configuration.

The Phase-0 foundation every later module imports, and the single source of truth for
the environment-variable names, documented defaults, the resolved vault path, the home
locations and the deep-link format. No other module re-reads the environment for these
values. Callers load the config once near process entry and pass the frozen result down,
which keeps the closed-surface promise auditable in one place.

Configuration comes from environment variables, optionally seeded from a ``.env`` file.
Seeding is non-mutating: file values only fill gaps the real environment does not
provide, and the environment is never written to.

Only the standard library is imported at top level, and ``python-dotenv`` is imported
lazily, so the package stays import-safe even when that dependency is unresolved.

The documented defaults:

* ``OBSIDIAN_VAULT_NAME`` defaults to ``pkm-vault``.
* ``THOTH_HOME`` defaults to ``~/.thoth``.
* ``THOTH_TIMEZONE`` defaults to ``Europe/London``, the IANA zone every calendar-date
  computation runs on. A bogus name fails fast.
* ``ANTHROPIC_MODEL`` defaults to :data:`DEFAULT_ANTHROPIC_MODEL`.
* ``THOTH_ANALYSE_MODEL`` is unset, so the folded vision call resolves to the default
  model. Set it to drop that call to a cheaper model without moving the default.
* ``THOTH_DIAGRAM_MODEL`` is unset for the same reason. That call needs spatial
  reasoning plus valid JSON, so it is worth pinning to a stronger model on its own.
* ``THOTH_INTENT_MODEL`` is unset, and the free-text gate then falls back to a cheap
  Haiku. The gate is a one-shot routing call, so cheap is the point.
* ``THOTH_HINDSIGHT_BASE_URL`` defaults to ``http://127.0.0.1:8888``, the standalone
  ``hindsight-api`` server the HTTP client talks to.
* ``THOTH_LOG_LEVEL`` defaults to ``INFO``, which the daemon entrypoint passes to
  :func:`logging.basicConfig` so the appliance is not silent on the happy path.
* ``SLACK_ALERT_CHANNEL`` is the unattended alert target (issue #15). Unset, it falls
  back to the first allow-listed user id as a DM.
* ``SLACK_CAPTURE_CHANNEL`` is the dedicated private channel the daemon listens and
  replies in (issue #61). It is required to start ``thoth slack``, with no DM fallback.

Only ``PKM_VAULT`` is hard-required, per :data:`REQUIRED_VARS`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import Config, ConfigError
from .model import _strip_user_token as _strip_user_token

__all__ = [
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_DAILY_LLM_BUDGET",
    "DEFAULT_HINDSIGHT_BASE_URL",
    "DEFAULT_IMAGE_RESIZE_THRESHOLD_BYTES",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_MAX_ANALYSE_IMAGES",
    "DEFAULT_OBSIDIAN_VAULT_NAME",
    "DEFAULT_THOTH_HOME",
    "DEFAULT_TIMEZONE",
    "REQUIRED_VARS",
    "Config",
    "ConfigError",
    "load_config",
]

DEFAULT_OBSIDIAN_VAULT_NAME: str = "pkm-vault"
"""Default registered Obsidian vault name used in ``obsidian://`` links."""

DEFAULT_ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
"""Default Anthropic model id."""

DEFAULT_TIMEZONE: str = "Europe/London"
"""Default IANA timezone for every calendar-date computation (the owner's locale).

``THOTH_TIMEZONE`` overrides it; it governs the day boundary for the daily budget,
the summary/alert schedules, lint freshness, and the relative-date resolution that
turns a captured "monday" into a concrete ``due_date``.
"""

DEFAULT_LOG_LEVEL: str = "INFO"
"""Default logging level (issue #52); ``THOTH_LOG_LEVEL`` overrides it at the daemon.

Honoured once at process start by :func:`logging.basicConfig` in the daemon entrypoint,
so the concise per-operation success lines (ingest/query/research/intent) are visible
without code changes; set ``THOTH_LOG_LEVEL=DEBUG`` for more, ``WARNING`` for less.
"""

DEFAULT_THOTH_HOME: Path = Path.home() / ".thoth"
"""Default ``~/.thoth`` home, computed at import time (tests monkeypatch ``HOME``)."""

DEFAULT_HINDSIGHT_BASE_URL: str = "http://127.0.0.1:8888"
"""Default ``hindsight-api`` base URL; ``THOTH_HINDSIGHT_BASE_URL`` overrides it.

The Hindsight seam (:mod:`thoth.hindsight`) is an HTTP client to a standalone
``hindsight-api`` server, by default the loopback instance on ``:8888``.
"""

DEFAULT_DAILY_LLM_BUDGET: int = 200
"""Default combined daily LLM call budget (issue #16), sized for personal use.

The cap on the appliance's own Anthropic calls plus the Gemini fact-extraction triggered
via Hindsight ``retain``, per Europe/London day; ``THOTH_DAILY_LLM_BUDGET`` overrides it
and a non-positive value disables the guard. See :mod:`thoth.budget`.
"""

DEFAULT_IMAGE_RESIZE_THRESHOLD_BYTES: int = 2 * 1024 * 1024
"""Default size above which a captured image is downscaled before storage + analysis.

An image whose encoded bytes exceed this (2 MB) is scaled down so its longest edge is at
most ~1568px (the point above which Claude's vision API downsamples anyway) *before* it
is hashed, written to ``raw/assets/``, or sent to the vision model -- so the reduced
binary is both what the vault commits and what the LLM sees (issue #108).
``THOTH_IMAGE_RESIZE_THRESHOLD_BYTES`` overrides it; a non-positive value disables
resizing. See :mod:`thoth.images`.
"""

DEFAULT_MAX_ANALYSE_IMAGES: int = 6
"""Default cap on how many images a multi-image batch sends to ONE analyse call (#124).

An all-image Slack batch is curated as one page with one shared summary/tag set, so
every image is sent as a block in a SINGLE vision call (one charge against the daily
budget guard). This caps the images-per-call so a pathological batch cannot blow up the
vision payload: the first ``THOTH_MAX_ANALYSE_IMAGES`` images are analysed and any
extras are logged-and-skipped from that call (they are still saved + embedded). A
non-positive value disables the cap (analyse every image).
"""

REQUIRED_VARS: tuple[str, ...] = ("PKM_VAULT",)
"""Environment variables that must be present; only the vault path in Phase 0."""


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
    use_dotenv: bool = True,
) -> Config:
    """Builds a config from the environment.

    The real environment wins, then values from the ``.env`` file, then the documented
    defaults. The environment is never mutated. ``THOTH_HOME`` is the one exception,
    read from the real environment alone, since it decides which ``.env`` to load.

    Args:
        env: Environment mapping, defaulting to the real one.
        env_file: Explicit path to seed from. None uses the home ``.env`` when it
            exists and dotenv is enabled.
        use_dotenv: False reads no ``.env`` file even when one is present.

    Returns:
        The validated, frozen config.

    Raises:
        ConfigError: naming every missing required variable.
    """
    real_env: Mapping[str, str] = os.environ if env is None else env

    # Resolve the home first, since it determines the default .env location. The real
    # environment wins here too, so a home set in env points the lookup at the right
    # place
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
        """Reads one name, with the real environment winning over the file.

        An empty string counts as unset.
        """
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
    # Non-None, since the vault path is required and passed the check above
    assert vault_raw is not None

    config = Config(
        vault_path=_resolve_path(vault_raw),
        vault_name=lookup("OBSIDIAN_VAULT_NAME") or DEFAULT_OBSIDIAN_VAULT_NAME,
        thoth_home=thoth_home,
        timezone=_tz_opt(lookup("THOTH_TIMEZONE")),
        log_level=lookup("THOTH_LOG_LEVEL") or DEFAULT_LOG_LEVEL,
        anthropic_api_key=lookup("ANTHROPIC_API_KEY"),
        anthropic_model=lookup("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
        analyse_model=lookup("THOTH_ANALYSE_MODEL"),
        diagram_model=lookup("THOTH_DIAGRAM_MODEL"),
        intent_model=lookup("THOTH_INTENT_MODEL"),
        slack_bot_token=lookup("SLACK_BOT_TOKEN"),
        slack_app_token=lookup("SLACK_APP_TOKEN"),
        slack_summary_channel=lookup("SLACK_SUMMARY_CHANNEL"),
        slack_alert_channel=lookup("SLACK_ALERT_CHANNEL"),
        slack_allowed_users=lookup("SLACK_ALLOWED_USERS"),
        slack_capture_channel=lookup("SLACK_CAPTURE_CHANNEL"),
        firecrawl_api_key=lookup("FIRECRAWL_API_KEY"),
        gemini_api_key=lookup("GEMINI_API_KEY"),
        hindsight_base_url=lookup("THOTH_HINDSIGHT_BASE_URL")
        or DEFAULT_HINDSIGHT_BASE_URL,
        daily_llm_budget=_int_opt(
            lookup("THOTH_DAILY_LLM_BUDGET"),
            default=DEFAULT_DAILY_LLM_BUDGET,
            name="THOTH_DAILY_LLM_BUDGET",
        ),
        image_resize_threshold_bytes=_int_opt(
            lookup("THOTH_IMAGE_RESIZE_THRESHOLD_BYTES"),
            default=DEFAULT_IMAGE_RESIZE_THRESHOLD_BYTES,
            name="THOTH_IMAGE_RESIZE_THRESHOLD_BYTES",
        ),
        max_analyse_images=_int_opt(
            lookup("THOTH_MAX_ANALYSE_IMAGES"),
            default=DEFAULT_MAX_ANALYSE_IMAGES,
            name="THOTH_MAX_ANALYSE_IMAGES",
        ),
        mcp_api_keys=lookup("THOTH_MCP_API_KEYS"),
        mcp_cf_access_team_domain=lookup("THOTH_MCP_CF_ACCESS_TEAM_DOMAIN"),
        mcp_cf_access_aud=lookup("THOTH_MCP_CF_ACCESS_AUD"),
        mcp_allowed_hosts=lookup("THOTH_MCP_ALLOWED_HOSTS"),
        mcp_allowed_origins=lookup("THOTH_MCP_ALLOWED_ORIGINS"),
        github_oauth_client_id=lookup("GITHUB_OAUTH_CLIENT_ID"),
        github_oauth_client_secret=lookup("GITHUB_OAUTH_CLIENT_SECRET"),
        jwt_signing_secret=lookup("THOTH_JWT_SIGNING_SECRET"),
        allowed_github_users=lookup("THOTH_ALLOWED_GITHUB_USERS"),
        oauth_server_url=lookup("THOTH_OAUTH_SERVER_URL"),
    )

    # OAuth is additive and opt-in, so with none of its settings the server starts in
    # key-only mode. But a partial configuration would run half-open, so if any OAuth
    # var is set we require the full set, failing fast and naming what is missing. The
    # allow-list counts as present even though it is not one of the four required
    oauth_vars_present = any(
        lookup(name) is not None
        for name in (
            "GITHUB_OAUTH_CLIENT_ID",
            "GITHUB_OAUTH_CLIENT_SECRET",
            "THOTH_JWT_SIGNING_SECRET",
            "THOTH_OAUTH_SERVER_URL",
            "THOTH_ALLOWED_GITHUB_USERS",
        )
    )
    if oauth_vars_present and not config.oauth_enabled():
        config.require_oauth()

    return config


def _read_dotenv(path: Path) -> dict[str, str]:
    """Returns the key and value pairs from a ``.env`` file.

    ``python-dotenv`` is imported here rather than at module scope, to keep the package
    import-safe when the dependency is unresolved. A bare key line yields no value and
    is dropped, so the merge layer only ever holds strings.
    """
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _resolve_path(value: str) -> Path:
    """Expands a path's home marker and variables, then resolves it absolutely."""
    expanded = os.path.expanduser(os.path.expandvars(value))
    return Path(expanded).resolve()


def _opt(value: str | None) -> str | None:
    """Treats an empty string as unset, matching the shell and dotenv habit."""
    return value or None


def _tz_opt(value: str | None) -> ZoneInfo:
    """Resolves an optional IANA timezone name, falling back to the default.

    Args:
        value: The raw value, already None when unset.

    Returns:
        The resolved zone, or :data:`DEFAULT_TIMEZONE` when unset.

    Raises:
        ConfigError: when the name is unknown to the timezone database, so a typo
            fails fast at startup rather than silently mis-dating.
    """
    name = value or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"THOTH_TIMEZONE must be a valid IANA timezone name, got {name!r}"
        ) from exc


def _int_opt(value: str | None, *, default: int, name: str) -> int:
    """Parses an optional integer value, falling back to a default when unset.

    Args:
        value: The raw string value, already None when unset.
        default: The documented default used when unset.
        name: The variable name, for a clear error on a non-integer.

    Returns:
        The parsed integer, or the default.

    Raises:
        ConfigError: when the value is present but not a base-10 integer.
    """
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
