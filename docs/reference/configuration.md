# Configuration reference

thoth is configured entirely through environment variables, optionally seeded from a
`.env` file at `$THOTH_HOME/.env` (chmod 600, never committed). The real environment wins
over the `.env` file, which wins over the documented defaults; `load_config` reads them
once at process entry and never mutates the environment. `src/thoth/config.py` is the
single source of truth, and `deploy/.env.example` is the copy-paste starting point.

Only **`PKM_VAULT`** is hard-required. Everything else has a default or is needed only for
the feature it powers (Slack tokens to run the daemon, an Anthropic key to make LLM calls,
and so on). Blank counts as unset.

## Core / vault

| Variable | Meaning | Default |
|---|---|---|
| `PKM_VAULT` | Absolute path to the Obsidian vault. **Required.** | — |
| `OBSIDIAN_VAULT_NAME` | Vault name used in `obsidian://` deep links. | `pkm-vault` |
| `THOTH_HOME` | thoth home dir; also the default `.env` and `state.db` location. | `~/.thoth` |
| `THOTH_LOG_LEVEL` | Log level for the daemon (`DEBUG` for the full pipeline trail). | `INFO` |

## Anthropic + models

| Variable | Meaning | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (required for any LLM call). | — |
| `ANTHROPIC_MODEL` | Default model for every call that does not pin its own. | `claude-sonnet-4-6` |
| `THOTH_ANALYSE_MODEL` | Override for the vision analyse/OCR/transcription call. | unset → `ANTHROPIC_MODEL` |
| `THOTH_DIAGRAM_MODEL` | Override for the Excalidraw reconstruction call (wants spatial reasoning). | unset → `ANTHROPIC_MODEL` |
| `THOTH_INTENT_MODEL` | Override for the free-text intent gate (a one-shot routing call). | unset → a cheap Haiku |

## Budgets + image handling

| Variable | Meaning | Default |
|---|---|---|
| `THOTH_DAILY_LLM_BUDGET` | Combined daily LLM call cap (appliance + Gemini extraction). Non-positive disables. | `200` |
| `THOTH_IMAGE_RESIZE_THRESHOLD_BYTES` | Downscale captured images larger than this before storage + vision. Non-positive disables. | `2097152` (2 MB) |
| `THOTH_MAX_ANALYSE_IMAGES` | Cap on images sent to one multi-image vision call (extras still saved + embedded). Non-positive = no cap. | `6` |

## Slack

| Variable | Meaning | Default |
|---|---|---|
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`). Required for `thoth slack`. | — |
| `SLACK_APP_TOKEN` | App-level token (`xapp-…`) for Socket Mode. Required for `thoth slack`. | — |
| `SLACK_CAPTURE_CHANNEL` | Private channel id the daemon listens/replies in. Required for `thoth slack`. | — |
| `SLACK_SUMMARY_CHANNEL` | Channel id for the daily/weekly digest. | — |
| `SLACK_ALERT_CHANNEL` | Channel/DM id for unattended error + heartbeat alerts. | unset → first `SLACK_ALLOWED_USERS` id |
| `SLACK_ALLOWED_USERS` | Allowed member id(s) (`U…`, **not** a `D…` DM id), comma-separated. | — |

## Web research + semantic index

| Variable | Meaning | Default |
|---|---|---|
| `FIRECRAWL_API_KEY` | Firecrawl URL→Markdown key. Blank = vault-only. | — |
| `GEMINI_API_KEY` | Gemini key for the Hindsight semantic index (embeddings + fact-extraction). | — |
| `THOTH_HINDSIGHT_BINARY` | Path to the `hindsight-embed` CLI. | `hindsight` |
| `THOTH_HINDSIGHT_PROFILE` | Named Hindsight profile (carries the index LLM key/port). | — |
| `THOTH_HINDSIGHT_BANK` | Hindsight bank id (positional on retain/recall). | `thoth` |

## Vault git sync

| Variable | Meaning | Default |
|---|---|---|
| `GITHUB_PKM_VAULT_TOKEN` | GitHub token (`ghp_…`) for two-way vault git sync. | — |

## MCP HTTP transport

Needed to run the MCP server ({doc}`../how-to/mcp-server-setup`). The server **fails fast**
if `THOTH_MCP_API_KEYS` is unset — it never binds an unauthenticated socket.

| Variable | Meaning | Default |
|---|---|---|
| `THOTH_MCP_API_KEYS` | Bearer key(s) for HTTP requests, comma-separated for rotation. **Required for the socket.** | — |
| `THOTH_MCP_CF_ACCESS_TEAM_DOMAIN` | Cloudflare Access team domain (Tier 2 JWT; both Cf vars needed to enable). | — |
| `THOTH_MCP_CF_ACCESS_AUD` | Cloudflare Access application AUD tag (Tier 2 JWT). | — |
| `THOTH_MCP_ALLOWED_HOSTS` | Extra `Host` values past FastMCP's DNS-rebinding guard (appended to loopback). | — |
| `THOTH_MCP_ALLOWED_ORIGINS` | Extra `Origin` values (with scheme) past the guard (appended to loopback). | — |
