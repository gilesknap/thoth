"""Request authentication for the MCP HTTP transport (issue #103).

The ``thoth mcp --transport http`` server is a network socket, so unlike the stdio
transport (where the parent process is the trust boundary) it must authenticate every
request itself. Two tiers stack here, both enforced *before* any ``pkm_*`` tool is
dispatched:

* **Tier 1 -- static bearer (always on for HTTP).** Every request must carry
  ``Authorization: Bearer <key>``. ``<key>`` is accepted when it is either one of the
  comma-separated keys in ``THOTH_MCP_API_KEYS`` (rotation-friendly; the static-key
  match is constant-time via :func:`hmac.compare_digest` so a wrong key leaks no timing
  signal) **or** -- when OAuth 2.1 is configured (:meth:`Config.oauth_enabled`) -- a
  valid thoth-issued OAuth access-token JWT (HS256, unexpired, verified by
  :func:`thoth.mcp_oauth.verify_oauth_jwt`). The two are additive: a static key still
  works after OAuth is turned on. This is the tier Claude Code uses (a remote MCP
  client that sends a user-pasted bearer header); claude.ai obtains the JWT via the
  OAuth dance.

  When OAuth is enabled, the OAuth/discovery routes themselves
  (:data:`thoth.mcp_oauth.OAUTH_PUBLIC_PATHS`) are allow-listed so an unauthenticated
  client can reach them to *get* a token, and a 401 carries a
  ``WWW-Authenticate: Bearer resource_metadata="..."`` hint pointing at the RFC 9728
  protected-resource metadata so MCP clients can discover the authorization server.
* **Tier 2 -- Cloudflare-Access JWT (opt-in defense-in-depth).** When BOTH
  ``THOTH_MCP_CF_ACCESS_TEAM_DOMAIN`` and ``THOTH_MCP_CF_ACCESS_AUD`` are set, the
  request must ALSO carry a valid ``Cf-Access-Jwt-Assertion`` header: a JWT signed by
  the team's JWKS (``https://<team-domain>/cdn-cgi/access/certs``) whose ``aud`` matches
  the configured tag and whose ``exp`` is in the future. The algorithm is pinned to
  ``RS256`` to reject the ``none`` algorithm and RS/HS confusion. claude.ai's web/mobile
  connectors authenticate through Cloudflare-Access OAuth (a user-pasted static bearer
  is not supported by those connectors -- see ADR 0011), so the JWT is how that path
  proves the request really transited Access.

The closed-surface model (SPEC section 3) still governs *what* a caller may do once past
the door; this module only governs *who* gets through it.

The validation primitives (:func:`bearer_key_accepted`,
:func:`verify_cf_access_jwt`) are pure and unit-tested with a throwaway RSA keypair and
a stubbed JWKS. The ASGI middleware (:func:`build_auth_middleware`) wires them onto the
FastMCP app; ``pyjwt`` / ``starlette`` are imported lazily inside it so importing this
module stays CI-safe (only the standard library is needed at module top level).
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from thoth.config import Config

__all__ = [
    "AuthError",
    "bearer_key_accepted",
    "extract_bearer_token",
    "verify_cf_access_jwt",
    "build_auth_middleware",
]

# The Cloudflare-Access assertion header and the JWKS path suffix (Cf publishes the
# team signing certs at this fixed path under the team domain).
CF_ACCESS_HEADER: str = "cf-access-jwt-assertion"
CF_ACCESS_CERTS_PATH: str = "/cdn-cgi/access/certs"
# Cf-Access tokens are RS256; pinning the algorithm rejects the ``none`` algorithm and
# the RS/HS key-confusion attack (a forged HS256 token signed with the public key).
CF_ACCESS_ALGORITHMS: tuple[str, ...] = ("RS256",)


class AuthError(Exception):
    """Raised when a request fails authentication (surfaced as HTTP 401)."""


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header value.

    Returns ``None`` when the header is absent, not a ``Bearer`` scheme, or carries no
    token. The scheme match is case-insensitive (RFC 7235), the rest is byte-exact.

    Args:
        authorization_header: The raw ``Authorization`` header value, or ``None``.

    Returns:
        The bearer token string, or ``None`` when there is no usable bearer token.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def bearer_key_accepted(token: str | None, accepted_keys: Iterable[str]) -> bool:
    """Return ``True`` when ``token`` constant-time-matches one of ``accepted_keys``.

    Every candidate is compared with :func:`hmac.compare_digest` so a near-miss key
    cannot be discovered by timing. A ``None``/empty token never matches. The full set
    is always scanned (no early ``return True``) so the work is independent of *which*
    key matched -- only the boolean result varies.

    Args:
        token: The presented bearer token (``None`` when the header was missing).
        accepted_keys: The configured key set (``Config.mcp_api_key_set()``).

    Returns:
        ``True`` if the token matches an accepted key, else ``False``.
    """
    if not token:
        return False
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII *str* input,
    # and the token is fully attacker-controlled (the Authorization header). Encoding
    # both sides turns a malformed/non-ASCII token into a clean non-match (-> 401)
    # instead of an unhandled error (-> 500), while staying constant-time.
    token_bytes = token.encode("utf-8")
    matched = False
    for key in accepted_keys:
        if hmac.compare_digest(token_bytes, key.encode("utf-8")):
            matched = True
    return matched


def verify_cf_access_jwt(
    token: str | None,
    *,
    team_domain: str,
    audience: str,
    jwks_fetcher: Any | None = None,
) -> dict[str, Any]:
    """Validate a Cloudflare-Access ``Cf-Access-Jwt-Assertion`` JWT (issue #103).

    Verifies the signature against the team JWKS, pins the algorithm to ``RS256`` (so
    the ``none`` algorithm and RS/HS confusion are rejected), and checks the ``aud`` and
    ``exp`` claims. ``pyjwt`` is imported lazily so this module stays import-safe in CI.

    Args:
        token: The raw assertion header value (``None`` when missing).
        team_domain: The Cloudflare-One team domain
            (e.g. ``myteam.cloudflareaccess.com``); ``https://`` is added if absent.
        audience: The Access application's Audience (``aud``) tag.
        jwks_fetcher: Test seam -- a callable ``url -> PyJWK-compatible client`` (or a
            client exposing ``get_signing_key_from_jwt``). When ``None`` a real
            ``jwt.PyJWKClient`` is built for the team certs URL.

    Returns:
        The decoded, validated claims dict.

    Raises:
        AuthError: when the token is missing, malformed, or fails any check.
    """
    if not token:
        raise AuthError("missing Cf-Access-Jwt-Assertion header")

    import jwt  # lazy: pyjwt[crypto] is a runtime-only optional dependency

    base = team_domain if team_domain.startswith("http") else f"https://{team_domain}"
    issuer = base.rstrip("/")
    certs_url = f"{issuer}{CF_ACCESS_CERTS_PATH}"

    try:
        if jwks_fetcher is not None:
            client = jwks_fetcher(certs_url)
        else:
            client = jwt.PyJWKClient(certs_url)
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=list(CF_ACCESS_ALGORITHMS),
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "aud"]},
        )
    except AuthError:
        raise
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid Cf-Access JWT: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 401
        raise AuthError(f"Cf-Access JWT verification failed: {exc}") from exc


def build_auth_middleware(config: Config) -> Any:
    """Build a Starlette ``BaseHTTPMiddleware`` class enforcing the two auth tiers.

    The returned class rejects (HTTP 401, no tool dispatch) any request whose bearer is
    neither an accepted static ``THOTH_MCP_API_KEYS`` key nor -- when OAuth is
    configured (:meth:`Config.oauth_enabled`) -- a valid thoth-issued OAuth access-token
    JWT, and
    -- when Cf-Access is configured -- additionally rejects a request without a valid
    ``Cf-Access-Jwt-Assertion``. When OAuth is enabled the OAuth/discovery routes
    (:data:`thoth.mcp_oauth.OAUTH_PUBLIC_PATHS`) are allow-listed (they must be
    reachable without a token so a client can obtain one), and the 401 carries a
    ``resource_metadata`` discovery hint. ``starlette`` is imported here, not at module
    top level, so importing this module never needs the optional web stack.

    Args:
        config: The frozen runtime config (provides the bearer key set, the optional
            OAuth essentials, and the optional Cf-Access team domain / audience).

    Returns:
        A ``BaseHTTPMiddleware`` subclass ready to add to the FastMCP ASGI app.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    accepted_keys = config.require_mcp_api_keys()
    cf_enabled = config.mcp_cf_access_enabled()
    cf_team_domain = config.mcp_cf_access_team_domain
    cf_aud = config.mcp_cf_access_aud

    # OAuth is additive and opt-in: only when the four required vars are set does the
    # gate also accept a thoth-issued JWT, allow-list the OAuth/discovery routes, and
    # emit the RFC 9728 resource_metadata discovery hint on a 401. ``verify_oauth_jwt``
    # and the allow-list set are imported lazily so this module stays import-safe in CI
    # (mcp_oauth's top level is stdlib-only too).
    oauth_enabled = config.oauth_enabled()
    oauth_public_paths: frozenset[str] = frozenset()
    challenge = "Bearer"
    verify_oauth_jwt = None
    if oauth_enabled:
        from thoth.mcp_oauth import OAUTH_PUBLIC_PATHS, verify_oauth_jwt

        oauth_public_paths = OAUTH_PUBLIC_PATHS
        # The hint points the client at the protected-resource metadata so it can find
        # the authorization server. server_url is guaranteed non-None by oauth_enabled.
        assert config.oauth_server_url is not None
        metadata_url = (
            config.oauth_server_url.rstrip("/")
            + "/.well-known/oauth-protected-resource"
        )
        challenge = f'Bearer resource_metadata="{metadata_url}"'

    def _unauthorised(detail: str) -> Any:
        """A 401 carrying the (OAuth-aware) WWW-Authenticate discovery hint."""
        return JSONResponse(
            {"error": "invalid_token", "detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )

    class _ThothMcpAuthMiddleware(BaseHTTPMiddleware):
        """Reject unauthenticated requests with 401 before any tool is dispatched."""

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            # The OAuth/discovery routes must be reachable WITHOUT a bearer so a client
            # can complete the dance and obtain a token; let them straight through.
            if oauth_enabled and request.url.path in oauth_public_paths:
                return await call_next(request)

            token = extract_bearer_token(request.headers.get("authorization"))
            # Tier 1a: a static THOTH_MCP_API_KEYS bearer (constant-time match).
            allowed = bearer_key_accepted(token, accepted_keys)
            # Tier 1b: else a valid thoth-issued OAuth JWT (additive, opt-in). The
            # decoded ``sub`` is attached to request.state for downstream logging.
            if not allowed and verify_oauth_jwt is not None:
                try:
                    claims = verify_oauth_jwt(token, config)
                except AuthError:
                    claims = None
                if claims is not None:
                    request.state.oauth_sub = claims.get("sub")
                    allowed = True
            if not allowed:
                return _unauthorised("missing or invalid bearer")
            if cf_enabled:
                assert cf_team_domain is not None  # guaranteed by mcp_cf_access_enabled
                assert cf_aud is not None
                try:
                    verify_cf_access_jwt(
                        request.headers.get(CF_ACCESS_HEADER),
                        team_domain=cf_team_domain,
                        audience=cf_aud,
                    )
                except AuthError as exc:
                    return JSONResponse(
                        {"error": "invalid_token", "detail": str(exc)},
                        status_code=401,
                    )
            return await call_next(request)

    return _ThothMcpAuthMiddleware
