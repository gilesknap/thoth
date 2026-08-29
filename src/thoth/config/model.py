"""The frozen :class:`Config` dataclass, :class:`ConfigError`, and their helpers.

Everything here is re-exported from :mod:`thoth.config`, which owns the documented
defaults and the loader, so import from there. This module stays standard-library-only
at import time.
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
        """Absolute path to the transient state DB (``<thoth_home>/state.db``)."""
        return self.thoth_home / "state.db"

    @property
    def env_file_path(self) -> Path:
        """Absolute path to the secrets file (``<thoth_home>/.env``, chmod 600)."""
        return self.thoth_home / ".env"

    def require_anthropic(self) -> str:
        """Returns the Anthropic API key."""
        return _require(
            self.anthropic_api_key,
            "ANTHROPIC_API_KEY is required for this operation but is not set",
        )

    def require_slack(self) -> tuple[str, str]:
        """Returns the Slack bot and app tokens."""
        bot_token, app_token = _require_all(
            (
                ("SLACK_BOT_TOKEN", self.slack_bot_token),
                ("SLACK_APP_TOKEN", self.slack_app_token),
            ),
            "Slack requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN; ",
        )
        return bot_token, app_token

    def require_slack_summary_channel(self) -> str:
        """Returns the summary channel id.

        The digest (SPEC section 9) is posted here by the summary cron. It lives in
        configuration rather than as a literal, so the target is not baked into the
        code.
        """
        return _require(
            self.slack_summary_channel,
            "SLACK_SUMMARY_CHANNEL is required to post a summary but is not set",
        )

    def require_slack_capture_channel(self) -> str:
        """Returns the capture channel id.

        The Slack surface (issue #61) is one dedicated private channel holding you and
        the bot, and the daemon listens and replies only there, keying each capture to
        its own thread. There is no DM fallback, so the daemon fails fast at startup
        rather than listen nowhere.
        """
        return _require(
            self.slack_capture_channel,
            "SLACK_CAPTURE_CHANNEL is required to run the Slack daemon but is unset",
        )

    def mcp_api_key_set(self) -> frozenset[str]:
        """Parses ``THOTH_MCP_API_KEYS`` into the accepted bearer-key set (#103).

        The HTTP transport authenticates every request with a static bearer key.
        Multiple keys are comma-separated so one can be rotated without downtime: add
        the new key, let clients cut over, then drop the old one.

        Returns:
            The non-empty bearer keys, or an empty set when unconfigured, which the
            caller treats as fail-fast rather than binding an open socket.
        """
        return frozenset(_split_csv(self.mcp_api_keys))

    def require_mcp_api_keys(self) -> frozenset[str]:
        """Returns the bearer-key set, requiring at least one.

        Called when starting the HTTP transport. An unauthenticated socket must never
        bind (issue #103), so an empty setting is a fail-fast startup error rather than
        a silently open server.
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
        """Reports whether Cloudflare-Access JWT enforcement is configured.

        The Cf-Access second factor (issue #103) is opt-in and needs both the team
        domain and the audience set. With either unset the transport is bearer-only. The
        tunnel and Access still front it in production, and this only governs whether
        the origin also validates the signed assertion header.
        """
        return bool(self.mcp_cf_access_team_domain and self.mcp_cf_access_aud)

    def mcp_allowed_hosts_list(self) -> tuple[str, ...]:
        """Extra ``Host`` values to allow past the DNS-rebinding guard (#103).

        FastMCP's transport accepts only loopback hosts by default. Behind the tunnel
        the inbound host is the public hostname, so without these entries every real
        connector request would 421.

        The alternative is having cloudflared rewrite the host to loopback. The loopback
        defaults are always kept and these are appended.

        Returns:
            The extra allowed-host patterns, empty when unconfigured.
        """
        return _split_csv(self.mcp_allowed_hosts)

    def mcp_allowed_origins_list(self) -> tuple[str, ...]:
        """Extra ``Origin`` values to allow past the DNS-rebinding guard (#103).

        The companion to :meth:`mcp_allowed_hosts_list`, for the origin header, which is
        checked only when present. Set it with a scheme when a client sends an origin
        the loopback defaults reject. The defaults are always kept and these are
        appended.

        Returns:
            The extra allowed-origin patterns, empty when unconfigured.
        """
        return _split_csv(self.mcp_allowed_origins)

    def allowed_github_user_set(self) -> frozenset[str]:
        """Parses ``THOTH_ALLOWED_GITHUB_USERS`` into the OAuth allow-list.

        OAuth authenticates a user by their GitHub identity and then mints a
        thoth-signed token, and this bounds which logins may obtain one. Logins are
        comma-separated so the set can be edited without a code change.

        Returns:
            The allowed logins, or an empty set when unconfigured, which admits nobody
            until the operator populates it.
        """
        return frozenset(_split_csv(self.allowed_github_users))

    def oauth_enabled(self) -> bool:
        """Reports whether OAuth 2.1 for the MCP server is fully configured.

        OAuth is opt-in and additive to the static bearer keys, so the server still
        starts in key-only mode when its settings are absent. All four vars must be set.
        A partial configuration is a startup error rather than a silent fallback, which
        :meth:`require_oauth` enforces.
        """
        return bool(
            self.github_oauth_client_id
            and self.github_oauth_client_secret
            and self.jwt_signing_secret
            and self.oauth_server_url
        )

    def require_oauth(self) -> tuple[str, str, str, str]:
        """Returns the four OAuth essentials.

        Called when any OAuth var is present, so a half-set configuration fails fast at
        startup rather than running half-open.
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
        """Resolves where unattended error and heartbeat alerts are posted (#15).

        The dedicated alert channel wins. Failing that, the first id in the allow-list
        serves as a DM target, since an allow-listed user id doubles as a valid channel
        for a bot DM (SPEC section 10).

        Returns:
            The channel or DM id, or None when unconfigured, so the caller can no-op.
            An alert path must never itself crash the daemon or a cron job.
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
        """Builds an ``obsidian://open`` deep link for a vault-relative path.

        The path is percent-encoded in full, separators included, per the SPEC appendix,
        and so is the vault name. This does not assert the path is inside the vault,
        because the caller passes an already-validated one and disk-side confinement
        lives in the vault module.

        Raises:
            ValueError: if the path is empty or absolute.
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
    """Splits a comma-separated env value, trimming entries and dropping blanks."""
    if not raw:
        return ()
    return tuple(piece.strip() for piece in raw.split(",") if piece.strip())


def _require(value: str | None, message: str) -> str:
    """Returns a value, requiring it to be set."""
    if value is None:
        raise ConfigError(message)
    return value


def _require_all(
    pairs: tuple[tuple[str, str | None], ...], prefix: str
) -> tuple[str, ...]:
    """Returns the paired values, requiring every one to be set."""
    missing = [name for name, value in pairs if value is None]
    if missing:
        raise ConfigError(prefix + f"missing: {', '.join(missing)}")
    return tuple(value for _, value in pairs if value is not None)


def _strip_user_token(token: str) -> str:
    """Strips the mention wrappers and any label from one allow-list token.

    The single normaliser for allow-list tokens, shared with
    :func:`thoth.slack_app.parse_allowed_users`. It lives here so
    :meth:`Config.alert_target` can pull a DM id without importing the heavy, CI-absent
    Slack module.
    """
    token = token.strip()
    if token.startswith("<@") and token.endswith(">"):
        token = token[2:-1]
    if token.startswith("@"):
        token = token[1:]
    return token.split("|", 1)[0].strip()
