# 11. MCP HTTP transport with tiered authentication

Date: 2026-06-03

## Status

Accepted

## Context

thoth's MCP server (`thoth mcp`) has so far spoken only **stdio**: the host (Claude
Code, locally) spawns `thoth mcp` as a child process and the OS process boundary is the
trust boundary — there is no socket, no authentication, and only a local user who can
already run the binary can reach the tools. That is the right default and it stays the
default.

But the appliance runs headless on a VPS, and the owner wants to reach the same seven
`pkm_*` tools from **claude.ai (web + mobile)** and from a **remote Claude Code**. That
needs a network transport. Three things make this more than "open a port":

1. **A network socket is an attack surface.** The MCP tools are a closed surface (SPEC
   section 3) — no shell, no arbitrary file access, every path vault-confined — but they
   still *write the vault* and *spend the LLM budget*. An unauthenticated socket would
   let anyone who can route to it file pages and burn API spend. So the transport must
   authenticate every request, and it must be *impossible to start it unauthenticated*.

2. **claude.ai connectors do not accept a user-pasted static bearer.** A remote MCP
   client like Claude Code can be configured with a static `Authorization: Bearer <key>`
   header. claude.ai's web/mobile **custom connectors** cannot: they only authenticate a
   remote MCP server through an **OAuth 2.1** flow (discovery, dynamic client
   registration, PKCE). We do not want to *build* an OAuth authorization server inside
   thoth.

3. **The closed-surface model already governs _what_, not _who_.** thoth's authority
   model is the tool surface itself — every caller, however authenticated, can do exactly
   the seven `pkm_*` operations and nothing more. Authentication here is purely a
   gate on *who reaches the door*; it does not need per-user authorization, scopes, or
   roles, because there is only one user and one capability set.

## Decision

**Add an opt-in HTTP transport with two stacked authentication tiers, and delegate
network exposure and OAuth to Cloudflare rather than building them in thoth.**

**Transport.** `thoth mcp` gains `--transport stdio|http` (default `stdio`, byte-for-byte
unchanged — no socket, host/port ignored). `--transport http` serves FastMCP's
streamable-HTTP app via uvicorn, bound to `--host` (default `127.0.0.1` — **loopback,
never `0.0.0.0` by default**) and `--port` (default `8765`).

**Tier 1 — static bearer (mandatory for HTTP).** Every HTTP request must carry
`Authorization: Bearer <key>` matching one of the comma-separated keys in
`THOTH_MCP_API_KEYS` (comma-separated to support rotation). The match is constant-time
(`hmac.compare_digest`). A missing/invalid bearer is rejected **401 before any tool is
dispatched**. If `--transport http` is selected and `THOTH_MCP_API_KEYS` is unset/empty,
the server **fails fast at startup** — it never binds an unauthenticated socket. This is
the tier remote Claude Code uses.

**Tier 2 — Cloudflare-Access JWT (opt-in defense-in-depth).** When BOTH
`THOTH_MCP_CF_ACCESS_TEAM_DOMAIN` and `THOTH_MCP_CF_ACCESS_AUD` are set, the origin
*also* requires and validates the `Cf-Access-Jwt-Assertion` header that cloudflared adds:
signature against the team JWKS (`https://<team-domain>/cdn-cgi/access/certs`), `aud`
match, `exp` in the future, with the algorithm **pinned to RS256** (rejecting the `none`
algorithm and RS/HS key-confusion). Unset → bearer-only.

**Exposure + OAuth — Cloudflare, not thoth.** The loopback socket is exposed to the
internet by a **cloudflared tunnel**, and **claude.ai's OAuth requirement is satisfied by
Cloudflare Access "Managed OAuth"** sitting in front of it — Cloudflare is the OAuth 2.1
authorization server (discovery, DCR, PKCE), not thoth. Cloudflare authenticates the
human; the Cf-Access JWT (Tier 2) lets the origin verify the request really transited
Access; the static bearer (Tier 1) is the always-on baseline that also serves the
non-browser Claude Code path. The Tier-2 wiring is documented as a how-to
(`docs/how-to/deploy-appliance.md`, placeholders only); thoth ships the *enforcement
hook* (env-gated) but no live Cloudflare configuration.

## Consequences

- **The local default is untouched.** `thoth mcp` (stdio) is byte-for-byte the same
  spawn-as-a-child server; no socket, no key, no behaviour change for the local Claude
  Code path.
- **An HTTP server can never start unauthenticated.** `THOTH_MCP_API_KEYS` is a fail-fast
  precondition checked *before* the graph is wired or a socket is bound.
- **Two callers, one surface.** Claude Code authenticates with a static bearer;
  claude.ai authenticates through Cloudflare-Access OAuth and the request arrives bearing
  both a bearer (configured on the Access side) and the Cf-Access JWT. Both reach the
  identical seven-tool closed surface — authentication gates *who*, the surface still
  governs *what*.
- **No OAuth server in thoth.** We depend on Cloudflare Access Managed OAuth for the
  claude.ai flow rather than implementing RFC 8414 / 7591 / PKCE ourselves. The trade-off
  is a dependency on that Cloudflare feature and on tuning Cloudflare bot rules so OAuth
  discovery is not blocked (documented in the how-to).
- **New optional dependencies** (the `runtime` extra only): `uvicorn` + `starlette` to
  serve the ASGI app, `pyjwt[crypto]` to verify the Cf-Access assertion. CI is unchanged
  (these are absent there; the HTTP wiring is exercised live, the auth primitives are unit
  tested with a throwaway RSA keypair).
- **Defense-in-depth, opt-in.** Running bearer-only (no Cf-Access vars) is supported and
  simplest; turning on Tier 2 hardens the origin against a tunnel misconfiguration that
  ever let a request reach it without transiting Access.
