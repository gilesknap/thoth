"""Load and validate thoth's runtime configuration.

This package is the Phase-0 foundation that every later module imports. It is the
single source of truth for the environment-variable names, documented defaults, the
resolved vault path, the ``~/.thoth`` home locations, and the ``obsidian://`` deep-link
format. No other module re-reads :data:`os.environ` for these values; callers invoke
:func:`load_config` once (typically near process entry) and pass the frozen
:class:`Config` down. This keeps the closed-surface promise auditable in one place.

Configuration is read from environment variables, optionally seeded from a ``.env``
file via ``python-dotenv``. Seeding is non-mutating: file values only fill gaps that the
real environment does not already provide, and :data:`os.environ` is never written to.

The package imports only the standard library at top level; ``python-dotenv`` is
imported lazily inside :func:`_read_dotenv` so the package stays import-safe even when
the dependency is not resolved (for example during doctest collection on a bare
checkout).

Documented defaults (the single source of truth):

* ``OBSIDIAN_VAULT_NAME`` defaults to :data:`DEFAULT_OBSIDIAN_VAULT_NAME`
  (``pkm-vault``).
* ``THOTH_HOME`` defaults to :data:`DEFAULT_THOTH_HOME` (``~/.thoth``).
* ``THOTH_TIMEZONE`` defaults to :data:`DEFAULT_TIMEZONE` (``Europe/London``) -- the
  IANA timezone for every calendar-date computation (day boundary, schedules, lint
  freshness, and the curate relative-date resolution). A bogus name fails fast.
* ``ANTHROPIC_MODEL`` defaults to :data:`DEFAULT_ANTHROPIC_MODEL`
  (``claude-sonnet-4-6``).
* ``THOTH_ANALYSE_MODEL`` defaults to ``None`` -- the folded analyse/kind/transcription
  vision call (issue #68) then resolves to :data:`DEFAULT_ANTHROPIC_MODEL` via the LLM.
  Set it to drop the analyse call to a cheaper model (a Haiku) for document A/B work
  without changing the default model used everywhere else.
* ``THOTH_DIAGRAM_MODEL`` defaults to ``None`` -- the Excalidraw reconstruction call
  (issue #68, hand-drawn diagram -> editable scene) then resolves to
  :data:`DEFAULT_ANTHROPIC_MODEL` via the LLM. That call needs spatial reasoning plus
  valid JSON, so it is worth pinning to a stronger model (Sonnet/Opus) independently.
* ``THOTH_INTENT_MODEL`` defaults to ``None`` -- the free-text intent gate (issue #5)
  then falls back to :data:`thoth.intent.DEFAULT_INTENT_MODEL` (a cheap Haiku). The gate
  is a one-shot routing call, so a cheap model is the point; override it to re-tier the
  gate without a redeploy.
* ``THOTH_HINDSIGHT_BASE_URL`` defaults to :data:`DEFAULT_HINDSIGHT_BASE_URL`
  (``http://127.0.0.1:8888``) -- the standalone ``hindsight-api`` server the
  :mod:`thoth.hindsight` HTTP client talks to.
* ``THOTH_LOG_LEVEL`` defaults to :data:`DEFAULT_LOG_LEVEL` (``INFO``); the daemon
  entrypoint passes it to :func:`logging.basicConfig` so the appliance is no longer
  silent on the happy path (issue #52).
* ``SLACK_ALERT_CHANNEL`` is the unattended error/heartbeat alert target (issue #15);
  when unset, :meth:`Config.alert_target` falls back to the first
  ``SLACK_ALLOWED_USERS`` id as a DM target.
* ``SLACK_CAPTURE_CHANNEL`` is the dedicated private channel the Slack daemon listens
  and replies in (issue #61); it is required to start ``thoth slack`` (a pure cutover
  from the old ``message.im`` DM flow, no DM fallback) and is read via
  :meth:`Config.require_slack_capture_channel`.

Only ``PKM_VAULT`` is hard-required in Phase 0 (see :data:`REQUIRED_VARS`).
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

    # OAuth 2.1 for the MCP server is additive and opt-in: with no OAuth env at all
    # the server starts in API-key-only mode (``oauth_enabled()`` is False). But a
    # *partial* configuration is a foot-gun -- it would run half-open -- so if ANY OAuth
    # var is set we require the full required set, failing fast at startup and naming
    # what is missing. ``THOTH_ALLOWED_GITHUB_USERS`` counts as "OAuth env present" even
    # though it is not one of the four required vars.
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
    """Return key/value pairs from a .env file, ``{}`` if it is absent.

    ``python-dotenv`` is imported here (not at module scope) to keep the package
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


def _tz_opt(value: str | None) -> ZoneInfo:
    """Resolve an optional IANA timezone name, falling back to :data:`DEFAULT_TIMEZONE`.

    Args:
        value: The raw ``THOTH_TIMEZONE`` value (already ``None`` when unset/blank).

    Returns:
        The resolved :class:`zoneinfo.ZoneInfo` (the owner's locale when unset).

    Raises:
        ConfigError: when ``value`` names a timezone the ``tzdata`` database does not
            know, so a typo fails fast at startup rather than silently mis-dating.
    """
    name = value or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"THOTH_TIMEZONE must be a valid IANA timezone name, got {name!r}"
        ) from exc


def _int_opt(value: str | None, *, default: int, name: str) -> int:
    """Parse an optional integer env value, falling back to ``default`` when unset.

    Args:
        value: The raw string value (already ``None`` when unset/blank via ``lookup``).
        default: The documented default to use when ``value`` is ``None``.
        name: The variable name, for a clear :class:`ConfigError` on a non-integer.

    Returns:
        The parsed integer, or ``default`` when unset.

    Raises:
        ConfigError: when ``value`` is present but not a base-10 integer.
    """
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
