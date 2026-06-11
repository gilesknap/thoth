"""The streamable-HTTP transport: auth-gated uvicorn serving (issue #103)."""

from __future__ import annotations

import logging
from typing import Any

from thoth.config import Config

logger = logging.getLogger("thoth")


def _run_http(server: Any, config: Config, *, host: str, port: int) -> None:
    """Serve a built FastMCP over streamable-HTTP with the two-tier auth gate.

    Points the FastMCP settings at ``host``:``port``, wraps the streamable-HTTP ASGI app
    with the bearer (+ optional Cf-Access JWT) middleware
    (:func:`thoth.mcp_auth.build_auth_middleware`) so every request is authenticated
    BEFORE any tool dispatch, and serves it with uvicorn. All web-stack imports
    (``uvicorn``, ``starlette`` via the middleware) happen here, never at module top
    level, so importing this module stays CI-safe. This is exercised live, not in CI
    (the suite has no ``mcp``/``uvicorn``).

    Args:
        server: The built FastMCP instance.
        config: The frozen runtime config (bearer keys + optional Cf-Access settings).
        host: The bind address (loopback by default).
        port: The listen port.
    """
    import uvicorn

    from thoth.mcp_auth import build_auth_middleware

    oauth_enabled = config.oauth_enabled()

    # FastMCP reads host/port from its settings; set them before building the ASGI app.
    server.settings.host = host
    server.settings.port = port
    # FastMCP's streamable-HTTP transport enables DNS-rebinding protection that, by
    # default, only accepts loopback Host/Origin headers. Behind the cloudflared tunnel
    # the inbound Host is the public hostname, so without this every real connector
    # request 421s. Append any operator-configured public host(s)/origin(s) to the
    # loopback defaults (ADR 0011); the alternative is a cloudflared httpHostHeader
    # rewrite, documented in the deploy how-to.
    extra_hosts = list(config.mcp_allowed_hosts_list())
    extra_origins = list(config.mcp_allowed_origins_list())
    if oauth_enabled:
        # OAuth's issuer host must also pass the DNS-rebinding guard, otherwise the
        # discovery/authorize/token requests a connector makes against the public
        # THOTH_OAUTH_SERVER_URL would 421. Derive it from the server URL rather than
        # making the operator duplicate the host into THOTH_MCP_ALLOWED_HOSTS (§11).
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
    # Mount the OAuth 2.1 routes (discovery, register, authorize, callback, token) onto
    # the same ASGI app BEFORE the bearer gate is added: those endpoints must be
    # reachable WITHOUT a token so a connector can complete the sign-in dance. The gate
    # in mcp_auth allow-lists OAUTH_PUBLIC_PATHS and additionally accepts a thoth-issued
    # OAuth JWT in place of a static THOTH_MCP_API_KEYS bearer (additive, per the OAuth
    # plan). With no OAuth env set this is skipped and the transport stays API-key-only.
    if oauth_enabled:
        from thoth.mcp_oauth import mount_oauth_routes

        mount_oauth_routes(app, config)
    # The auth gate runs ahead of the MCP routes: a missing/invalid bearer (or, when
    # Cf-Access is configured, a missing/invalid assertion) yields 401 and the request
    # never reaches a pkm_* tool (issue #103).
    app.add_middleware(build_auth_middleware(config))
    logger.info(
        "thoth MCP serving streamable-HTTP on http://%s:%d (bearer auth%s%s)",
        host,
        port,
        ", + Cf-Access JWT" if config.mcp_cf_access_enabled() else "",
        ", + OAuth 2.1" if oauth_enabled else "",
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
