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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_OBSIDIAN_VAULT_NAME: str = "pkm-vault"
"""Default registered Obsidian vault name used in ``obsidian://`` links."""

DEFAULT_ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
"""Default Anthropic model id (the dated fallback id belongs to ``llm.py``)."""

DEFAULT_LOG_LEVEL: str = "INFO"
"""Default logging level (issue #52); ``THOTH_LOG_LEVEL`` overrides it at the daemon.

Honoured once at process start by :func:`logging.basicConfig` in the daemon entrypoint,
so the concise per-operation success lines (ingest/query/research/intent) are visible
without code changes; set ``THOTH_LOG_LEVEL=DEBUG`` for more, ``WARNING`` for less.
"""

DEFAULT_THOTH_HOME: Path = Path.home() / ".thoth"
"""Default ``~/.thoth`` home, computed at import time (tests monkeypatch ``HOME``)."""

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


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable, validated thoth runtime configuration."""

    vault_path: Path
    vault_name: str
    thoth_home: Path
    log_level: str
    anthropic_api_key: str | None
    anthropic_model: str
    analyse_model: str | None
    diagram_model: str | None
    intent_model: str | None
    slack_bot_token: str | None
    slack_app_token: str | None
    slack_summary_channel: str | None
    slack_alert_channel: str | None
    slack_allowed_users: str | None
    slack_capture_channel: str | None
    exa_api_key: str | None
    firecrawl_api_key: str | None
    gemini_api_key: str | None
    daily_llm_budget: int
    image_resize_threshold_bytes: int
    max_analyse_images: int
    mcp_api_keys: str | None
    mcp_cf_access_team_domain: str | None
    mcp_cf_access_aud: str | None

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

    def require_slack_capture_channel(self) -> str:
        """Return the capture channel id or raise :class:`ConfigError` if unset.

        The Slack surface (issue #61) is a single dedicated **private channel** (you
        plus the bot): the daemon listens and replies only there, keying each
        capture/ask to its own thread. The channel id lives in ``SLACK_CAPTURE_CHANNEL``
        and is **required** to start ``thoth slack`` -- there is no DM fallback (a pure
        cutover from the old ``message.im`` flow), so the daemon fails fast at startup
        rather than listen nowhere.
        """
        if self.slack_capture_channel is None:
            raise ConfigError(
                "SLACK_CAPTURE_CHANNEL is required to run the Slack daemon but is unset"
            )
        return self.slack_capture_channel

    def mcp_api_key_set(self) -> frozenset[str]:
        """Parse ``THOTH_MCP_API_KEYS`` into the accepted bearer-key set (issue #103).

        The HTTP transport (``thoth mcp --transport http``) authenticates every request
        with a static ``Authorization: Bearer <key>`` (Tier 1). Multiple keys are
        comma-separated so a key can be rotated without downtime (add the new one, let
        clients cut over, then drop the old one). Surrounding whitespace and blank
        entries are dropped; an unset/empty var yields the empty set, which the caller
        treats as "fail fast, never bind an unauthenticated socket".

        Returns:
            The frozenset of non-empty bearer keys (empty when unconfigured).
        """
        raw = self.mcp_api_keys
        if not raw:
            return frozenset()
        return frozenset(key.strip() for key in raw.split(",") if key.strip())

    def require_mcp_api_keys(self) -> frozenset[str]:
        """Return the bearer-key set or raise :class:`ConfigError` if none are set.

        Called when starting the HTTP transport: an unauthenticated network socket must
        never bind (issue #103), so an unset/empty ``THOTH_MCP_API_KEYS`` is a fail-fast
        startup error rather than a silently-open server.
        """
        keys = self.mcp_api_key_set()
        if not keys:
            raise ConfigError(
                "THOTH_MCP_API_KEYS is required for the MCP HTTP transport (at least "
                "one bearer key) but is unset; refusing to bind an unauthenticated "
                "socket"
            )
        return keys

    def mcp_cf_access_enabled(self) -> bool:
        """Return ``True`` when Cloudflare-Access JWT enforcement is configured.

        The Cf-Access second factor (Tier 2 defense-in-depth, issue #103) is *opt-in*:
        it is enabled only when BOTH ``THOTH_MCP_CF_ACCESS_TEAM_DOMAIN`` and
        ``THOTH_MCP_CF_ACCESS_AUD`` are set. With either unset the HTTP transport is
        bearer-only (the cloudflared tunnel + Access still front it in production; this
        flag governs whether the origin *also* validates the signed assertion header).
        """
        return bool(self.mcp_cf_access_team_domain and self.mcp_cf_access_aud)

    def alert_target(self) -> str | None:
        """Resolve where unattended error/heartbeat alerts are posted (issue #15).

        Resolution (SPEC section 10 observability): the dedicated
        ``SLACK_ALERT_CHANNEL`` wins; failing that, the first id parsed from the
        allow-list (``SLACK_ALLOWED_USERS``) is used as a DM target (an allow-listed
        user id doubles as a valid ``chat.postMessage`` channel for a bot DM). When
        neither is set this returns ``None`` so the caller can no-op rather than
        raise -- an alert path must never itself crash the daemon or a cron job.

        Returns:
            The Slack channel / DM id to post alerts to, or ``None`` when unconfigured.
        """
        if self.slack_alert_channel is not None:
            return self.slack_alert_channel
        raw = self.slack_allowed_users
        if not raw:
            return None
        for piece in raw.replace(",", " ").split():
            token = _strip_user_token(piece)
            if token:
                return token
        return None

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
        exa_api_key=lookup("EXA_API_KEY"),
        firecrawl_api_key=lookup("FIRECRAWL_API_KEY"),
        gemini_api_key=lookup("GEMINI_API_KEY"),
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


def _strip_user_token(token: str) -> str:
    """Strip ``<@...>`` / leading ``@`` / a ``|label`` from one allow-list token.

    Mirrors ``slack_app._strip_user_wrapper`` so :meth:`Config.alert_target` can pull a
    DM id out of ``SLACK_ALLOWED_USERS`` without importing the (heavy, CI-absent)
    Slack-daemon module. Kept tiny and pure.
    """
    token = token.strip()
    if token.startswith("<@") and token.endswith(">"):
        token = token[2:-1]
    if token.startswith("@"):
        token = token[1:]
    return token.split("|", 1)[0].strip()
