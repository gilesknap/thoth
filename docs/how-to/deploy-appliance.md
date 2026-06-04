# Deploy the thoth appliance

This is the full, dependency-by-dependency setup for the **unattended appliance**: a small
VPS that runs the Slack capture/retrieve daemon, the Hindsight semantic index, and a few
cron jobs, all writing to a git-backed Obsidian vault. Work top-to-bottom; later steps
assume the earlier ones. When you reach the live checks, hand off to {doc}`slack-setup`
(the Slack app) and {doc}`first-light` (verifying every real boundary).

thoth is a **single-user, clean-slate** project: there is one operator, and the vault +
config + Slack app are re-created from scratch when needed. These steps describe the one
true way to set it up — there is no migration path to preserve.

## 0. What you are building

```text
               +----------------------- VPS (user: pkm) ------------------------+
Slack  <-----> | thoth-slack.service  --(127.0.0.1:8888)-->  thoth-hindsight    |
(private       |   (capture / retrieve)                      (semantic index)   |
 channel)      |        |                                                       |
               |        v   whisper | Firecrawl | Claude                        |
               | /opt/pkm-vault  --git push/pull (HTTPS)-->  pkm-vault (GitHub) |
               +----------------------------------------------------------------+
                 cron: 06:30 reindex | 07:00 daily/weekly summary | config-backup every 6h
```

- **Vault is canonical.** Knowledge is Markdown in the `pkm-vault` git repo. The Hindsight
  index is **disposable** (rebuilt from the vault). See {doc}`recovery`.
- Everything runs as an unprivileged **`pkm`** user — Hindsight's embedded Postgres
  `initdb` refuses to run as root, so every thoth unit is unprivileged.

### Prerequisites checklist

```text
- [ ] A VPS: Ubuntu 24.04+ (24.04/26.04 tested), 2 vCPU, ~8 GB RAM, 50 GB+ disk. CPU-only is fine.
- [ ] Two GitHub repos: this one (thoth) and your own empty `pkm-vault` (your knowledge).
- [ ] API keys (created in step 6): Anthropic (required), Gemini (for the index),
      Firecrawl (optional, for URL ingest), a GitHub PAT (to push the vault).
- [ ] A Slack workspace where you can create an app ({doc}`slack-setup`).
```

## 1. System packages and `uv`

As **root**:

```bash
apt-get update
apt-get install -y --no-install-recommends ffmpeg git curl ca-certificates
```

- `ffmpeg` is Whisper's audio decoder (step 5). `git`/`curl`/`ca-certificates` are for
  cloning and HTTPS.

Install [`uv`](https://docs.astral.sh/uv/) so it is on the **system** `PATH` (the systemd
units and cron all expect `uv` at `/usr/local/bin`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
uv --version
```

## 2. Create the `pkm` user

```bash
useradd --create-home --shell /bin/bash pkm
```

All the steps below that say "as `pkm`" run as that user, e.g. `sudo -u pkm -i bash -lc '…'`
or `machinectl shell pkm@` / `su - pkm`.

## 3. Clone thoth and the vault

thoth lives at `/opt/thoth`, the vault at `/opt/pkm-vault`, both **owned by `pkm`**.

```bash
install -d -o pkm -g pkm /opt/thoth /opt/pkm-vault
```

As **`pkm`**, clone this repo (public, no auth needed):

```bash
git clone https://github.com/gilesknap/thoth.git /opt/thoth
cd /opt/thoth
uv sync --extra runtime      # builds .venv, installs runtime clients + editable thoth
.venv/bin/thoth --version
```

Now the vault. You need a GitHub repo to hold it (it is your durable backup). If you do not
have one yet, create an **empty private** repo named `pkm-vault`, then:

```bash
# as pkm, with the GitHub token from step 6 exported as GH_PKM (or use `gh repo clone`):
git clone https://github.com/<owner>/pkm-vault.git /opt/pkm-vault
cd /opt/pkm-vault

# FRESH vault only — seed the spine (index.md / SCHEMA.md) and push it:
PKM_VAULT=/opt/pkm-vault /opt/thoth/.venv/bin/thoth init
git add -A && git commit -m "seed vault spine" && git push origin main
```

The clone's `origin` must be the **HTTPS** GitHub URL — thoth pushes back to that same
remote using the token from step 6 (never SSH, never a token-in-URL). If you are
**recovering** rather than starting fresh, skip `thoth init` and follow {doc}`recovery`.

## 4. Install the semantic index (Hindsight)

[Hindsight](https://hindsight.vectorize.io) is the rebuildable vector index. thoth talks to
it as a standalone **`hindsight-api`** HTTP server; install it **as `pkm`** with `uv tool`
(it lands in `~/.local/bin`):

```bash
# as pkm
uv tool install hindsight-api
hindsight-api --version
```

The server reads its LLM provider config (the backend that does embedding/fact-extraction)
from the environment; thoth's deployment uses Gemini. Put the `HINDSIGHT_API_*` env in a
locked-down EnvironmentFile that only the service reads (the `thoth-hindsight.service` unit
references it, so the Gemini key never lands in thoth's own `.env`):

```bash
# as pkm — ~/.hindsight/api.env, chmod 600
install -d -m700 ~/.hindsight
cat > ~/.hindsight/api.env <<'EOF'
HINDSIGHT_API_LLM_PROVIDER=gemini
HINDSIGHT_API_LLM_MODEL=gemini-2.5-flash
HINDSIGHT_API_LLM_API_KEY=<your-gemini-key>
EOF
chmod 600 ~/.hindsight/api.env
```

You do **not** start the server by hand — the `thoth-hindsight.service` unit (step 7) owns
its lifecycle and runs it as `hindsight-api --host 127.0.0.1 --port 8888`. The bank id is
`thoth` (a path segment on every retain/recall/forget).

```{note}
The exact provider/model strings follow the live deployment; check the
[Hindsight docs](https://hindsight.vectorize.io) if you want a different embedding backend.
thoth only requires: `hindsight-api` on PATH, bank `thoth`, and the server reachable at the
`THOTH_HINDSIGHT_BASE_URL` (default `http://127.0.0.1:8888`).
```

## 5. Install audio transcription (Whisper) — optional

Voice memos are transcribed by shelling out to a local `whisper` binary. Skip this if you
never send audio — thoth raises a clean `TranscriptionError` if `whisper` is absent.

Whisper is **not** a thoth dependency; install it in its own venv and put it on the daemon's
`PATH`. The CPU-only torch build is correct for a VPS with no GPU. As **root**:

```bash
# Use a SHARED managed Python so the unprivileged pkm daemon can exec the venv.
# (A uv-managed Python defaults under /root, which is 0700 — pkm cannot read it.)
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv python install 3.12
uv venv --python 3.12 --python-preference only-managed /opt/whisper
uv pip install --python /opt/whisper/bin/python --torch-backend=cpu openai-whisper

# Make it readable/executable by pkm and put `whisper` on the daemon PATH.
chmod -R o+rX /opt/uv-python /opt/whisper
ln -sf /opt/whisper/bin/whisper /usr/local/bin/whisper
```

Pre-download the model **as `pkm`** — the Slack daemon runs with `ProtectHome=read-only` and
cannot download at run time, so the model must already be in `pkm`'s cache:

```bash
sudo -u pkm /opt/whisper/bin/python -c 'import whisper; whisper.load_model("base")'
ls -lh /home/pkm/.cache/whisper/base.pt     # ~139 MB
```

```{note}
System Python on a current Ubuntu may be too new for torch wheels (e.g. 3.14), which is why
we pin a uv-managed 3.12. The two foot-guns above — the managed interpreter under `/root`,
and the read-only-home model download — are the whole reason this is more than `pip install
openai-whisper`.
```

## 6. Configure secrets — `~/.thoth/.env`

thoth reads its configuration from the environment; the systemd units load it from
`/home/pkm/.thoth/.env` via `EnvironmentFile`. Copy the template and lock it down:

```bash
# as pkm
install -d -m700 /home/pkm/.thoth
install -m600 /opt/thoth/deploy/.env.example /home/pkm/.thoth/.env
$EDITOR /home/pkm/.thoth/.env        # fill in real values, then keep it OUT of git
```

Fill in these variables. **Where to get each key:**

| Variable | Required? | Where to get it / what it is |
| --- | --- | --- |
| `PKM_VAULT` | yes | Vault path — `/opt/pkm-vault`. |
| `OBSIDIAN_VAULT_NAME` | yes | The vault folder name (`pkm-vault`); used to build `obsidian://` links. |
| `THOTH_HOME` | yes | `/home/pkm/.thoth` (state, manifest). |
| `THOTH_HINDSIGHT_BASE_URL` | no | Base URL of the `hindsight-api` server (step 4). Default `http://127.0.0.1:8888`. |
| `THOTH_HINDSIGHT_BANK` | yes | `thoth`. |
| `ANTHROPIC_API_KEY` | **yes** | [console.anthropic.com](https://console.anthropic.com) → **Settings → API Keys**. Powers classify / curate. |
| `ANTHROPIC_MODEL` | no | Override the default model for all calls. `THOTH_ANALYSE_MODEL` (vision), `THOTH_DIAGRAM_MODEL` (Excalidraw, worth an Opus), and `THOTH_INTENT_MODEL` (the intent gate, a cheap Haiku) override per-call. |
| `THOTH_IMAGE_RESIZE_THRESHOLD_BYTES` | no | Captured images larger than this are downscaled (longest edge capped at ~1568px, aspect ratio preserved) before they are stored in `raw/assets/` *and* before they reach the vision model. Default `2097152` (2 MB); a non-positive value disables resizing. |
| `GEMINI_API_KEY` | yes (for the index) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey). The **same key** you gave Hindsight in step 4; it powers embeddings + fact-extraction. |
| `FIRECRAWL_API_KEY` | no | [firecrawl.dev](https://www.firecrawl.dev) → dashboard → API keys. URL→Markdown **extraction** for URL ingest. Blank ⇒ URLs stored without fetched content. |
| `GITHUB_PKM_VAULT_TOKEN` | yes | A GitHub **fine-grained PAT** scoped to the `pkm-vault` repo with **Contents: Read and write** ([github.com/settings/tokens](https://github.com/settings/tokens)). thoth feeds it to git as an `x-access-token` HTTPS credential to push the vault. |
| `SLACK_BOT_TOKEN` | yes | `xoxb-…` — see {doc}`slack-setup`. |
| `SLACK_APP_TOKEN` | yes | `xapp-…` (scope `connections:write`) — see {doc}`slack-setup`. |
| `SLACK_CAPTURE_CHANNEL` | yes | The private channel id (`C…`/`G…`) the bot listens in — see {doc}`slack-setup`. |
| `SLACK_ALLOWED_USERS` | yes | Your Slack **member id** (`U…`, *not* a `D…`/`C…`). Fail-closed: blank denies everyone. |
| `SLACK_SUMMARY_CHANNEL` | for `summary` | Channel/DM id the daily/weekly digest posts to. |

The **Slack** variables have their own guide — do {doc}`slack-setup` now (it creates the app
from a manifest, enables Socket Mode, mints both tokens, and creates the capture channel).

```{warning}
`~/.thoth/.env` holds every secret. It is `chmod 600`, owned by `pkm`, and **must never be
committed** to either repo. The only other place a secret lives is Hindsight's
`~/.hindsight/api.env` (the Gemini key for the index server, step 4). Keep a copy of both in
your password manager — that is the *only* backup of your secrets ({doc}`recovery`).
```

## 7. Install and enable the systemd units + cron

The unit files and crontab ship in `deploy/`. As **root**:

```bash
cp /opt/thoth/deploy/thoth-hindsight.service /etc/systemd/system/
cp /opt/thoth/deploy/thoth-slack.service     /etc/systemd/system/
systemctl daemon-reload

# Index first (the Slack daemon orders after it and only TALKS to it).
systemctl enable --now thoth-hindsight.service
systemctl enable --now thoth-slack.service

systemctl status thoth-hindsight.service thoth-slack.service --no-pager
```

The units are pre-hardened (`ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`,
a narrow `ReadWritePaths`) and run as `pkm`. They read secrets only from
`/home/pkm/.thoth/.env`; nothing sensitive is in the tracked unit files.

Install the cron jobs (reindex, summaries, config backup) for `pkm`, and create their logs:

```bash
crontab -u pkm /opt/thoth/deploy/crontab
install -d -o pkm -g pkm /var/log     # logs: /var/log/thoth-*.log (paths in deploy/crontab)
```

| When (Europe/London) | Job |
| --- | --- |
| 06:30 daily | `thoth reindex` (incremental) + optional Hindsight snapshot |
| 07:00 daily | `thoth summary daily` → Slack |
| 07:00 Monday | `thoth summary weekly` → Slack |
| every 6 h | `config-backup.sh` (push the thoth repo; `.env` stays gitignored) |

## 7a. The MCP server

Set up the bearer-authenticated MCP HTTP socket (`thoth-mcp.service`) so Claude Code and
claude.ai can reach the vault's `pkm_*` tools — its own recipe:
{doc}`mcp-server-setup` (the systemd unit, the bearer key, and the cloudflared +
Cloudflare-Access wiring for the claude.ai connector).

## 8. First light

The first time the box hits the real services is the first time those seams run for real.
Work through {doc}`first-light` — one happy-path check per boundary (Anthropic, Hindsight,
Slack, MCP, Firecrawl, cron), plus the one-command live-smoke suite. Post a note, a URL,
and a voice memo in the capture channel and watch them land in the vault.

## Upgrading / redeploying a change

To move the box to a new commit (or a branch you are verifying), as **`pkm`** in
`/opt/thoth`:

```bash
git pull                       # or: git fetch && git checkout <branch>
uv sync --extra runtime        # no-op if deps unchanged; rebuilds editable metadata
sudo systemctl restart thoth-slack.service
```

Confirm the deploy by the **source-tree git HEAD**, not the startup-log version string
(which can lag until `uv sync` rebuilds the editable metadata). For verifying boundary/SDK
changes against the real services before merge, see the `thoth-testing` skill.

## See also

- {doc}`slack-setup` — create the Slack app and wire the tokens.
- {doc}`first-light` — verify every live boundary after deploy.
- {doc}`recovery` — rebuild the box from the two git repos + secrets.
- {doc}`../explanations/architecture` — how the pieces fit and why.
