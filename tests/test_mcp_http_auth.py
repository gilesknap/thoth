"""Tests for the MCP HTTP transport and its two-tier auth gate (issue #103).

These cover the network surface added by ``thoth mcp --transport http``:

* transport selection -- ``stdio`` stays the byte-for-byte-unchanged default, ``http``
  routes through the auth-gated serve path, an unknown transport is rejected;
* fail-fast -- ``--transport http`` with ``THOTH_MCP_API_KEYS`` unset never binds;
* Tier-1 bearer -- accept a valid key (constant-time), reject a missing/wrong one
  with 401 BEFORE any tool dispatch;
* Tier-2 Cf-Access JWT -- validate a freshly-signed token (throwaway RSA keypair,
  stubbed JWKS) and reject expired / wrong-audience / ``alg=none`` tokens;
* ``tools/list`` parity -- the HTTP server still registers all five ``pkm_*`` tools.

No real ``mcp``/``uvicorn``/network: FastMCP, uvicorn and the JWKS fetch are faked. The
JWT path uses a real in-test RSA keypair via ``pyjwt[crypto]`` (a dev dependency), so
the signature/claim checks are exercised end to end without a live Cloudflare.
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from thoth.config import Config, ConfigError, load_config
from thoth.mcp_auth import (
    AuthError,
    bearer_key_accepted,
    build_auth_middleware,
    extract_bearer_token,
    verify_cf_access_jwt,
)

# --- shared fixtures: a seeded vault + config (mirrors test_mcp_server) -------------

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


def _seed_vault(root: Path) -> None:
    """Write the minimal folder skeleton + spine the Vault facade expects."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "log.md").write_text("# Vault Log\n", encoding="utf-8")


def _config(tmp_path: Path, **extra: str) -> Config:
    """Build a Config over a seeded tmp vault, overlaying any extra env vars."""
    _seed_vault(tmp_path)
    env = {"PKM_VAULT": str(tmp_path), **extra}
    return load_config(env)


# --- pure bearer-token primitives --------------------------------------------------


def test_extract_bearer_token_parses_scheme_case_insensitively() -> None:
    """The Bearer scheme is matched case-insensitively; the token is byte-exact."""
    assert extract_bearer_token("Bearer abc123") == "abc123"
    assert extract_bearer_token("bearer abc123") == "abc123"
    assert extract_bearer_token("BEARER   spaced  ") == "spaced"


@pytest.mark.parametrize(
    "header",
    [None, "", "abc123", "Basic abc123", "Bearer", "Bearer    "],
)
def test_extract_bearer_token_rejects_non_bearer(header: str | None) -> None:
    """A missing header, a non-Bearer scheme, or an empty token yields None."""
    assert extract_bearer_token(header) is None


def test_bearer_key_accepted_matches_one_of_the_keys() -> None:
    """A token matching any configured key is accepted; a near-miss is not."""
    keys = frozenset({"alpha", "beta"})
    assert bearer_key_accepted("alpha", keys) is True
    assert bearer_key_accepted("beta", keys) is True
    assert bearer_key_accepted("gamma", keys) is False
    assert bearer_key_accepted(None, keys) is False
    assert bearer_key_accepted("alpha", frozenset()) is False


def test_bearer_key_accepted_non_ascii_token_is_rejected_not_error() -> None:
    """A non-ASCII (attacker-controlled) token yields a clean False, never a TypeError.

    hmac.compare_digest raises on non-ASCII *str* operands; the bytes comparison turns
    such a token into a 401 instead of an unhandled 500 (issue #103 review).
    """
    assert bearer_key_accepted("café", frozenset({"secret"})) is False
    # A non-ASCII token that genuinely matches a non-ASCII key still works.
    assert bearer_key_accepted("clé-✓", frozenset({"clé-✓"})) is True


# --- config parsing of the new env vars --------------------------------------------


def test_config_parses_allowed_hosts_and_origins(tmp_path: Path) -> None:
    """THOTH_MCP_ALLOWED_HOSTS/_ORIGINS split on commas, trim, and drop blanks."""
    config = _config(
        tmp_path,
        THOTH_MCP_ALLOWED_HOSTS=" mcp.example.com , other:* ,, ",
        THOTH_MCP_ALLOWED_ORIGINS=" https://mcp.example.com ,, ",
    )
    assert config.mcp_allowed_hosts_list() == ("mcp.example.com", "other:*")
    assert config.mcp_allowed_origins_list() == ("https://mcp.example.com",)
    # Unset -> empty tuples (loopback defaults are kept as-is downstream).
    assert _config(tmp_path).mcp_allowed_hosts_list() == ()
    assert _config(tmp_path).mcp_allowed_origins_list() == ()


def test_config_parses_and_rotates_bearer_keys(tmp_path: Path) -> None:
    """THOTH_MCP_API_KEYS splits on commas, trims, and drops blanks (rotation set)."""
    config = _config(tmp_path, THOTH_MCP_API_KEYS=" k1 , k2 ,, k3 ")
    assert config.mcp_api_key_set() == frozenset({"k1", "k2", "k3"})


def test_require_mcp_api_keys_fails_when_unset(tmp_path: Path) -> None:
    """An unset THOTH_MCP_API_KEYS is a fail-fast ConfigError (no open socket)."""
    config = _config(tmp_path)
    assert config.mcp_api_key_set() == frozenset()
    with pytest.raises(ConfigError, match="THOTH_MCP_API_KEYS"):
        config.require_mcp_api_keys()


def test_cf_access_enabled_only_when_both_vars_set(tmp_path: Path) -> None:
    """Cf-Access is opt-in: enabled only when BOTH team domain and aud are set."""
    assert _config(tmp_path).mcp_cf_access_enabled() is False
    team_only = _config(tmp_path, THOTH_MCP_CF_ACCESS_TEAM_DOMAIN="t.example")
    assert team_only.mcp_cf_access_enabled() is False
    aud_only = _config(tmp_path, THOTH_MCP_CF_ACCESS_AUD="aud")
    assert aud_only.mcp_cf_access_enabled() is False
    both = _config(
        tmp_path,
        THOTH_MCP_CF_ACCESS_TEAM_DOMAIN="t.example",
        THOTH_MCP_CF_ACCESS_AUD="aud",
    )
    assert both.mcp_cf_access_enabled() is True


# --- Cf-Access JWT verification (real RSA keypair, stubbed JWKS) --------------------

TEAM_DOMAIN = "team.cloudflareaccess.com"
AUD = "test-audience-tag"


@pytest.fixture
def rsa_keypair() -> Any:
    """A throwaway RSA keypair for signing test JWTs (ephemeral, in-test only)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(private_key: Any, claims: dict[str, Any], *, alg: str = "RS256") -> str:
    """Sign a JWT with the test private key (or alg='none' for the unsigned case)."""
    import jwt

    if alg == "none":
        return jwt.encode(claims, key=None, algorithm="none")  # type: ignore[arg-type]
    return jwt.encode(claims, private_key, algorithm=alg)


def _jwks_fetcher(private_key: Any) -> Any:
    """Return a fetcher(url) -> client whose signing key is the keypair's public key."""

    class _StubClient:
        def __init__(self, _url: str) -> None:
            self._url = _url

        def get_signing_key_from_jwt(self, _token: str) -> Any:
            return types.SimpleNamespace(key=private_key.public_key())

    return _StubClient


def _claims(**overrides: Any) -> dict[str, Any]:
    """Default valid Cf-Access claims (future exp, matching aud + issuer)."""
    now = dt.datetime.now(tz=dt.UTC)
    base = {
        "aud": AUD,
        "iss": f"https://{TEAM_DOMAIN}",
        "exp": now + dt.timedelta(hours=1),
        "iat": now,
        "email": "owner@example.com",
    }
    base.update(overrides)
    return base


def test_verify_cf_access_jwt_accepts_valid_token(rsa_keypair: Any) -> None:
    """A correctly-signed, in-date, right-audience token verifies and returns claims."""
    token = _sign(rsa_keypair, _claims())
    claims = verify_cf_access_jwt(
        token,
        team_domain=TEAM_DOMAIN,
        audience=AUD,
        jwks_fetcher=_jwks_fetcher(rsa_keypair),
    )
    assert claims["email"] == "owner@example.com"
    assert claims["aud"] == AUD


def test_verify_cf_access_jwt_rejects_missing_token(rsa_keypair: Any) -> None:
    """A missing assertion header is an AuthError (surfaced 401)."""
    with pytest.raises(AuthError, match="missing"):
        verify_cf_access_jwt(
            None,
            team_domain=TEAM_DOMAIN,
            audience=AUD,
            jwks_fetcher=_jwks_fetcher(rsa_keypair),
        )


def test_verify_cf_access_jwt_rejects_expired_token(rsa_keypair: Any) -> None:
    """An expired token fails the exp check."""
    past = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)
    token = _sign(rsa_keypair, _claims(exp=past))
    with pytest.raises(AuthError):
        verify_cf_access_jwt(
            token,
            team_domain=TEAM_DOMAIN,
            audience=AUD,
            jwks_fetcher=_jwks_fetcher(rsa_keypair),
        )


def test_verify_cf_access_jwt_rejects_wrong_audience(rsa_keypair: Any) -> None:
    """A token whose aud does not match the configured tag is rejected."""
    token = _sign(rsa_keypair, _claims(aud="some-other-app"))
    with pytest.raises(AuthError):
        verify_cf_access_jwt(
            token,
            team_domain=TEAM_DOMAIN,
            audience=AUD,
            jwks_fetcher=_jwks_fetcher(rsa_keypair),
        )


def test_verify_cf_access_jwt_rejects_alg_none(rsa_keypair: Any) -> None:
    """An ``alg=none`` (unsigned) token is rejected -- the algorithm is pinned RS256."""
    token = _sign(rsa_keypair, _claims(), alg="none")
    with pytest.raises(AuthError):
        verify_cf_access_jwt(
            token,
            team_domain=TEAM_DOMAIN,
            audience=AUD,
            jwks_fetcher=_jwks_fetcher(rsa_keypair),
        )


# --- the run() transport selection (fake FastMCP + fake uvicorn) --------------------


class _FakeSettings:
    """Captures the host/port FastMCP settings the http path assigns."""

    def __init__(self) -> None:
        self.host = ""
        self.port = 0
        # Mirror FastMCP's real loopback DNS-rebinding defaults so the allowlist-
        # extension wiring (issue #103) can be asserted against a faithful stand-in.
        self.transport_security = types.SimpleNamespace(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )


class _FakeApp:
    """A stand-in ASGI app recording the middleware classes added to it."""

    def __init__(self) -> None:
        self.middlewares: list[Any] = []

    def add_middleware(self, cls: Any, **kwargs: Any) -> None:
        self.middlewares.append(cls)


class _FakeFastMCP:
    """Recording FastMCP: captures tools, stdio runs, and the streamable-http app."""

    instances: list[_FakeFastMCP] = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.registered: dict[str, Any] = {}
        self.ran_with: list[dict[str, Any]] = []
        self.settings = _FakeSettings()
        self.app = _FakeApp()
        _FakeFastMCP.instances.append(self)

    def tool(self, *, name: str) -> Any:
        def decorator(func: Any) -> Any:
            self.registered[name] = func
            return func

        return decorator

    def run(self, **kwargs: Any) -> None:
        self.ran_with.append(kwargs)

    def streamable_http_app(self) -> _FakeApp:
        return self.app


@pytest.fixture
def fake_mcp_and_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject a fake mcp.server.fastmcp and a fake uvicorn; record what they receive."""
    _FakeFastMCP.instances.clear()
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    server_mod.fastmcp = fastmcp_mod  # type: ignore[attr-defined]
    mcp_mod.server = server_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_mod)

    served: dict[str, Any] = {}
    uvicorn_mod = types.ModuleType("uvicorn")

    def _run(app: Any, **kwargs: Any) -> None:
        served["app"] = app
        served["kwargs"] = kwargs

    uvicorn_mod.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_mod)
    return {"served": served, "fastmcp": _FakeFastMCP}


def _ctx(config: Config) -> Any:
    """Build a real ToolContext over the config's vault (fakes for the engines)."""
    from thoth.mcp_server import ToolContext
    from thoth.vault import Vault

    class _Stub:
        def __getattr__(self, _name: str) -> Any:  # pragma: no cover - never called
            raise AssertionError("engine should not be touched by transport wiring")

    return ToolContext(
        config=config,
        vault=Vault(config),
        ingestor=_Stub(),  # type: ignore[arg-type]
        query_engine=_Stub(),  # type: ignore[arg-type]
        git=_Stub(),  # type: ignore[arg-type]
    )


def test_run_defaults_to_stdio_unchanged(
    tmp_path: Path, fake_mcp_and_uvicorn: dict[str, Any]
) -> None:
    """run() with no transport runs stdio (no socket, uvicorn never touched)."""
    from thoth import mcp_server

    config = _config(tmp_path)
    mcp_server.run(config, _ctx(config))
    server = fake_mcp_and_uvicorn["fastmcp"].instances[-1]
    assert server.ran_with == [{"transport": "stdio"}]
    assert fake_mcp_and_uvicorn["served"] == {}


def test_run_rejects_unknown_transport(tmp_path: Path) -> None:
    """An unknown transport is a ValueError before any wiring."""
    from thoth import mcp_server

    config = _config(tmp_path)
    with pytest.raises(ValueError, match="unknown MCP transport"):
        mcp_server.run(config, transport="websocket")


def test_run_http_fails_fast_without_api_keys(
    tmp_path: Path, fake_mcp_and_uvicorn: dict[str, Any]
) -> None:
    """--transport http with no THOTH_MCP_API_KEYS fails fast, binding nothing."""
    from thoth import mcp_server

    config = _config(tmp_path)  # no THOTH_MCP_API_KEYS
    with pytest.raises(ConfigError, match="THOTH_MCP_API_KEYS"):
        mcp_server.run(config, _ctx(config), transport="http")
    # No FastMCP was even built and uvicorn was never called.
    assert fake_mcp_and_uvicorn["served"] == {}


def test_run_http_serves_with_auth_middleware_on_host_port(
    tmp_path: Path, fake_mcp_and_uvicorn: dict[str, Any]
) -> None:
    """--transport http binds host:port, installs the auth middleware, runs uvicorn."""
    from thoth import mcp_server

    config = _config(tmp_path, THOTH_MCP_API_KEYS="secret-key")
    mcp_server.run(config, _ctx(config), transport="http", host="127.0.0.1", port=9999)
    server = fake_mcp_and_uvicorn["fastmcp"].instances[-1]
    # Host/port reached the FastMCP settings and uvicorn.
    assert server.settings.host == "127.0.0.1"
    assert server.settings.port == 9999
    served = fake_mcp_and_uvicorn["served"]
    assert served["app"] is server.app
    assert served["kwargs"]["host"] == "127.0.0.1"
    assert served["kwargs"]["port"] == 9999
    # The auth middleware was installed (exactly one), and stdio.run was NOT called.
    assert len(server.app.middlewares) == 1
    assert server.ran_with == []


def test_run_http_extends_dns_rebinding_allowlists_when_configured(
    tmp_path: Path, fake_mcp_and_uvicorn: dict[str, Any]
) -> None:
    """THOTH_MCP_ALLOWED_HOSTS/_ORIGINS append to (not replace) the loopback defaults.

    Without this the public Host header forwarded by cloudflared would 421 against
    FastMCP's DNS-rebinding guard (issue #103 / ADR 0011).
    """
    from thoth import mcp_server

    config = _config(
        tmp_path,
        THOTH_MCP_API_KEYS="secret-key",
        THOTH_MCP_ALLOWED_HOSTS="mcp.example.com",
        THOTH_MCP_ALLOWED_ORIGINS="https://mcp.example.com",
    )
    mcp_server.run(config, _ctx(config), transport="http", port=9001)
    server = fake_mcp_and_uvicorn["fastmcp"].instances[-1]
    sec = server.settings.transport_security
    assert "mcp.example.com" in sec.allowed_hosts
    assert "https://mcp.example.com" in sec.allowed_origins
    # Loopback defaults are preserved (append, not replace).
    assert "localhost:*" in sec.allowed_hosts


def test_run_http_leaves_allowlists_at_defaults_when_unset(
    tmp_path: Path, fake_mcp_and_uvicorn: dict[str, Any]
) -> None:
    """With no allowlist env, the transport-security defaults are left untouched."""
    from thoth import mcp_server

    config = _config(tmp_path, THOTH_MCP_API_KEYS="secret-key")
    mcp_server.run(config, _ctx(config), transport="http", port=9002)
    server = fake_mcp_and_uvicorn["fastmcp"].instances[-1]
    sec = server.settings.transport_security
    assert sec.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_run_http_registers_all_five_tools(
    tmp_path: Path, fake_mcp_and_uvicorn: dict[str, Any]
) -> None:
    """tools/list parity: the HTTP server still exposes all five pkm_* tools."""
    from thoth import mcp_server

    config = _config(tmp_path, THOTH_MCP_API_KEYS="secret-key")
    mcp_server.run(config, _ctx(config), transport="http", port=9998)
    server = fake_mcp_and_uvicorn["fastmcp"].instances[-1]
    assert set(server.registered) == set(mcp_server.TOOL_NAMES)
    assert len(server.registered) == 7


# --- the auth middleware end to end (bearer accept / reject before dispatch) --------


class _FakeRequest:
    """A minimal Starlette-request stand-in carrying what the gate reads.

    The gate reads ``request.headers`` (Tier 1/2 bearer + Cf-Access),
    ``request.url.path`` (the OAuth public-path allow-list) and writes
    ``request.state.oauth_sub`` (the decoded JWT subject). ``path`` defaults to a
    protected route so the bearer-only tests are unaffected by the OAuth allow-list.
    """

    def __init__(self, headers: dict[str, str], *, path: str = "/mcp") -> None:
        # Starlette headers are case-insensitive; lower-case the keys to match .get().
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = types.SimpleNamespace(path=path)
        self.state = types.SimpleNamespace()


async def _call_next_marker(_request: Any) -> str:
    """A sentinel downstream handler: returning this proves dispatch was reached."""
    return "dispatched"


def _run_async(coro: Any) -> Any:
    """Drive a coroutine to completion without pytest-asyncio."""
    import asyncio

    return asyncio.run(coro)


def test_auth_middleware_rejects_missing_bearer_before_dispatch(tmp_path: Path) -> None:
    """A request with no bearer is 401'd and the downstream handler never runs."""
    config = _config(tmp_path, THOTH_MCP_API_KEYS="good-key")
    middleware_cls = build_auth_middleware(config)
    gate = middleware_cls(app=lambda *a, **k: None)  # type: ignore[arg-type]

    response = _run_async(
        gate.dispatch(_FakeRequest({}), _call_next_marker)  # type: ignore[arg-type]
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_auth_middleware_rejects_wrong_bearer(tmp_path: Path) -> None:
    """A wrong bearer key is 401'd before dispatch."""
    config = _config(tmp_path, THOTH_MCP_API_KEYS="good-key")
    gate = build_auth_middleware(config)(app=lambda *a, **k: None)  # type: ignore[arg-type]

    response = _run_async(
        gate.dispatch(
            _FakeRequest({"Authorization": "Bearer wrong-key"}),
            _call_next_marker,
        )  # type: ignore[arg-type]
    )
    assert response.status_code == 401


def test_auth_middleware_accepts_valid_bearer_and_dispatches(tmp_path: Path) -> None:
    """A valid bearer (and no Cf-Access required) reaches the downstream handler."""
    config = _config(tmp_path, THOTH_MCP_API_KEYS="good-key,rotated-key")
    gate = build_auth_middleware(config)(app=lambda *a, **k: None)  # type: ignore[arg-type]

    out = _run_async(
        gate.dispatch(
            _FakeRequest({"Authorization": "Bearer rotated-key"}),
            _call_next_marker,
        )  # type: ignore[arg-type]
    )
    assert out == "dispatched"


def _cf_config(tmp_path: Path) -> Config:
    """A config with bearer keys AND Cf-Access enabled (both team domain + aud set)."""
    return _config(
        tmp_path,
        THOTH_MCP_API_KEYS="good-key",
        THOTH_MCP_CF_ACCESS_TEAM_DOMAIN=TEAM_DOMAIN,
        THOTH_MCP_CF_ACCESS_AUD=AUD,
    )


def test_auth_middleware_rejects_invalid_cf_assertion_with_valid_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Cf-Access enabled, a valid bearer but bad assertion is 401'd pre-dispatch.

    This exercises the ``cf_enabled`` branch of the gate -- the wiring that reads the
    ``Cf-Access-Jwt-Assertion`` header, calls ``verify_cf_access_jwt`` with the
    configured team domain / audience, and converts an ``AuthError`` into a 401 -- which
    the bearer-only dispatch tests never reach.
    """
    import thoth.mcp_auth as mcp_auth

    seen: dict[str, Any] = {}

    def _fake_verify(token: Any, *, team_domain: str, audience: str, **_: Any) -> Any:
        seen["token"] = token
        seen["team_domain"] = team_domain
        seen["audience"] = audience
        raise AuthError("bad assertion")

    monkeypatch.setattr(mcp_auth, "verify_cf_access_jwt", _fake_verify)

    gate = build_auth_middleware(_cf_config(tmp_path))(app=lambda *a, **k: None)  # type: ignore[arg-type]
    response = _run_async(
        gate.dispatch(
            _FakeRequest({"Authorization": "Bearer good-key"}),
            _call_next_marker,
        )  # type: ignore[arg-type]
    )
    assert response.status_code == 401
    # The gate passed the configured team domain / audience through to the verifier.
    assert seen["team_domain"] == TEAM_DOMAIN
    assert seen["audience"] == AUD


def test_auth_middleware_rejects_bad_bearer_before_checking_cf_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering: a wrong bearer is 401'd before the Cf-Access verifier runs at all."""
    import thoth.mcp_auth as mcp_auth

    def _exploding_verify(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("bearer must be checked before the Cf-Access assertion")

    monkeypatch.setattr(mcp_auth, "verify_cf_access_jwt", _exploding_verify)

    gate = build_auth_middleware(_cf_config(tmp_path))(app=lambda *a, **k: None)  # type: ignore[arg-type]
    response = _run_async(
        gate.dispatch(
            _FakeRequest({"Authorization": "Bearer wrong-key"}),
            _call_next_marker,
        )  # type: ignore[arg-type]
    )
    assert response.status_code == 401


def test_auth_middleware_accepts_valid_bearer_and_cf_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid bearer AND a valid Cf-Access assertion reach the downstream handler."""
    import thoth.mcp_auth as mcp_auth

    def _fake_verify(token: Any, **_: Any) -> dict[str, Any]:
        return {"email": "owner@example.com", "assertion": token}

    monkeypatch.setattr(mcp_auth, "verify_cf_access_jwt", _fake_verify)

    gate = build_auth_middleware(_cf_config(tmp_path))(app=lambda *a, **k: None)  # type: ignore[arg-type]
    out = _run_async(
        gate.dispatch(
            _FakeRequest(
                {
                    "Authorization": "Bearer good-key",
                    "Cf-Access-Jwt-Assertion": "a.valid.jwt",
                }
            ),
            _call_next_marker,
        )  # type: ignore[arg-type]
    )
    assert out == "dispatched"


# --- OAuth 2.1: the gate also accepts a thoth-issued JWT (additive, opt-in) ----------

# A signing secret >=32 bytes so PyJWT's HS256 key-length check stays quiet under
# filterwarnings=error. Fake placeholders only (gitleaks scans the commit).
_OAUTH_SIGNING_SECRET = "test-oauth-signing-secret-" + "z" * 32
_OAUTH_SERVER_URL = "https://mcp.example.com"


def _oauth_config(tmp_path: Path, **extra: str) -> Config:
    """A config with bearer keys AND OAuth 2.1 fully enabled (all required vars)."""
    return _config(
        tmp_path,
        THOTH_MCP_API_KEYS="good-key",
        GITHUB_OAUTH_CLIENT_ID="test-client-id",
        GITHUB_OAUTH_CLIENT_SECRET="test-client-secret",
        THOTH_JWT_SIGNING_SECRET=_OAUTH_SIGNING_SECRET,
        THOTH_OAUTH_SERVER_URL=_OAUTH_SERVER_URL,
        THOTH_ALLOWED_GITHUB_USERS="octocat",
        **extra,
    )


_EXPECTED_RESOURCE_METADATA = (
    f'Bearer resource_metadata="{_OAUTH_SERVER_URL}'
    '/.well-known/oauth-protected-resource"'
)


def gate_dispatch(config: Config, request: _FakeRequest) -> Any:
    """Build the gate for ``config`` and dispatch ``request`` through it (coroutine)."""
    gate = build_auth_middleware(config)(app=lambda *a, **k: None)  # type: ignore[arg-type]
    return gate.dispatch(request, _call_next_marker)  # type: ignore[arg-type]


def test_auth_middleware_static_bearer_still_works_with_oauth_enabled(
    tmp_path: Path,
) -> None:
    """Regression: a static THOTH_MCP_API_KEYS bearer still works after OAuth is on.

    OAuth is additive -- turning it on must not break the API-key path Claude Code uses.
    """
    gate = build_auth_middleware(_oauth_config(tmp_path))(app=lambda *a, **k: None)  # type: ignore[arg-type]
    out = _run_async(
        gate.dispatch(
            _FakeRequest({"Authorization": "Bearer good-key"}),
            _call_next_marker,
        )  # type: ignore[arg-type]
    )
    assert out == "dispatched"


def test_auth_middleware_accepts_valid_oauth_jwt_and_attaches_sub(
    tmp_path: Path,
) -> None:
    """A valid thoth OAuth JWT authorises and its ``sub`` lands on request.state."""
    from thoth.mcp_oauth import mint_oauth_jwt

    config = _oauth_config(tmp_path)
    token = mint_oauth_jwt("octocat", config)
    request = _FakeRequest({"Authorization": f"Bearer {token}"})

    out = _run_async(gate_dispatch(config, request))
    assert out == "dispatched"
    # The decoded subject is attached for downstream logging/attribution.
    assert request.state.oauth_sub == "octocat"


def test_auth_middleware_rejects_invalid_jwt_with_resource_metadata_hint(
    tmp_path: Path,
) -> None:
    """A garbage bearer (neither a key nor a valid JWT) is 401'd with the 9728 hint."""
    config = _oauth_config(tmp_path)
    response = _run_async(
        gate_dispatch(config, _FakeRequest({"Authorization": "Bearer not.a.jwt"}))
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == _EXPECTED_RESOURCE_METADATA


def test_auth_middleware_rejects_expired_jwt_with_resource_metadata_hint(
    tmp_path: Path,
) -> None:
    """An expired thoth JWT is 401'd; the hint still points at the resource metadata.

    The token is born expired via a negative TTL (no sleep, no real time-bomb).
    """
    from thoth.mcp_oauth import mint_oauth_jwt

    config = _oauth_config(tmp_path)
    token = mint_oauth_jwt("octocat", config, ttl_seconds=-10)
    response = _run_async(
        gate_dispatch(config, _FakeRequest({"Authorization": f"Bearer {token}"}))
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == _EXPECTED_RESOURCE_METADATA


def test_auth_middleware_rejects_missing_token_with_resource_metadata_hint(
    tmp_path: Path,
) -> None:
    """A request with no bearer at all is 401'd carrying the RFC 9728 discovery hint."""
    config = _oauth_config(tmp_path)
    response = _run_async(gate_dispatch(config, _FakeRequest({})))
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == _EXPECTED_RESOURCE_METADATA


def test_auth_middleware_lets_oauth_discovery_path_through_without_a_bearer(
    tmp_path: Path,
) -> None:
    """When OAuth is enabled, the discovery/OAuth routes are reachable without a token.

    A client must be able to fetch the metadata and run the authorize/token dance before
    it holds any credential, so those paths bypass the bearer gate entirely.
    """
    config = _oauth_config(tmp_path)
    request = _FakeRequest({}, path="/.well-known/oauth-protected-resource")
    out = _run_async(gate_dispatch(config, request))
    assert out == "dispatched"
