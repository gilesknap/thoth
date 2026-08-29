"""The streamable-HTTP transport: auth-gated uvicorn serving (issue #103)."""

from __future__ import annotations

import logging
from typing import Any

from thoth.config import Config

logger = logging.getLogger("thoth")


def _run_http(server: Any, config: Config, *, host: str, port: int) -> None:
    """Serves a built FastMCP over streamable-HTTP with the two-tier auth gate.

    Points the FastMCP settings at ``host``:``port``, wraps the streamable-HTTP ASGI app
    with the bearer and optional Cf-Access middleware
    (:func:`thoth.mcp_auth.build_auth_middleware`) so every request is authenticated
    before any tool dispatch, then serves it with uvicorn. Every web-stack import
    happens inside this function rather than at module level, so importing the module
    stays CI-safe. This path is exercised live rather than in CI, since the suite has no
    ``mcp`` or ``uvicorn``.

    Args:
        server: The built FastMCP instance
        config: The frozen runtime config, bearer keys plus Cf-Access settings
        host: The bind address, loopback by default
        port: The listen port
    """
    import uvicorn

    from thoth.mcp_auth import build_auth_middleware

    oauth_enabled = config.oauth_enabled()

    # FastMCP reads host/port from its settings, so set them before the ASGI app
    server.settings.host = host
    server.settings.port = port
    # FastMCP's DNS-rebinding protection accepts only loopback Host/Origin headers, and
    # behind the cloudflared tunnel the inbound Host is the public hostname, so without
    # the operator-configured hosts appended here every real connector request 421s. The
    # alternative is a cloudflared httpHostHeader rewrite (ADR 0011, deploy how-to)
    extra_hosts = list(config.mcp_allowed_hosts_list())
    extra_origins = list(config.mcp_allowed_origins_list())
    if oauth_enabled:
        # The issuer host must pass the same guard or the discovery, authorize and
        # token requests a connector makes would 421, so derive it from the server URL
        # rather than making the operator duplicate the host into
        # THOTH_MCP_ALLOWED_HOSTS (SPEC section 11)
        from urllib.parse import urlsplit

        parts = urlsplit(config.oauth_server_url or "")
        if parts.hostname:
            if parts.hostname not in extra_hosts:
                extra_hosts.append(parts.hostname)
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in extra_origins:
                extra_origins.append(origin)
    if extra_hosts or extra_origins:
        sec = server.settings.transport_security
        if sec is None:  # pragma: no cover - FastMCP always provides defaults
            from mcp.server.transport_security import TransportSecuritySettings

            sec = TransportSecuritySettings()
            server.settings.transport_security = sec
        sec.allowed_hosts = [*sec.allowed_hosts, *extra_hosts]
        sec.allowed_origins = [*sec.allowed_origins, *extra_origins]
    app = server.streamable_http_app()
    # The OAuth 2.1 routes mount before the bearer gate is added, because a connector
    # must reach discovery, register, authorize, callback and token WITHOUT a token to
    # complete the sign-in dance. The gate allow-lists OAUTH_PUBLIC_PATHS and also
    # accepts a thoth-issued JWT in place of a static THOTH_MCP_API_KEYS bearer. With no
    # OAuth env set this is skipped and the transport stays API-key-only
    if oauth_enabled:
        from thoth.mcp_oauth import mount_oauth_routes

        mount_oauth_routes(app, config)
    # The gate runs ahead of the MCP routes, so a missing or invalid bearer (or
    # Cf-Access assertion) yields 401 and never reaches a pkm_* tool (issue #103)
    app.add_middleware(build_auth_middleware(config))
    logger.info(
        "thoth MCP serving streamable-HTTP on http://%s:%d (bearer auth%s%s)",
        host,
        port,
        ", + Cf-Access JWT" if config.mcp_cf_access_enabled() else "",
        ", + OAuth 2.1" if oauth_enabled else "",
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
