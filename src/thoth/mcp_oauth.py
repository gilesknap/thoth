"""OAuth 2.1 and PKCE authorization for thoth's MCP HTTP server.

Additive to the static bearer (issue #103). With no OAuth settings the transport stays
API-key-only, and a request may present either a static key or a thoth-issued token.
With all four vars set, this mounts the six routes that let a remote client obtain a
per-user token by signing in with GitHub. thoth acts as two things at once:

* an authorization server to the MCP client, publishing discovery metadata (RFC 8414 and
  RFC 9728), accepting Dynamic Client Registration (RFC 7591), running the OAuth 2.1
  authorization-code flow with mandatory PKCE S256, and minting its own HS256 access
  tokens signed with ``THOTH_JWT_SIGNING_SECRET``;
* a client of GitHub, the upstream identity provider. The authenticated login becomes
  the token subject, and only logins in ``THOTH_ALLOWED_GITHUB_USERS`` get one.

The mounted routes, all relative to ``THOTH_OAUTH_SERVER_URL``:

* ``GET  /.well-known/oauth-authorization-server`` -- RFC 8414 server metadata
* ``GET  /.well-known/oauth-protected-resource``   -- RFC 9728 resource metadata
* ``POST /register``  -- Dynamic Client Registration, public clients only
* ``GET  /authorize`` -- stash the PKCE challenge, bounce the user to GitHub
* ``GET  /callback``  -- verify identity and the allow-list, issue a short-lived code
* ``POST /token``     -- verify PKCE and mint the access token

State is single-replica. The pending authorizations, issued codes and registered clients
live in module-level dicts, lost on restart and not shared across processes.

That is fine for this appliance: an in-flight sign-in just restarts and a client
re-registers. Do not reach for Redis or SQLite here.

``starlette``, ``jwt`` and ``httpx`` are imported lazily inside the functions that need
them, so importing this module needs only the standard library and stays CI-safe,
matching :mod:`thoth.mcp_auth`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from thoth.mcp_auth import AuthError

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from thoth.config import Config

__all__ = [
    "mount_oauth_routes",
    "mint_oauth_jwt",
    "verify_oauth_jwt",
    "ACCESS_TOKEN_TTL_SECONDS",
    "AUTH_CODE_TTL_SECONDS",
    "PENDING_AUTH_TTL_SECONDS",
    "REGISTERED_CLIENT_TTL_SECONDS",
    "MAX_REGISTERED_CLIENTS",
]

logger = logging.getLogger(__name__)

# Access-token lifetime of a day, so a connector survives a normal working session
# without a re-auth round-trip
ACCESS_TOKEN_TTL_SECONDS: int = 24 * 3600
# Authorization-code lifetime, short and single-use. The client redeems it immediately
# after the GitHub round-trip, and RFC 6749 recommends 10 minutes or less
AUTH_CODE_TTL_SECONDS: int = 300
# Pending-authorization lifetime, covering the GitHub round-trip. The entry is created
# at /authorize and must survive the whole interactive sign-in, consent screen and
# possibly 2FA, so it is deliberately longer than the auth-code TTL. A slow human login
# must not return to a swept entry
PENDING_AUTH_TTL_SECONDS: int = 600
# Registered-client lifetime and hard cap. /register is unauthenticated by RFC 7591
# design, so anyone reaching the public origin could otherwise grow the store without
# bound. The TTL expires stale registrations, since a real client just re-registers and
# the store is throwaway, and the cap guards a burst faster than the TTL
REGISTERED_CLIENT_TTL_SECONDS: int = 24 * 3600
MAX_REGISTERED_CLIENTS: int = 1000

# HS256, so thoth signs and verifies with the same shared secret. The Cf-Access JWTs in
# mcp_auth are RS256 and JWKS-verified, so do not conflate the two paths
_JWT_ALG: str = "HS256"

# The upstream identity provider, where thoth is a client of these endpoints
_GITHUB_AUTHORIZE_URL: str = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL: str = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL: str = "https://api.github.com/user"
# The minimal scope needed to read the authenticated user's login
_GITHUB_SCOPE: str = "read:user"

# Registration is unauthenticated, so a self-registered client could otherwise have
# /authorize redirect a code to a scheme that executes or exfiltrates it. We do not
# restrict to https: native clients legitimately use loopback http and private-use
# schemes (RFC 8252) and onboarding must stay friction-free, so only the actively
# dangerous schemes are denied
_DANGEROUS_REDIRECT_SCHEMES: frozenset[str] = frozenset(
    {"javascript", "data", "vbscript", "file"}
)


def _redirect_uri_allowed(uri: str) -> bool:
    """Rejects a redirect URI whose scheme could run code or leak the auth code."""
    scheme = urlsplit(uri).scheme.lower()
    return bool(scheme) and scheme not in _DANGEROUS_REDIRECT_SCHEMES


# thoth's own route paths, relative to the server URL. The callback is where GitHub
# redirects back
_AUTHORIZE_PATH: str = "/authorize"
_TOKEN_PATH: str = "/token"
_REGISTER_PATH: str = "/register"
_CALLBACK_PATH: str = "/callback"
_AS_METADATA_PATH: str = "/.well-known/oauth-authorization-server"
_PR_METADATA_PATH: str = "/.well-known/oauth-protected-resource"

# Paths the bearer gate must let through without a token, covering discovery and the
# whole dance. The middleware allow-lists these so the flow is reachable
OAUTH_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        _AS_METADATA_PATH,
        _PR_METADATA_PATH,
        _REGISTER_PATH,
        _AUTHORIZE_PATH,
        _CALLBACK_PATH,
        _TOKEN_PATH,
    }
)


# --------------------------------------------------------------------------- #
# In-memory stores, single-replica and lost on restart by design
# --------------------------------------------------------------------------- #


@dataclass
class _RegisteredClient:
    """One registered public PKCE client. No client secret is ever held."""

    client_id: str
    redirect_uris: list[str]
    client_name: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class _PendingAuth:
    """One in-flight authorize request, keyed by the GitHub callback state.

    Bridges the redirect out to GitHub and the callback back, carrying the client's PKCE
    challenge and original redirect and state so they can be bound to the authorization
    code thoth issues once GitHub identifies the user.
    """

    client_id: str
    redirect_uri: str  # the MCP client's redirect_uri
    client_state: str  # the MCP client's original ``state``
    code_challenge: str
    code_challenge_method: str
    scope: str
    created_at: float = field(default_factory=time.time)


@dataclass
class _AuthCode:
    """One issued, single-use authorization code, bound to its PKCE challenge."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    github_login: str
    created_at: float = field(default_factory=time.time)


_clients: dict[str, _RegisteredClient] = {}
_pending: dict[str, _PendingAuth] = {}  # github-callback state -> _PendingAuth
_auth_codes: dict[str, _AuthCode] = {}  # thoth code -> _AuthCode


def _gc(store: dict[str, Any], ttl: int) -> None:
    """Drops entries older than the TTL, as cheap opportunistic expiry."""
    now = time.time()
    for key in [k for k, v in store.items() if now - v.created_at > ttl]:
        store.pop(key, None)


# --------------------------------------------------------------------------- #
# PKCE (RFC 7636). OAuth 2.1 mandates S256 and plain is rejected
# --------------------------------------------------------------------------- #


def _b64url_no_pad(data: bytes) -> str:
    """Encodes bytes as base64url without padding (RFC 7636)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    """Reports whether a verifier matches its challenge under S256.

    Computes the unpadded base64url digest and constant-time-compares it with the stored
    challenge. Any method other than S256 is rejected outright, because OAuth 2.1
    forbids plain.
    """
    if method != "S256" or not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = _b64url_no_pad(digest)
    return hmac.compare_digest(expected, challenge)


# --------------------------------------------------------------------------- #
# JWT mint / verify (thoth's own HS256 access tokens)
# --------------------------------------------------------------------------- #


def mint_oauth_jwt(
    sub: str, config: Config, *, ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS
) -> str:
    """Mints a thoth access token for an authenticated GitHub login.

    The claim set is deliberately minimal, carrying only the subject and the times, with
    the GitHub username travelling downstream as the caller identity.

    Args:
        sub: The authenticated GitHub login, becoming the token subject.
        config: Frozen runtime config, supplying the signing secret.
        ttl_seconds: Token lifetime.

    Returns:
        The encoded JWT.
    """
    import jwt  # lazy: pyjwt is a runtime-only optional dependency

    _, _, signing_secret, _ = config.require_oauth()
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, signing_secret, algorithm=_JWT_ALG)


def verify_oauth_jwt(token: str | None, config: Config) -> dict[str, Any]:
    """Verifies a thoth-issued access token and returns its claims.

    Pins the algorithm to HS256, rejecting the none algorithm, and requires a valid
    signature and an unexpired token. The bearer gate uses this as the OAuth alternative
    to a static key.

    The subject is re-checked against the live allow-list on every call, not just at
    issue time. The allow-list is the appliance's only authorization control and there
    is no other revocation path, so dropping a user must cut off access promptly rather
    than waiting out the token lifetime.

    Args:
        token: The raw bearer token, or None when the header was missing.
        config: Frozen runtime config, supplying the signing secret and allow-list.

    Returns:
        The decoded, validated claims.

    Raises:
        AuthError: when the token is missing, malformed, expired, or carries a subject
            no longer on the allow-list.
    """
    if not token:
        raise AuthError("missing bearer token")

    import jwt  # lazy: pyjwt is a runtime-only optional dependency

    _, _, signing_secret, _ = config.require_oauth()
    try:
        claims = jwt.decode(
            token,
            signing_secret,
            algorithms=[_JWT_ALG],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid thoth OAuth JWT: {exc}") from exc

    # Bound the long-lived token by the current allow-list, not the one at mint time
    sub = claims.get("sub")
    if sub not in config.allowed_github_user_set():
        raise AuthError("thoth OAuth JWT subject is no longer allow-listed")
    return claims


# --------------------------------------------------------------------------- #
# Route handlers
# --------------------------------------------------------------------------- #


def mount_oauth_routes(app: Any, config: Config) -> None:
    """Mounts the six OAuth routes onto the existing MCP app.

    No second server is started. The routes are appended to the streamable-HTTP app's
    own router, so discovery, registration, the redirect dance and the token endpoint
    all live on the same origin as the transport. Called before the bearer middleware is
    added, and the gate allow-lists :data:`OAUTH_PUBLIC_PATHS` so these routes are
    reachable without a token.

    Args:
        app: The MCP ASGI app, a Starlette app carrying a router.
        config: Frozen runtime config, supplying the OAuth essentials and allow-list.
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    client_id, client_secret, _signing_secret, server_url = config.require_oauth()
    issuer = server_url.rstrip("/")
    allowed_users = config.allowed_github_user_set()
    github_redirect_uri = f"{issuer}{_CALLBACK_PATH}"

    def _oauth_err(error: str, detail: str, status: int = 400) -> Response:
        """Builds a local, non-redirect OAuth error response."""
        return JSONResponse({"error": error, "detail": detail}, status)

    def _client_err(detail: str, *, status: int = 400) -> Response:
        """Builds a local OAuth error, used before the redirect URI is trusted."""
        return _oauth_err("invalid_request", detail, status)

    # -- RFC 8414: authorization-server metadata ------------------------------------
    async def authorization_server_metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}{_AUTHORIZE_PATH}",
                "token_endpoint": f"{issuer}{_TOKEN_PATH}",
                "registration_endpoint": f"{issuer}{_REGISTER_PATH}",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    # -- RFC 9728: protected-resource metadata --------------------------------------
    async def protected_resource_metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "resource": f"{issuer}/mcp",
                "authorization_servers": [issuer],
                "bearer_methods_supported": ["header"],
            }
        )

    # -- RFC 7591: Dynamic Client Registration --------------------------------------
    async def register(request: Request) -> Response:
        # /register is unauthenticated, so bound the store on every call. Expire stale
        # registrations first, then refuse at the hard cap. Together these stop an
        # unauthenticated caller growing the store until OOM
        _gc(_clients, REGISTERED_CLIENT_TTL_SECONDS)
        if len(_clients) >= MAX_REGISTERED_CLIENTS:
            logger.warning(
                "OAuth DCR: registered-client cap (%d) reached; refusing registration",
                MAX_REGISTERED_CLIENTS,
            )
            return _oauth_err(
                "temporarily_unavailable",
                "client registration limit reached; retry later",
                503,
            )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any unparseable body is a client error
            return _client_err("request body must be JSON")

        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _oauth_err(
                "invalid_redirect_uri",
                "redirect_uris is required and must be a non-empty list",
            )
        if not all(_redirect_uri_allowed(str(u)) for u in redirect_uris):
            return _oauth_err(
                "invalid_redirect_uri",
                "redirect_uris must not use a code-executing scheme",
            )

        new_client_id = "thoth-" + secrets.token_urlsafe(24)
        client = _RegisteredClient(
            client_id=new_client_id,
            redirect_uris=[str(u) for u in redirect_uris],
            client_name=str(body.get("client_name", "")),
        )
        _clients[new_client_id] = client
        logger.info("OAuth DCR: registered public client %s", new_client_id)
        # A public PKCE client, so no client secret is issued
        return JSONResponse(
            {
                "client_id": new_client_id,
                "client_id_issued_at": int(client.created_at),
                "redirect_uris": client.redirect_uris,
                "client_name": client.client_name,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
            201,
        )

    # -- OAuth 2.1 authorize: stash PKCE, bounce to GitHub --------------------------
    async def authorize(request: Request) -> Response:
        _gc(_pending, PENDING_AUTH_TTL_SECONDS)
        q = request.query_params

        req_client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")
        response_type = q.get("response_type", "")
        code_challenge = q.get("code_challenge", "")
        code_challenge_method = q.get("code_challenge_method", "")
        scope = q.get("scope", "")
        client_state = q.get("state", "")

        client = _clients.get(req_client_id)
        if client is None:
            return _client_err("unknown client_id")
        # Never redirect to an unregistered URI, so render the error locally
        if redirect_uri not in client.redirect_uris:
            return _client_err("redirect_uri does not match a registered URI")

        def _redirect_err(code: str) -> Response:
            params = {"error": code}
            if client_state:
                params["state"] = client_state
            return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", 302)

        if response_type != "code":
            return _redirect_err("unsupported_response_type")
        # PKCE S256 is mandatory under OAuth 2.1
        if code_challenge_method != "S256" or not code_challenge:
            return _redirect_err("invalid_request")

        github_state = secrets.token_urlsafe(24)
        _pending[github_state] = _PendingAuth(
            client_id=req_client_id,
            redirect_uri=redirect_uri,
            client_state=client_state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
        )
        github_params = {
            "client_id": client_id,
            "redirect_uri": github_redirect_uri,
            "scope": _GITHUB_SCOPE,
            "state": github_state,
            "allow_signup": "false",
        }
        return RedirectResponse(
            f"{_GITHUB_AUTHORIZE_URL}?{urlencode(github_params)}", 302
        )

    # -- GitHub callback: identify user, allow-list, issue thoth code ---------------
    async def callback(request: Request) -> Response:
        import httpx  # lazy: httpx is a base runtime dependency

        q = request.query_params
        github_state = q.get("state", "")
        github_code = q.get("code", "")

        pending = _pending.pop(github_state, None)
        if pending is None or not github_code:
            return _client_err("unknown or expired authorization state")

        async with httpx.AsyncClient(timeout=10) as http:
            token_resp = await http.post(
                _GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": github_code,
                    "redirect_uri": github_redirect_uri,
                },
            )
            try:
                token_payload = token_resp.json()
            except ValueError:
                logger.warning(
                    "OAuth callback: GitHub token endpoint returned non-JSON (HTTP %s)",
                    token_resp.status_code,
                )
                return _oauth_err("access_denied", "GitHub code exchange failed", 403)
            github_token = token_payload.get("access_token")
            if not github_token:
                # GitHub signals a bad or expired code through an error payload rather
                # than a status, so log that, never the token, to stay diagnosable
                logger.warning(
                    "OAuth callback: GitHub code exchange returned no token (%s: %s)",
                    token_payload.get("error", "no_token"),
                    token_payload.get("error_description", ""),
                )
                return _oauth_err("access_denied", "GitHub code exchange failed", 403)
            user_resp = await http.get(
                _GITHUB_USER_URL,
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            try:
                login = (user_resp.json().get("login") or "").strip()
            except ValueError:
                logger.warning(
                    "OAuth callback: GitHub /user returned non-JSON (HTTP %s)",
                    user_resp.status_code,
                )
                login = ""

        if not login:
            return _oauth_err("access_denied", "no GitHub login", 403)
        if login not in allowed_users:
            logger.warning("OAuth callback: GitHub login %r not allow-listed", login)
            return _oauth_err("access_denied", "user not allow-listed", 403)

        thoth_code = secrets.token_urlsafe(32)
        _auth_codes[thoth_code] = _AuthCode(
            client_id=pending.client_id,
            redirect_uri=pending.redirect_uri,
            code_challenge=pending.code_challenge,
            code_challenge_method=pending.code_challenge_method,
            scope=pending.scope,
            github_login=login,
        )
        logger.info("OAuth callback: issued authorization code for %s", login)
        params = {"code": thoth_code}
        if pending.client_state:
            params["state"] = pending.client_state
        return RedirectResponse(f"{pending.redirect_uri}?{urlencode(params)}", 302)

    # -- OAuth 2.1 token: verify PKCE, mint the access-token JWT ---------------------
    async def token(request: Request) -> Response:
        _gc(_auth_codes, AUTH_CODE_TTL_SECONDS)
        form = await request.form()

        grant_type = form.get("grant_type", "")
        code = form.get("code", "")
        redirect_uri = form.get("redirect_uri", "")
        req_client_id = form.get("client_id", "")
        code_verifier = form.get("code_verifier", "")

        no_store = {"Cache-Control": "no-store"}
        if grant_type != "authorization_code":
            return JSONResponse(
                {"error": "unsupported_grant_type"}, 400, headers=no_store
            )

        # Codes are single-use, so pop unconditionally and a replay always misses
        auth = _auth_codes.pop(str(code), None)
        if (
            auth is None
            or auth.client_id != req_client_id
            or auth.redirect_uri != redirect_uri
            or not _verify_pkce(
                str(code_verifier), auth.code_challenge, auth.code_challenge_method
            )
        ):
            return JSONResponse({"error": "invalid_grant"}, 400, headers=no_store)

        access_token = mint_oauth_jwt(auth.github_login, config)
        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "scope": auth.scope,
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    routes = [
        Route(_AS_METADATA_PATH, authorization_server_metadata, methods=["GET"]),
        Route(_PR_METADATA_PATH, protected_resource_metadata, methods=["GET"]),
        Route(_REGISTER_PATH, register, methods=["POST"]),
        Route(_AUTHORIZE_PATH, authorize, methods=["GET"]),
        Route(_CALLBACK_PATH, callback, methods=["GET"]),
        Route(_TOKEN_PATH, token, methods=["POST"]),
    ]
    # Prepend so the OAuth routes win over the catch-all MCP mount on the same app
    app.router.routes[:0] = routes
    logger.info(
        "OAuth 2.1 routes mounted on the MCP app (issuer %s, %d allow-listed user(s))",
        issuer,
        len(allowed_users),
    )
