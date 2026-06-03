"""Request authentication for the MCP HTTP transport (issue #103).

The ``thoth mcp --transport http`` server is a network socket, so unlike the stdio
transport (where the parent process is the trust boundary) it must authenticate every
request itself. Two tiers stack here, both enforced *before* any ``pkm_*`` tool is
dispatched:

* **Tier 1 -- static bearer (always on for HTTP).** Every request must carry
  ``Authorization: Bearer <key>`` where ``<key>`` is one of the comma-separated keys in
  ``THOTH_MCP_API_KEYS`` (rotation-friendly). The match is constant-time
  (:func:`hmac.compare_digest`) so a wrong key leaks no timing signal. This is the tier
  Claude Code uses (a remote MCP client that sends a user-pasted bearer header).
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

    The returned class rejects (HTTP 401, no tool dispatch) any request that lacks a
    valid bearer token, and -- when Cf-Access is configured -- additionally rejects a
    request without a valid ``Cf-Access-Jwt-Assertion``. ``starlette`` is imported here,
    not at module top level, so importing this module never needs the optional web
    stack.

    Args:
        config: The frozen runtime config (provides the bearer key set and the optional
            Cf-Access team domain / audience).

    Returns:
        A ``BaseHTTPMiddleware`` subclass ready to add to the FastMCP ASGI app.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    accepted_keys = config.require_mcp_api_keys()
    cf_enabled = config.mcp_cf_access_enabled()
    cf_team_domain = config.mcp_cf_access_team_domain
    cf_aud = config.mcp_cf_access_aud

    class _ThothMcpAuthMiddleware(BaseHTTPMiddleware):
        """Reject unauthenticated requests with 401 before any tool is dispatched."""

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            token = extract_bearer_token(request.headers.get("authorization"))
            if not bearer_key_accepted(token, accepted_keys):
                return JSONResponse(
                    {"error": "invalid_token", "detail": "missing or invalid bearer"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
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
