# 11. MCP HTTP transport with tiered authentication

Date: 2026-06-03

## Status

Accepted

## Context

thoth's MCP server (`thoth mcp`) has so far spoken only stdio. The host, which is Claude Code running locally, spawns `thoth mcp` as a child process, and the OS process boundary is the trust boundary. There is no socket, no authentication, and only a local user who can already run the binary can reach the tools. That is the right default and it stays the default.

But the appliance runs headless on a VPS, and the owner wants to reach the same seven `pkm_*` tools from claude.ai on web and mobile, and from a remote Claude Code. That needs a network transport, and three things make it more than opening a port:

1. **A network socket is an attack surface.** The MCP tools are a closed surface (SPEC section 3), with no shell, no arbitrary file access and every path vault-confined, but they still write the vault and spend the LLM budget. An unauthenticated socket would let anyone who can route to it file pages and burn API spend. So the transport must authenticate every request, and it must be impossible to start it unauthenticated.
2. **claude.ai connectors do not accept a user-pasted static bearer.** A remote MCP client like Claude Code is configured with a static `Authorization: Bearer <key>` header. claude.ai's web and mobile custom connectors cannot be, because they only authenticate a remote MCP server through an OAuth 2.1 flow of discovery, dynamic client registration and PKCE. We do not want to build an OAuth authorization server inside thoth.
3. **The closed-surface model already governs *what*, not *who*.** thoth's authority model is the tool surface itself, so every caller, however authenticated, can do exactly the seven `pkm_*` operations and nothing more. Authentication here is purely a gate on who reaches the door, and it needs no per-user authorization, scopes or roles, because there is one user and one capability set.

## Decision

**Add an opt-in HTTP transport with two stacked authentication tiers, and delegate network exposure and OAuth to Cloudflare rather than building them into thoth.**

### Transport

`thoth mcp` gains `--transport stdio|http`, defaulting to `stdio`, which is byte-for-byte unchanged with no socket and host and port ignored.

`--transport http` serves FastMCP's streamable-HTTP app via uvicorn, bound to `--host`, default `127.0.0.1` and therefore loopback rather than `0.0.0.0`, and `--port`, default `8765`.

### Tier 1, a static bearer, mandatory for HTTP

Every HTTP request must carry `Authorization: Bearer <key>` matching one of the keys in `THOTH_MCP_API_KEYS`, which is comma-separated to support rotation. The match is constant-time via `hmac.compare_digest`.

A missing or invalid bearer is rejected with a 401 before any tool is dispatched.

If `--transport http` is selected and `THOTH_MCP_API_KEYS` is unset or empty, the server fails fast at startup and never binds an unauthenticated socket. This is the tier remote Claude Code uses.

### Tier 2, a Cloudflare-Access JWT, opt-in defence-in-depth

When both `THOTH_MCP_CF_ACCESS_TEAM_DOMAIN` and `THOTH_MCP_CF_ACCESS_AUD` are set, the origin also requires and validates the `Cf-Access-Jwt-Assertion` header that cloudflared adds.

Validation checks the signature against the team JWKS at `https://<team-domain>/cdn-cgi/access/certs`, an `aud` match and an `exp` in the future, with the algorithm pinned to RS256 so the `none` algorithm and RS or HS key-confusion are rejected. Leaving them unset runs bearer-only.

### Exposure and OAuth belong to Cloudflare, not thoth

The loopback socket is exposed to the internet by a cloudflared tunnel, and claude.ai's OAuth requirement is satisfied by Cloudflare Access "Managed OAuth" sitting in front of it. Cloudflare is the OAuth 2.1 authorization server for discovery, DCR and PKCE, not thoth.

Cloudflare authenticates the human, the Cf-Access JWT of Tier 2 lets the origin verify the request really transited Access, and the static bearer of Tier 1 is the always-on baseline that also serves the non-browser Claude Code path.

The Tier-2 wiring is documented as a how-to in `docs/how-to/mcp-server-setup.md`, with placeholders only. thoth ships the env-gated enforcement hook but no live Cloudflare configuration.

## Consequences

- **The local default is untouched.** `thoth mcp` over stdio is byte-for-byte the same spawn-as-a-child server, with no socket, no key and no behaviour change for the local Claude Code path.
- **An HTTP server can never start unauthenticated.** `THOTH_MCP_API_KEYS` is a fail-fast precondition checked before the graph is wired or a socket is bound.
- **Two callers, one surface.** Claude Code authenticates with a static bearer, while claude.ai authenticates through Cloudflare-Access OAuth and the request arrives bearing both a bearer, configured on the Access side, and the Cf-Access JWT. Both reach the identical seven-tool closed surface, because authentication gates who and the surface still governs what.
- **No OAuth server in thoth.** We depend on Cloudflare Access Managed OAuth for the claude.ai flow rather than implementing RFC 8414, RFC 7591 and PKCE ourselves. The trade-off is a dependency on that Cloudflare feature, and on tuning Cloudflare bot rules so OAuth discovery is not blocked, which the how-to documents.
- **New optional dependencies**, in the `runtime` extra only: `uvicorn` and `starlette` to serve the ASGI app, and `pyjwt[crypto]` to verify the Cf-Access assertion. CI is unchanged, because those are absent there, the HTTP wiring is exercised live, and the auth primitives are unit tested with a throwaway RSA keypair.
- **Defence-in-depth is opt-in.** Running bearer-only, with no Cf-Access vars, is supported and simplest. Turning on Tier 2 hardens the origin against a tunnel misconfiguration that ever let a request reach it without transiting Access.
- **The DNS-rebinding guard fights the tunnel.** FastMCP's streamable-HTTP transport enables DNS-rebinding protection that by default accepts only loopback `Host` and `Origin` headers, so the public hostname forwarded by cloudflared would `421`. The deployment resolves this by rewriting the origin `Host` to loopback in the tunnel ingress with `httpHostHeader: localhost:<port>`, which is preferred because the guard stays meaningful, with optional `THOTH_MCP_ALLOWED_HOSTS` and `THOTH_MCP_ALLOWED_ORIGINS` env vars that *append* to the loopback defaults as an explicit-allowlist alternative. This was verified locally against the real `mcp` package: with an allowed `Host`, an authenticated `initialize` round-trips over streamable-HTTP through the auth middleware, confirming the bearer gate sits ahead of dispatch without breaking the SSE stream.
