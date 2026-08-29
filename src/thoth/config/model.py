"""The frozen :class:`Config` dataclass, :class:`ConfigError`, and their helpers.

Import from :mod:`thoth.config` instead, the package ``__init__`` that re-exports
everything here and owns the documented defaults and :func:`thoth.config.load_config`.
This module stays standard-library-only at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable, validated thoth runtime configuration."""

    vault_path: Path
    vault_name: str
    thoth_home: Path
    timezone: ZoneInfo
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
    firecrawl_api_key: str | None
    gemini_api_key: str | None
    hindsight_base_url: str
    daily_llm_budget: int
    image_resize_threshold_bytes: int
    max_analyse_images: int
    mcp_api_keys: str | None
    mcp_cf_access_team_domain: str | None
    mcp_cf_access_aud: str | None
    mcp_allowed_hosts: str | None
    mcp_allowed_origins: str | None
    github_oauth_client_id: str | None
    github_oauth_client_secret: str | None
    jwt_signing_secret: str | None
    allowed_github_users: str | None
    oauth_server_url: str | None

    @property
    def state_db_path(self) -> Path:
        """Absolute path to the transient state database, ``<thoth_home>/state.db``."""
        return self.thoth_home / "state.db"

    @property
    def env_file_path(self) -> Path:
        """Absolute path to the secrets file, ``<thoth_home>/.env``, at chmod 600."""
        return self.thoth_home / ".env"

    def require_anthropic(self) -> str:
        """Return the Anthropic API key, or raise :class:`ConfigError` when unset."""
        return _require(
            self.anthropic_api_key,
            "ANTHROPIC_API_KEY is required for this operation but is not set",
        )

    def require_slack(self) -> tuple[str, str]:
        """Return ``(bot_token, app_token)`` or raise :class:`ConfigError`.

        Raises when either ``SLACK_BOT_TOKEN`` or ``SLACK_APP_TOKEN`` is unset.
        """
        bot_token, app_token = _require_all(
            (
                ("SLACK_BOT_TOKEN", self.slack_bot_token),
                ("SLACK_APP_TOKEN", self.slack_app_token),
            ),
            "Slack requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN; ",
        )
        return bot_token, app_token

    def require_slack_summary_channel(self) -> str:
        """Return the summary channel id, or raise :class:`ConfigError` when unset.

        The ``thoth summary`` cron entrypoint posts the daily and weekly digest (SPEC
        section 9) to this Slack channel. It lives in the ``SLACK_SUMMARY_CHANNEL``
        configuration rather than a literal, so the target is not baked into the code.
        """
        return _require(
            self.slack_summary_channel,
            "SLACK_SUMMARY_CHANNEL is required to post a summary but is not set",
        )

    def require_slack_capture_channel(self) -> str:
        """Return the capture channel id or raise :class:`ConfigError` if unset.

        The Slack surface (issue #61) is a single dedicated **private channel** holding
        you and the bot, where the daemon listens and replies only, keying each capture
        or ask to its own thread. The channel id lives in ``SLACK_CAPTURE_CHANNEL`` and
        is **required** to start ``thoth slack``, with no DM fallback after the pure
        cutover from the old ``message.im`` flow, so the daemon fails fast at startup
        rather than listens nowhere.
        """
        return _require(
            self.slack_capture_channel,
            "SLACK_CAPTURE_CHANNEL is required to run the Slack daemon but is unset",
        )

    def mcp_api_key_set(self) -> frozenset[str]:
        """Parse ``THOTH_MCP_API_KEYS`` into the accepted bearer-key set (issue #103).

        The HTTP transport, ``thoth mcp --transport http``, authenticates every request
        with a static ``Authorization: Bearer <key>`` (Tier 1). Keys are comma-separated
        so one can be rotated without downtime: add the new one, let clients cut over,
        then drop the old one. Surrounding whitespace and blank entries are dropped, and
        an unset or empty var yields the empty set, which the caller treats as "fail
        fast, never bind an unauthenticated socket".

        Returns:
            The frozenset of non-empty bearer keys, empty when unconfigured.
        """
        return frozenset(_split_csv(self.mcp_api_keys))

    def require_mcp_api_keys(self) -> frozenset[str]:
        """Return the bearer-key set, or raise :class:`ConfigError` when none are set.

        Called when starting the HTTP transport. An unauthenticated network socket must
        never bind (issue #103), so an unset or empty ``THOTH_MCP_API_KEYS`` is a
        fail-fast startup error rather than a silently-open server.
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

        The Cf-Access second factor (Tier 2 defence-in-depth, issue #103) is *opt-in*,
        enabled only when BOTH ``THOTH_MCP_CF_ACCESS_TEAM_DOMAIN`` and
        ``THOTH_MCP_CF_ACCESS_AUD`` are set. With either unset the HTTP transport is
        bearer-only. The cloudflared tunnel and Access still front it in production, and
        this flag governs only whether the origin *also* validates the signed assertion
        header.
        """
        return bool(self.mcp_cf_access_team_domain and self.mcp_cf_access_aud)

    def mcp_allowed_hosts_list(self) -> tuple[str, ...]:
        """Extra ``Host`` values to allow past FastMCP's DNS-rebinding guard (#103).

        FastMCP's streamable-HTTP transport ships DNS-rebinding protection that by
        default accepts only loopback ``Host`` headers. Behind the cloudflared tunnel
        the inbound ``Host`` is the public hostname, so without these entries every real
        connector request would 421. Set ``THOTH_MCP_ALLOWED_HOSTS`` to the public
        hosts, comma-separated, such as ``mcp.example.com``. Equivalently, have
        cloudflared rewrite the ``Host`` to loopback with ``httpHostHeader``, as the
        deploy how-to shows. The loopback defaults are always kept, and these are
        appended.

        Returns:
            The extra allowed-host patterns, empty when unconfigured.
        """
        return _split_csv(self.mcp_allowed_hosts)

    def mcp_allowed_origins_list(self) -> tuple[str, ...]:
        """Extra ``Origin`` values to allow past the DNS-rebinding guard (#103).

        Companion to :meth:`mcp_allowed_hosts_list` for the ``Origin`` header, checked
        only when present. Set ``THOTH_MCP_ALLOWED_ORIGINS``, comma-separated and with a
        scheme such as ``https://mcp.example.com``, when a client sends an ``Origin``
        the loopback defaults reject. The loopback defaults are always kept, and these
        are appended.

        Returns:
            The extra allowed-origin patterns, empty when unconfigured.
        """
        return _split_csv(self.mcp_allowed_origins)

    def allowed_github_user_set(self) -> frozenset[str]:
        """Parse ``THOTH_ALLOWED_GITHUB_USERS`` into the OAuth allow-list set.

        OAuth 2.1 for the MCP server authenticates a user by their GitHub identity, then
        mints a thoth-signed access token, and this allow-list bounds *which* GitHub
        logins may obtain one. Logins are comma-separated so the set can be edited
        without code changes. Surrounding whitespace and blank entries are dropped, and
        an unset or empty var yields the empty set, allowing no user, so the OAuth flow
        admits nobody until the operator populates it.

        Returns:
            The frozenset of allowed GitHub logins, empty when unconfigured.
        """
        return frozenset(_split_csv(self.allowed_github_users))

    def oauth_enabled(self) -> bool:
        """Return ``True`` when OAuth 2.1 for the MCP server is fully configured.

        OAuth is *opt-in* and additive to the static ``THOTH_MCP_API_KEYS`` bearer, so
        the server still starts in API-key-only mode when the OAuth environment is
        absent. It is enabled only when ALL four required vars are set:
        ``GITHUB_OAUTH_CLIENT_ID``, ``GITHUB_OAUTH_CLIENT_SECRET``,
        ``THOTH_JWT_SIGNING_SECRET`` and ``THOTH_OAUTH_SERVER_URL``. A partial
        configuration is a startup error, not a silent fallback, as
        :meth:`require_oauth` shows.
        """
        return bool(
            self.github_oauth_client_id
            and self.github_oauth_client_secret
            and self.jwt_signing_secret
            and self.oauth_server_url
        )

    def require_oauth(self) -> tuple[str, str, str, str]:
        """Return the four OAuth essentials or raise :class:`ConfigError`.

        Returns ``(client_id, client_secret, signing_secret, server_url)``. Raises when
        any of ``GITHUB_OAUTH_CLIENT_ID``, ``GITHUB_OAUTH_CLIENT_SECRET``,
        ``THOTH_JWT_SIGNING_SECRET`` or ``THOTH_OAUTH_SERVER_URL`` is unset, naming
        exactly which are missing. Called when ANY OAuth var is present, so a half-set
        configuration fails fast at startup rather than runs half-open.
        """
        client_id, client_secret, signing_secret, server_url = _require_all(
            (
                ("GITHUB_OAUTH_CLIENT_ID", self.github_oauth_client_id),
                ("GITHUB_OAUTH_CLIENT_SECRET", self.github_oauth_client_secret),
                ("THOTH_JWT_SIGNING_SECRET", self.jwt_signing_secret),
                ("THOTH_OAUTH_SERVER_URL", self.oauth_server_url),
            ),
            "OAuth requires GITHUB_OAUTH_CLIENT_ID, GITHUB_OAUTH_CLIENT_SECRET, "
            "THOTH_JWT_SIGNING_SECRET and THOTH_OAUTH_SERVER_URL; ",
        )
        return client_id, client_secret, signing_secret, server_url

    def alert_target(self) -> str | None:
        """Resolve where unattended error/heartbeat alerts are posted (issue #15).

        Resolution (SPEC section 10 observability): the dedicated
        ``SLACK_ALERT_CHANNEL`` wins, and failing that the first id parsed from the
        ``SLACK_ALLOWED_USERS`` allow-list serves as a DM target, since an allow-listed
        user id doubles as a valid ``chat.postMessage`` channel for a bot DM. With
        neither set this returns ``None`` so the caller can no-op rather than raise,
        because an alert path must never itself crash the daemon or a cron job.

        Returns:
            The Slack channel or DM id to post alerts to, or ``None`` when unconfigured.
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

        The path must be vault-relative, with no leading ``/``, and is percent-encoded
        in full including path separators, per the SPEC Appendix. The vault name is
        encoded too. This does not assert the path is inside the vault, since the caller
        passes an already-validated relative path, and disk-side confinement lives in
        ``vault.py``.

        Raises:
            ValueError: when ``vault_relative_path`` is empty or absolute.
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


def _split_csv(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated env value, trimming entries and dropping blanks."""
    if not raw:
        return ()
    return tuple(piece.strip() for piece in raw.split(",") if piece.strip())


def _require(value: str | None, message: str) -> str:
    """Return ``value``, or raise :class:`ConfigError` with ``message`` when unset."""
    if value is None:
        raise ConfigError(message)
    return value


def _require_all(
    pairs: tuple[tuple[str, str | None], ...], prefix: str
) -> tuple[str, ...]:
    """Return the paired values or raise :class:`ConfigError` naming the unset vars.

    ``prefix`` opens the error message, and the missing variable names follow in pair
    order.
    """
    missing = [name for name, value in pairs if value is None]
    if missing:
        raise ConfigError(prefix + f"missing: {', '.join(missing)}")
    return tuple(value for _, value in pairs if value is not None)


def _strip_user_token(token: str) -> str:
    """Strip ``<@...>``, a leading ``@`` or a ``|label`` from one allow-list token.

    The single normaliser for ``SLACK_ALLOWED_USERS`` tokens, shared with
    :func:`thoth.slack_app.parse_allowed_users`. It lives here so
    :meth:`Config.alert_target` can pull a DM id without importing the heavy,
    CI-absent Slack-daemon module. Kept tiny and pure.
    """
    token = token.strip()
    if token.startswith("<@") and token.endswith(">"):
        token = token[2:-1]
    if token.startswith("@"):
        token = token[1:]
    return token.split("|", 1)[0].strip()
