# Configuration reference

thoth is configured entirely through environment variables, optionally seeded from a `.env` file at `$THOTH_HOME/.env`, which is chmod 600 and never committed.

The real environment wins over the `.env` file, which wins over the documented defaults. `load_config` reads them once at process entry and never mutates the environment.

`src/thoth/config/` is the single source of truth, and `deploy/.env.example` is the copy-paste starting point.

Only `PKM_VAULT` is hard-required. Everything else has a default, or is needed only for the feature it powers, such as the Slack tokens to run the daemon or an Anthropic key to make LLM calls. Blank counts as unset.

## Core and vault

| Variable | Meaning | Default |
|---|---|---|
| `PKM_VAULT` | Absolute path to the Obsidian vault. **Required.** | none |
| `OBSIDIAN_VAULT_NAME` | Vault name used in `obsidian://` deep links. | `pkm-vault` |
| `THOTH_HOME` | thoth home dir, and the default `.env` and `state.db` location. | `~/.thoth` |
| `THOTH_LOG_LEVEL` | Log level for the daemon. `DEBUG` gives the full pipeline trail. | `INFO` |

## Anthropic and models

| Variable | Meaning | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key, required for any LLM call. | none |
| `ANTHROPIC_MODEL` | Default model for every call that does not pin its own. | `claude-sonnet-4-6` |
| `THOTH_ANALYSE_MODEL` | Override for the vision analyse, OCR and transcription call. | unset → `ANTHROPIC_MODEL` |
| `THOTH_DIAGRAM_MODEL` | Override for the Excalidraw reconstruction call, which wants spatial reasoning. | unset → `ANTHROPIC_MODEL` |
| `THOTH_INTENT_MODEL` | Override for the free-text intent gate, a one-shot routing call. | unset → a cheap Haiku |

## Budgets and image handling

| Variable | Meaning | Default |
|---|---|---|
| `THOTH_DAILY_LLM_BUDGET` | Combined daily LLM call cap, covering the appliance and Hindsight's Claude extraction, since both are Anthropic. Non-positive disables it. | `200` |
| `THOTH_IMAGE_RESIZE_THRESHOLD_BYTES` | Downscale captured images larger than this before storage and vision. Non-positive disables it. | `2097152` (2 MB) |
| `THOTH_MAX_ANALYSE_IMAGES` | Cap on images sent to one multi-image vision call. Extras are still saved and embedded. Non-positive means no cap. | `6` |

## Slack

| Variable | Meaning | Default |
|---|---|---|
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`). Required for `thoth slack`. | none |
| `SLACK_APP_TOKEN` | App-level token (`xapp-…`) for Socket Mode. Required for `thoth slack`. | none |
| `SLACK_CAPTURE_CHANNEL` | Private channel id the daemon listens and replies in. Required for `thoth slack`. | none |
| `SLACK_SUMMARY_CHANNEL` | Channel id for the daily and weekly digest. | none |
| `SLACK_ALERT_CHANNEL` | Channel or DM id for unattended error and heartbeat alerts. | unset → first `SLACK_ALLOWED_USERS` id |
| `SLACK_ALLOWED_USERS` | Allowed member ids (`U…`, **not** a `D…` DM id), comma-separated. | none |

## Web research and semantic index

| Variable | Meaning | Default |
|---|---|---|
| `EXA_API_KEY` | Exa web-search key. With `FIRECRAWL_API_KEY` it powers the blended `research:` path. Blank means vault-only. | none |
| `FIRECRAWL_API_KEY` | Firecrawl URL to Markdown key. Blank means vault-only. | none |
| `THOTH_HINDSIGHT_BASE_URL` | Base URL of the standalone `hindsight-api` HTTP server. | `http://127.0.0.1:8888` |
| `THOTH_HINDSIGHT_BANK` | Hindsight bank id, a path segment on retain, recall and forget. | `thoth` |

Hindsight's own backend config is not a thoth env var. That covers the LLM provider, model and key for fact-extraction, the local embeddings, and the embedded-Postgres `pg0://` URL.

It lives in a dedicated file read only by `thoth-hindsight.service`, and `deploy/hindsight-api.env.example` is the template.

## Vault git sync

| Variable | Meaning | Default |
|---|---|---|
| `GITHUB_PKM_VAULT_TOKEN` | GitHub token (`ghp_…`) for two-way vault git sync. | none |

## MCP HTTP transport

These are needed to run the MCP server ({doc}`../how-to/mcp-server-setup`).

```{warning}
The server fails fast if `THOTH_MCP_API_KEYS` is unset. It never binds an unauthenticated
socket.
```

| Variable | Meaning | Default |
|---|---|---|
| `THOTH_MCP_API_KEYS` | Bearer keys for HTTP requests, comma-separated for rotation. **Required for the socket.** | none |
| `THOTH_MCP_CF_ACCESS_TEAM_DOMAIN` | Cloudflare Access team domain (Tier 2 JWT). Both Cf vars are needed to enable it. | none |
| `THOTH_MCP_CF_ACCESS_AUD` | Cloudflare Access application AUD tag (Tier 2 JWT). | none |
| `THOTH_MCP_ALLOWED_HOSTS` | Extra `Host` values past FastMCP's DNS-rebinding guard, appended to loopback. | none |
| `THOTH_MCP_ALLOWED_ORIGINS` | Extra `Origin` values, with scheme, past the guard, appended to loopback. | none |
