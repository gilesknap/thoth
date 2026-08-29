"""Request authentication for the MCP HTTP transport (issue #103).

The HTTP server is a network socket, so unlike the stdio transport, where the parent
process is the trust boundary, it must authenticate every request itself. Two tiers
stack, both enforced before any tool is dispatched.

* **Tier 1, a static bearer, always on for HTTP.** A key is accepted when it is one of
  the comma-separated keys in ``THOTH_MCP_API_KEYS``, matched in constant time so a
  wrong key leaks no timing signal, or, when OAuth is configured, a valid thoth-issued
  access token. The two are additive, so a static key still works after OAuth is turned
  on: Claude Code uses the bearer while claude.ai obtains a token through the dance.
  With OAuth enabled the discovery routes are allow-listed so an unauthenticated client
  can reach them, and a 401 carries a ``resource_metadata`` hint pointing at the RFC
  9728 metadata.
* **Tier 2, a Cloudflare-Access JWT, opt-in defence in depth.** With both Cf settings
  present the request must also carry a valid assertion header, signed by the team JWKS,
  whose audience matches and whose expiry is in the future. The algorithm is pinned to
  RS256 to reject the none algorithm and RS/HS confusion. claude.ai's connectors
  authenticate through Access OAuth rather than a pasted bearer (ADR 0011), so the JWT
  is how that path proves the request really transited Access.

The closed-surface model (SPEC section 3) still governs what a caller may do once past
the door. This module only governs who gets through it. The validation primitives are
pure and unit-tested with a throwaway keypair and a stubbed JWKS, and ``pyjwt`` and
``starlette`` are imported lazily inside the middleware, so importing this module stays
CI-safe.
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

# The Cf-Access assertion header and the JWKS path suffix. Cloudflare publishes the team
# signing certs at this fixed path under the team domain
CF_ACCESS_HEADER: str = "cf-access-jwt-assertion"
CF_ACCESS_CERTS_PATH: str = "/cdn-cgi/access/certs"
# Cf-Access tokens are RS256. Pinning the algorithm rejects the none algorithm and the
# RS/HS key-confusion attack, where a token is forged by signing HS256 with the public
# key
CF_ACCESS_ALGORITHMS: tuple[str, ...] = ("RS256",)


class AuthError(Exception):
    """Raised when a request fails authentication (surfaced as HTTP 401)."""


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Pulls the token out of an ``Authorization: Bearer`` header value.

    The scheme match is case-insensitive per RFC 7235, and the token comes back with
    its surrounding whitespace stripped.

    Args:
        authorization_header: The raw header value, or None.

    Returns:
        The bearer token, or None when the header is absent, not a bearer scheme, or
        carries no token.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def bearer_key_accepted(token: str | None, accepted_keys: Iterable[str]) -> bool:
    """Reports whether a token constant-time-matches one of the accepted keys.

    Every candidate is compared with :func:`hmac.compare_digest`, so a near-miss key
    cannot be found by timing. The full set is always scanned, with no early return, so
    the work is independent of which key matched and only the result varies.

    Args:
        token: The presented bearer token, or None when the header was missing.
        accepted_keys: The configured key set.

    Returns:
        True when the token matches an accepted key, otherwise False. An empty token
        never matches.
    """
    if not token:
        return False
    # Compare as bytes. compare_digest raises TypeError on non-ASCII str input and the
    # token is fully attacker-controlled, so encoding both sides turns a malformed token
    # into a clean 401 rather than an unhandled 500, while staying constant-time
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
    """Validates a Cloudflare-Access assertion JWT (issue #103).

    Verifies the signature against the team JWKS, pins the algorithm to RS256 so the
    none algorithm and RS/HS confusion are rejected, and checks the audience and expiry.
    ``pyjwt`` is imported lazily so this module stays import-safe in CI.

    Args:
        token: The raw assertion header value, or None when missing.
        team_domain: The Cloudflare-One team domain, with the scheme added if absent.
        audience: The Access application's audience tag.
        jwks_fetcher: Test seam taking the certs URL and returning a client exposing
            ``get_signing_key_from_jwt``. None builds a real client.

    Returns:
        The decoded, validated claims.

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
    """Builds the Starlette middleware class enforcing the two auth tiers.

    The returned class rejects with a 401, and no tool dispatch, any request whose
    bearer is neither an accepted static key nor, when OAuth is configured, a valid
    thoth-issued token. With Cf-Access configured it additionally rejects a request
    without a valid assertion.

    With OAuth enabled the discovery routes are allow-listed, since they must be
    reachable without a token for a client to obtain one, and the 401 carries a
    discovery hint.

    ``starlette`` is imported here rather than at module level, so importing this module
    never needs the optional web stack.

    Args:
        config: Frozen runtime config, supplying the bearer keys and the optional
            OAuth and Cf-Access settings.

    Returns:
        A middleware subclass ready to add to the FastMCP app.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    accepted_keys = config.require_mcp_api_keys()
    cf_enabled = config.mcp_cf_access_enabled()
    cf_team_domain = config.mcp_cf_access_team_domain
    cf_aud = config.mcp_cf_access_aud

    # OAuth is additive and opt-in. Only with all four vars set does the gate also
    # accept a thoth-issued JWT, allow-list the discovery routes, and emit the RFC 9728
    # hint on a 401. The imports are lazy so this module stays import-safe in CI
    oauth_enabled = config.oauth_enabled()
    oauth_public_paths: frozenset[str] = frozenset()
    challenge = "Bearer"
    verify_oauth_jwt = None
    if oauth_enabled:
        from thoth.mcp_oauth import OAUTH_PUBLIC_PATHS, verify_oauth_jwt

        oauth_public_paths = OAUTH_PUBLIC_PATHS
        # The hint points the client at the protected-resource metadata so it can find
        # the authorization server. oauth_enabled guarantees server_url is set
        assert config.oauth_server_url is not None
        metadata_url = (
            config.oauth_server_url.rstrip("/")
            + "/.well-known/oauth-protected-resource"
        )
        challenge = f'Bearer resource_metadata="{metadata_url}"'

    def _unauthorised(detail: str) -> Any:
        """Builds a 401 carrying the OAuth-aware discovery hint."""
        return JSONResponse(
            {"error": "invalid_token", "detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )

    class _ThothMcpAuthMiddleware(BaseHTTPMiddleware):
        """Rejects unauthenticated requests with a 401 before any tool is dispatched."""

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            # The discovery routes must be reachable without a bearer so a client can
            # complete the dance and obtain a token, so let them straight through
            if oauth_enabled and request.url.path in oauth_public_paths:
                return await call_next(request)

            token = extract_bearer_token(request.headers.get("authorization"))
            # Tier 1a, a static bearer key matched in constant time
            allowed = bearer_key_accepted(token, accepted_keys)
            # Tier 1b, else a valid thoth-issued OAuth JWT, which is additive and
            # opt-in. The decoded sub rides on request.state for downstream logging
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
