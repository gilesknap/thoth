# Set up the MCP server

The MCP server is how Claude Code and claude.ai reach the vault's seven `pkm_*`
tools. It runs as the `thoth-mcp.service` systemd unit — `thoth mcp --transport
http`, serving bearer-authenticated streamable-HTTP bound to **loopback**
(`127.0.0.1:8765`) — with a cloudflared tunnel exposing it to your remote
clients. See {doc}`../explanations/decisions/0011-mcp-http-transport-and-tiered-auth`
for the design.

This guide assumes the appliance is already deployed ({doc}`deploy-appliance`).
All hostnames, teams, and tags below are **placeholders** — substitute your own
and never commit the real values.

## 1. Add a bearer key

The server **fails fast** if no key is set — it never binds an unauthenticated
socket. As **`pkm`**:

```bash
# generate a key and append it; rotate later by adding a second comma-separated value
KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf 'THOTH_MCP_API_KEYS=%s\n' "$KEY" >> /home/pkm/.thoth/.env
chmod 600 /home/pkm/.thoth/.env
```

## 2. Install and enable the unit

As **root**:

```bash
cp /opt/thoth/deploy/thoth-mcp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now thoth-mcp.service
systemctl status thoth-mcp.service --no-pager
```

Like the other units it is pre-hardened and runs as `pkm`, reading secrets only
from `/home/pkm/.thoth/.env`.

## 3. Connect Claude Code

A remote Claude Code reaches it with that bearer:

```bash
# from your laptop, once the tunnel (section 4) is up — replace <public-hostname> and <key>
claude mcp add --transport http thoth https://<public-hostname>/mcp \
  --header "Authorization: Bearer <key>"
```

## 4. Expose it with cloudflared + Cloudflare Access

claude.ai's web/mobile custom connectors authenticate a remote MCP server
through an **OAuth 2.1** flow, not a pasted bearer — so to use the connector from
claude.ai, front the loopback server with a **cloudflared tunnel** and
**Cloudflare Access "Managed OAuth"** (Cloudflare is the OAuth authorization
server; thoth ships only the env-gated JWT enforcement hook).

1. **Install cloudflared and create a tunnel** (as root):

   ```bash
   # install cloudflared per Cloudflare's apt/rpm instructions, then:
   cloudflared tunnel login
   cloudflared tunnel create thoth-mcp
   cloudflared tunnel route dns thoth-mcp <public-hostname>   # e.g. mcp.example.com
   ```

2. **Point the tunnel ingress at the loopback MCP server.** In
   `/etc/cloudflared/config.yml` (placeholders):

   ```yaml
   tunnel: <tunnel-uuid>
   credentials-file: /etc/cloudflared/<tunnel-uuid>.json

   ingress:
     - hostname: <public-hostname>          # e.g. mcp.example.com
       service: http://127.0.0.1:8765       # matches thoth-mcp.service ExecStart
       originRequest:
         httpHostHeader: localhost:8765     # see the DNS-rebinding note below
     - service: http_status:404
   ```

   Run it as a service: `cloudflared service install && systemctl enable --now cloudflared`.

   **DNS-rebinding / `Host` header (don't skip this).** FastMCP's streamable-HTTP
   transport ships DNS-rebinding protection that, by default, only accepts a **loopback**
   `Host` header (`127.0.0.1:*` / `localhost:*`). The tunnel forwards your *public*
   hostname, so without intervention every real request gets a `421 Misdirected Request`.
   The `httpHostHeader: localhost:8765` line above rewrites the origin `Host` back to the
   allowed loopback value — the simplest fix, and it keeps the guard meaningful.
   *Alternatively* (or if a client also sends an `Origin` the guard rejects), allow the
   public names explicitly in `~/.thoth/.env` — these **append to**, never replace, the
   loopback defaults:

   ```bash
   # as pkm — only if you are NOT using httpHostHeader above
   THOTH_MCP_ALLOWED_HOSTS=<public-hostname>
   THOTH_MCP_ALLOWED_ORIGINS=https://<public-hostname>
   ```

   During the live verify, watch `journalctl -u thoth-mcp` for `421` and adjust whichever
   knob your setup needs.

3. **Create a Cloudflare Access application** for `<public-hostname>` (self-hosted),
   enable **Managed OAuth** in its Advanced settings, allow **localhost / loopback
   clients** with redirect `http://localhost/*` (claude.ai uses an ephemeral callback
   port), and add an Access policy permitting your identity (e.g. one-time-PIN to your
   email). Add a **cache rule** to bypass cache for `/.well-known/*` and ensure the
   "Block AI training bots" rule does **not** cover this app, or OAuth discovery is blocked.

4. **Turn on origin-side JWT enforcement (Tier 2, defense-in-depth).** Copy the Access
   application's **Audience (AUD) tag** and your **team domain**, and add to
   `~/.thoth/.env` (placeholders), then restart the unit:

   ```bash
   # as pkm — placeholders only, real values from the Cloudflare dashboard
   cat >> /home/pkm/.thoth/.env <<'EOF'
   THOTH_MCP_CF_ACCESS_TEAM_DOMAIN=<your-team>.cloudflareaccess.com
   THOTH_MCP_CF_ACCESS_AUD=<your-access-application-aud-tag>
   EOF
   chmod 600 /home/pkm/.thoth/.env
   sudo systemctl restart thoth-mcp.service
   ```

   With both set, thoth additionally validates the `Cf-Access-Jwt-Assertion` header
   (signature against `https://<your-team>.cloudflareaccess.com/cdn-cgi/access/certs`,
   `aud`, `exp`, RS256-pinned) on every request. Leave them unset to run bearer-only.

5. **Add the connector in claude.ai** → Settings → Connectors → *Add custom connector* →
   MCP server URL `https://<public-hostname>/mcp`, and complete the Access login when
   prompted. Verify discovery first with
   `curl -s https://<public-hostname>/.well-known/oauth-authorization-server`.

The env vars added above are also listed (with placeholders) in `deploy/.env.example`
and in {doc}`../reference/configuration`.
