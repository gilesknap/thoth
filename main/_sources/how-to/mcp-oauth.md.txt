# Connect with GitHub OAuth

The MCP server can authenticate clients with a built-in **OAuth 2.1 + PKCE**
flow backed by **GitHub** as the identity provider. claude.ai and Claude Code
walk the user through a GitHub login, thoth checks the GitHub username against an
allow-list, and then issues its own short-lived JWT access token that the client
sends as a bearer on every request.

This is an alternative to fronting the server with Cloudflare Access
({doc}`mcp-server-setup`) — thoth is the OAuth authorization server itself, so
no Cloudflare tunnel or Access application is required for the OAuth path.

All hostnames below are **placeholders** — substitute your own and never commit
the real values.

## 1. Create a GitHub OAuth App

In GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**:

- **Application name** — anything, e.g. `thoth-mcp`.
- **Homepage URL** — `https://<host>` (your server's public base URL).
- **Authorization callback URL** — `https://<host>/callback`.

Register the app, then **generate a client secret**. Keep the **Client ID** and
**Client secret** for the next step.

## 2. Set the OAuth env vars

Add these to `~/.thoth/.env` (as **`pkm`** on the appliance), then restart the
unit. OAuth activates only when all four required values are present:

```bash
# as pkm — real values from the GitHub OAuth App and openssl
cat >> /home/pkm/.thoth/.env <<'EOF'
GITHUB_OAUTH_CLIENT_ID=<github-oauth-app-client-id>
GITHUB_OAUTH_CLIENT_SECRET=<github-oauth-app-client-secret>
THOTH_JWT_SIGNING_SECRET=<paste-output-of-openssl-rand-hex-32>
THOTH_ALLOWED_GITHUB_USERS=<your-github-login>,<another-github-login>
THOTH_OAUTH_SERVER_URL=https://<host>
EOF
chmod 600 /home/pkm/.thoth/.env
sudo systemctl restart thoth-mcp.service
```

| Variable | Purpose |
|---|---|
| `GITHUB_OAUTH_CLIENT_ID` | Client ID of the GitHub OAuth App from section 1. |
| `GITHUB_OAUTH_CLIENT_SECRET` | Client secret of that app. |
| `THOTH_JWT_SIGNING_SECRET` | Secret thoth uses to sign its own access-token JWTs (HS256). Generate with `openssl rand -hex 32`. |
| `THOTH_ALLOWED_GITHUB_USERS` | Comma-separated GitHub logins allowed to connect. A successful GitHub login whose username is not in this list is rejected. |
| `THOTH_OAUTH_SERVER_URL` | The server's public base URL (`https://<host>`), used to build the OAuth discovery and redirect endpoints. |

The signing secret and the client secret are sensitive — keep them in
`~/.thoth/.env` (mode `600`) and never commit them.

## 3. Connect from claude.ai

In claude.ai → **Settings → Connectors** (or a Project's
**Settings → Integrations**) → *Add custom connector*:

- **MCP server URL** — `https://<host>/mcp`.

When you connect, claude.ai opens the GitHub login, you authorize the OAuth App,
and — if your GitHub username is in `THOTH_ALLOWED_GITHUB_USERS` — the connector
is linked.

## 4. Connect from Claude Code

```bash
# from your laptop — replace <host>
claude mcp add --transport http thoth https://<host>/mcp
```

Claude Code triggers the same GitHub OAuth flow in your browser on first use; no
bearer header is needed.

## 5. API-key auth still works in parallel

The static bearer-key path ({doc}`mcp-server-setup`, `THOTH_MCP_API_KEYS`)
remains available alongside OAuth. The server accepts **either** a valid
`THOTH_MCP_API_KEYS` bearer **or** a thoth-issued OAuth JWT, so scripts and cron
jobs can keep using a long-lived API key while interactive clients use OAuth:

```bash
claude mcp add --transport http thoth https://<host>/mcp \
  --header "Authorization: Bearer <key>"
```

The env vars above are also listed (with placeholders) in `deploy/.env.example`
and in {doc}`../reference/configuration`.
