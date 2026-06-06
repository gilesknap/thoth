"""Tests for the OAuth 2.1 + PKCE layer of the MCP HTTP server (plan §6).

These cover the OAuth surface added by :mod:`thoth.mcp_oauth` -- the routes thoth mounts
when the four required OAuth vars are set (:meth:`Config.oauth_enabled`) so a remote MCP
client can obtain a per-user access token by signing in with GitHub:

* discovery -- the two ``.well-known`` metadata endpoints return the RFC 8414 / RFC 9728
  JSON shapes;
* registration -- ``POST /register`` mints a fresh ``client_id`` (RFC 7591 DCR);
* authorize -- ``GET /authorize`` stashes the PKCE challenge + state under a fresh
  GitHub-callback ``state`` and 302s to GitHub carrying that state and the read scope;
* callback -- ``GET /callback`` rejects a non-allow-listed GitHub login (403) and, for
  an allow-listed one, issues a single-use authorization code;
* token -- ``POST /token`` rejects a mismatched PKCE verifier (``invalid_grant``) and,
  for a matching verifier, returns an HS256 access-token JWT with ``{sub, iat, exp}``.

The OAuth routes are exercised through a *real* Starlette app driven over
:class:`httpx.ASGITransport` (the handlers are closures, so they cannot be called
directly, and ASGITransport avoids the deprecated ``TestClient``); GitHub's token/user
HTTP calls are intercepted with :class:`httpx.MockTransport` (no new dependency, no real
network). The JWT is verified in-test with the same ``import jwt`` and the shared
signing secret.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import thoth.mcp_oauth as mcp_oauth
from thoth.config import Config, load_config
from thoth.mcp_oauth import (
    ACCESS_TOKEN_TTL_SECONDS,
    AUTH_CODE_TTL_SECONDS,
    mount_oauth_routes,
)

# Starlette's ``request.form()`` (used by POST /token) requires python-multipart even
# for urlencoded bodies; it ships transitively with the ``runtime`` extra (the GATE /
# live env) but may be absent on a bare dev checkout. Skip the form-parsing token tests
# gracefully when it is missing, mirroring the ``requires_dotenv`` guard in test_config.
HAVE_MULTIPART = importlib.util.find_spec("multipart") is not None
requires_multipart = pytest.mark.skipif(
    not HAVE_MULTIPART,
    reason="python-multipart not installed (install the runtime extra)",
)

# --- shared fixtures: a seeded vault + OAuth-enabled config -------------------------

_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "notes",
    "memories",
    "actions",
    "inbox",
)

# Obviously-fake placeholders only (gitleaks scans the commit). The signing secret is
# padded past 32 bytes so PyJWT's HS256 key-length check stays quiet (filterwarnings=
# error would otherwise escalate its InsecureKeyLengthWarning).
SIGNING_SECRET = "test-signing-secret-" + "x" * 32
CLIENT_ID = "test-github-client-id"
CLIENT_SECRET = "test-github-client-secret"
SERVER_URL = "https://mcp.example.com"
ALLOWED_USER = "octocat"


def _seed_vault(root: Path) -> None:
    """Write the minimal folder skeleton + spine the Vault facade expects."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")


def _oauth_config(tmp_path: Path, **extra: str) -> Config:
    """Build an OAuth-enabled Config over a seeded tmp vault."""
    _seed_vault(tmp_path)
    env = {
        "PKM_VAULT": str(tmp_path),
        "GITHUB_OAUTH_CLIENT_ID": CLIENT_ID,
        "GITHUB_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
        "THOTH_JWT_SIGNING_SECRET": SIGNING_SECRET,
        "THOTH_OAUTH_SERVER_URL": SERVER_URL,
        "THOTH_ALLOWED_GITHUB_USERS": ALLOWED_USER,
        **extra,
    }
    return load_config(env)


@pytest.fixture(autouse=True)
def _clear_oauth_stores() -> Any:
    """Reset the module-level in-memory stores between tests (single-replica state)."""
    mcp_oauth._clients.clear()
    mcp_oauth._pending.clear()
    mcp_oauth._auth_codes.clear()
    yield
    mcp_oauth._clients.clear()
    mcp_oauth._pending.clear()
    mcp_oauth._auth_codes.clear()


class _Driver:
    """Drives the mounted OAuth routes over an in-process ASGI httpx transport.

    The OAuth handlers are closures created inside :func:`mount_oauth_routes`, so they
    are exercised end to end through a real Starlette router, not called directly.
    ``follow_redirects`` defaults off so the 302s to GitHub / back to the client can be
    asserted. The sync wrappers drive the coroutine with :func:`asyncio.run` (matching
    the sibling auth-middleware tests) so the test bodies stay flat.
    """

    def __init__(self, config: Config) -> None:
        from starlette.applications import Starlette

        app = Starlette()
        mount_oauth_routes(app, config)
        self._transport = httpx.ASGITransport(app=app)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=self._transport, base_url="http://thoth.test"
        ) as http:
            return await http.request(method, path, follow_redirects=False, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return asyncio.run(self._request("GET", path, **kwargs))

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return asyncio.run(self._request("POST", path, **kwargs))


def _client(config: Config) -> _Driver:
    """Mount the OAuth routes on a real Starlette app and return an ASGI driver."""
    return _Driver(config)


def _pkce_pair() -> tuple[str, str]:
    """Return a (verifier, S256-challenge) PKCE pair (RFC 7636)."""
    verifier = "a" * 64  # high-entropy enough for the test; fixed for determinism
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _register_client(client: Any, redirect_uri: str = "https://app.example/cb") -> str:
    """Register a public client via DCR and return its issued client_id."""
    resp = client.post("/register", json={"redirect_uris": [redirect_uri]})
    assert resp.status_code == 201
    return resp.json()["client_id"]


def _github_mock_transport(login: str | None) -> httpx.MockTransport:
    """A MockTransport answering GitHub's token + user endpoints.

    ``login`` is the GitHub username returned by ``/user`` (``None`` -> empty login).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_test"})
        if request.url.host == "api.github.com" and request.url.path == "/user":
            return httpx.Response(200, json={"login": login})
        return httpx.Response(404, json={"error": "unexpected"})  # pragma: no cover

    return httpx.MockTransport(handler)


def _patch_github(monkeypatch: pytest.MonkeyPatch, login: str | None) -> None:
    """Route the callback's outbound httpx calls at a GitHub MockTransport.

    The callback constructs ``httpx.AsyncClient(timeout=10)`` with no transport, so the
    factory injects the GitHub mock only in that case, leaving a caller-supplied
    transport (the test driver's ASGI transport) untouched.
    """
    real_async_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("transport", _github_mock_transport(login))
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


# --- discovery metadata (RFC 8414 + RFC 9728) --------------------------------------


def test_authorization_server_metadata_shape(tmp_path: Path) -> None:
    """The RFC 8414 AS metadata exposes the endpoints + PKCE/S256 capabilities."""
    client = _client(_oauth_config(tmp_path))
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert body["issuer"] == SERVER_URL
    assert body["authorization_endpoint"] == f"{SERVER_URL}/authorize"
    assert body["token_endpoint"] == f"{SERVER_URL}/token"
    assert body["registration_endpoint"] == f"{SERVER_URL}/register"
    assert body["response_types_supported"] == ["code"]
    assert body["grant_types_supported"] == ["authorization_code"]
    # OAuth 2.1 mandates S256; the AS advertises only that (no ``plain``).
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]


def test_protected_resource_metadata_shape(tmp_path: Path) -> None:
    """The RFC 9728 resource metadata points at the issuer as the AS for /mcp."""
    client = _client(_oauth_config(tmp_path))
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == f"{SERVER_URL}/mcp"
    assert body["authorization_servers"] == [SERVER_URL]
    assert body["bearer_methods_supported"] == ["header"]


# --- Dynamic Client Registration (RFC 7591) ----------------------------------------


def test_register_returns_generated_client_id(tmp_path: Path) -> None:
    """POST /register mints a fresh, namespaced client_id for a public PKCE client."""
    client = _client(_oauth_config(tmp_path))
    resp = client.post(
        "/register",
        json={"redirect_uris": ["https://app.example/cb"], "client_name": "demo"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"].startswith("thoth-")
    assert body["redirect_uris"] == ["https://app.example/cb"]
    assert body["client_name"] == "demo"
    # Public client: no secret, PKCE only.
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body
    # The client is now in the store under that id.
    assert body["client_id"] in mcp_oauth._clients


def test_register_rejects_missing_redirect_uris(tmp_path: Path) -> None:
    """A registration with no redirect_uris is an invalid_redirect_uri error."""
    client = _client(_oauth_config(tmp_path))
    resp = client.post("/register", json={"client_name": "demo"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_register_refuses_once_client_cap_reached(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """/register is unauthenticated, so it caps ``_clients`` to bound memory (DoS)."""
    monkeypatch.setattr(mcp_oauth, "MAX_REGISTERED_CLIENTS", 2)
    client = _client(_oauth_config(tmp_path))
    body = {"redirect_uris": ["https://app.example/cb"]}
    assert client.post("/register", json=body).status_code == 201
    assert client.post("/register", json=body).status_code == 201
    # Third registration hits the cap and is refused (no unbounded growth).
    over = client.post("/register", json=body)
    assert over.status_code == 503
    assert over.json()["error"] == "temporarily_unavailable"
    assert len(mcp_oauth._clients) == 2


def test_register_gc_expires_stale_clients(tmp_path: Path, monkeypatch: Any) -> None:
    """A registration older than the TTL is GC'd, freeing room under the cap."""
    monkeypatch.setattr(mcp_oauth, "REGISTERED_CLIENT_TTL_SECONDS", 0)
    client = _client(_oauth_config(tmp_path))
    body = {"redirect_uris": ["https://app.example/cb"]}
    first = client.post("/register", json=body).json()["client_id"]
    # TTL of 0 means the prior entry is already stale on the next call -> dropped.
    client.post("/register", json=body)
    assert first not in mcp_oauth._clients
    assert len(mcp_oauth._clients) == 1


# --- authorize: stash PKCE + bounce to GitHub --------------------------------------


def test_authorize_stashes_pending_and_redirects_to_github(tmp_path: Path) -> None:
    """GET /authorize stores the PKCE challenge/state and 302s to GitHub with state."""
    config = _oauth_config(tmp_path)
    client = _client(config)
    redirect_uri = "https://app.example/cb"
    reg_client_id = _register_client(client, redirect_uri)
    _verifier, challenge = _pkce_pair()

    resp = client.get(
        "/authorize",
        params={
            "client_id": reg_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "read",
            "state": "client-state-xyz",
        },
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize")
    parts = urlsplit(location)
    qs = parse_qs(parts.query)
    # GitHub is asked for thoth's GitHub client id + the read scope + a fresh state.
    assert qs["client_id"] == [CLIENT_ID]
    assert qs["scope"] == [mcp_oauth._GITHUB_SCOPE]
    assert qs["redirect_uri"] == [f"{SERVER_URL}/callback"]
    github_state = qs["state"][0]

    # The pending authorization is stashed under the GitHub-callback state, carrying the
    # client's PKCE challenge and original state for later binding.
    assert github_state in mcp_oauth._pending
    pending = mcp_oauth._pending[github_state]
    assert pending.code_challenge == challenge
    assert pending.code_challenge_method == "S256"
    assert pending.client_state == "client-state-xyz"
    assert pending.redirect_uri == redirect_uri


def test_authorize_rejects_unknown_client(tmp_path: Path) -> None:
    """An unregistered client_id is a local invalid_request (no open redirect)."""
    client = _client(_oauth_config(tmp_path))
    _verifier, challenge = _pkce_pair()
    resp = client.get(
        "/authorize",
        params={
            "client_id": "never-registered",
            "redirect_uri": "https://app.example/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# --- callback: GitHub identity + allow-list ----------------------------------------


def _seed_pending(config: Config, *, challenge: str) -> str:
    """Insert a pending authorization directly and return its GitHub-callback state."""
    github_state = "gh-state-fixed"
    mcp_oauth._pending[github_state] = mcp_oauth._PendingAuth(
        client_id="thoth-test-client",
        redirect_uri="https://app.example/cb",
        client_state="client-state-xyz",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="read",
    )
    return github_state


def test_callback_rejects_non_allowlisted_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GitHub login not in THOTH_ALLOWED_GITHUB_USERS is 403'd, no code issued."""
    config = _oauth_config(tmp_path)
    client = _client(config)
    _verifier, challenge = _pkce_pair()
    github_state = _seed_pending(config, challenge=challenge)
    _patch_github(monkeypatch, login="intruder")

    resp = client.get("/callback", params={"state": github_state, "code": "gh-code"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "access_denied"
    # No authorization code was issued for a rejected user.
    assert mcp_oauth._auth_codes == {}


def test_callback_accepts_allowlisted_user_and_issues_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allow-listed login redirects back to the client with a single-use code."""
    config = _oauth_config(tmp_path)
    client = _client(config)
    _verifier, challenge = _pkce_pair()
    github_state = _seed_pending(config, challenge=challenge)
    _patch_github(monkeypatch, login=ALLOWED_USER)

    resp = client.get("/callback", params={"state": github_state, "code": "gh-code"})
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://app.example/cb")
    qs = parse_qs(urlsplit(location).query)
    # The client's original state is echoed back, alongside a fresh thoth code.
    assert qs["state"] == ["client-state-xyz"]
    thoth_code = qs["code"][0]
    assert thoth_code in mcp_oauth._auth_codes
    issued = mcp_oauth._auth_codes[thoth_code]
    assert issued.github_login == ALLOWED_USER
    assert issued.code_challenge == challenge
    # The pending entry was consumed (single use).
    assert github_state not in mcp_oauth._pending


# --- token: PKCE verification + JWT mint -------------------------------------------


def _seed_auth_code(config: Config, *, challenge: str) -> str:
    """Insert an issued authorization code directly and return the code string."""
    code = "thoth-auth-code-fixed"
    mcp_oauth._auth_codes[code] = mcp_oauth._AuthCode(
        client_id="thoth-test-client",
        redirect_uri="https://app.example/cb",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="read",
        github_login=ALLOWED_USER,
    )
    return code


@requires_multipart
def test_token_rejects_mismatched_pkce_verifier(tmp_path: Path) -> None:
    """A code_verifier not matching the stored S256 challenge -> invalid_grant."""
    config = _oauth_config(tmp_path)
    client = _client(config)
    _verifier, challenge = _pkce_pair()
    code = _seed_auth_code(config, challenge=challenge)

    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://app.example/cb",
            "client_id": "thoth-test-client",
            "code_verifier": "the-wrong-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    # The code is single-use: even a failed redemption consumes it (replay misses).
    assert code not in mcp_oauth._auth_codes


@requires_multipart
def test_token_matching_verifier_returns_decodable_hs256_jwt(tmp_path: Path) -> None:
    """A matching verifier mints an HS256 JWT with {sub, iat, exp}, decodable by us."""
    import jwt

    config = _oauth_config(tmp_path)
    client = _client(config)
    verifier, challenge = _pkce_pair()
    code = _seed_auth_code(config, challenge=challenge)

    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://app.example/cb",
            "client_id": "thoth-test-client",
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == ACCESS_TOKEN_TTL_SECONDS
    assert body["scope"] == "read"

    # The access token is a thoth-issued HS256 JWT decodable with the test secret.
    claims = jwt.decode(body["access_token"], SIGNING_SECRET, algorithms=["HS256"])
    assert claims["sub"] == ALLOWED_USER
    assert "iat" in claims
    assert "exp" in claims
    # The lifetime matches the configured access-token TTL.
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_SECONDS


@requires_multipart
def test_token_rejects_non_authorization_code_grant(tmp_path: Path) -> None:
    """Only the authorization_code grant is supported; others are rejected."""
    client = _client(_oauth_config(tmp_path))
    resp = client.post("/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


# --- JWT mint / verify round-trip (HS256, no network, no form parsing) --------------


def test_mint_oauth_jwt_round_trips_through_verify(tmp_path: Path) -> None:
    """A minted token verifies via verify_oauth_jwt and carries {sub, iat, exp}."""
    from thoth.mcp_oauth import mint_oauth_jwt, verify_oauth_jwt

    config = _oauth_config(tmp_path)
    token = mint_oauth_jwt(ALLOWED_USER, config)
    claims = verify_oauth_jwt(token, config)
    assert claims["sub"] == ALLOWED_USER
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_SECONDS


def test_mint_oauth_jwt_is_hs256_decodable_with_the_secret(tmp_path: Path) -> None:
    """The minted access token is a plain HS256 JWT signed with the config secret."""
    import jwt

    from thoth.mcp_oauth import mint_oauth_jwt

    config = _oauth_config(tmp_path)
    token = mint_oauth_jwt(ALLOWED_USER, config)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "HS256"
    claims = jwt.decode(token, SIGNING_SECRET, algorithms=["HS256"])
    assert claims["sub"] == ALLOWED_USER


def test_verify_oauth_jwt_rejects_expired_token(tmp_path: Path) -> None:
    """An expired token (TTL constructed in the past) is an AuthError, not a default."""
    from thoth.mcp_auth import AuthError
    from thoth.mcp_oauth import mint_oauth_jwt, verify_oauth_jwt

    config = _oauth_config(tmp_path)
    # Construct the TTL directly (negative) so the token is born expired -- no sleep, no
    # real time-bomb.
    token = mint_oauth_jwt(ALLOWED_USER, config, ttl_seconds=-10)
    with pytest.raises(AuthError):
        verify_oauth_jwt(token, config)


def test_verify_oauth_jwt_rejects_wrong_secret(tmp_path: Path) -> None:
    """A token signed with a different secret fails signature verification."""
    import jwt

    from thoth.mcp_auth import AuthError
    from thoth.mcp_oauth import verify_oauth_jwt

    config = _oauth_config(tmp_path)
    import time as _time

    now = int(_time.time())
    forged = jwt.encode(
        {"sub": ALLOWED_USER, "iat": now, "exp": now + 3600},
        "not-the-real-secret-" + "y" * 32,  # >=32 bytes; quiets PyJWT key-length check
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_oauth_jwt(forged, config)


def test_verify_oauth_jwt_rejects_missing_token(tmp_path: Path) -> None:
    """A missing (None) token is an AuthError surfaced as a 401 upstream."""
    from thoth.mcp_auth import AuthError
    from thoth.mcp_oauth import verify_oauth_jwt

    config = _oauth_config(tmp_path)
    with pytest.raises(AuthError, match="missing"):
        verify_oauth_jwt(None, config)


def test_verify_oauth_jwt_rejects_deauthorized_subject(tmp_path: Path) -> None:
    """A still-signed, unexpired token is rejected once its sub leaves the allow-list.

    The allow-list is the appliance's only authorization control and there is no
    revocation path, so de-authorizing a user must bound the 24h token by the *current*
    config -- not the config at mint time.
    """
    from thoth.mcp_auth import AuthError
    from thoth.mcp_oauth import mint_oauth_jwt, verify_oauth_jwt

    # Mint while allow-listed, then verify against a config where the user is gone.
    minting_config = _oauth_config(tmp_path)
    token = mint_oauth_jwt(ALLOWED_USER, minting_config)
    revoked_config = _oauth_config(tmp_path, THOTH_ALLOWED_GITHUB_USERS="someone-else")
    with pytest.raises(AuthError, match="allow-listed"):
        verify_oauth_jwt(token, revoked_config)


# --- TTL constants (no real time-bombs; assert the documented lifetimes) ------------


def test_token_ttls_are_the_documented_lifetimes() -> None:
    """Access-token TTL is 24h and the authorization-code TTL is 5 min (plan §6)."""
    assert ACCESS_TOKEN_TTL_SECONDS == 24 * 3600
    assert AUTH_CODE_TTL_SECONDS == 300
