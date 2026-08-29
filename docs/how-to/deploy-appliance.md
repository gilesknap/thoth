# Deploy the thoth appliance

This is the full, dependency-by-dependency setup for the unattended appliance. That is a small VPS running the Slack capture and retrieve daemon, the Hindsight semantic index, and a few cron jobs, all writing to a git-backed Obsidian vault.

Work top to bottom, because later steps assume the earlier ones. When you reach the live checks, hand off to {doc}`slack-setup` for the Slack app and {doc}`first-light` for verifying every real boundary.

thoth is a single-user, clean-slate project. There is one operator, and the vault, the config and the Slack app are re-created from scratch when needed, so these steps describe the one true way to set it up and there is no migration path to preserve.

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

- **Vault is canonical.** Knowledge is Markdown in the `pkm-vault` git repo, and the Hindsight index is disposable because it is rebuilt from the vault. See {doc}`recovery`.
- Everything runs as the unprivileged `pkm` user. Hindsight's embedded Postgres `initdb` refuses to run as root, so every thoth unit is unprivileged.

### Prerequisites checklist

```text
- [ ] A VPS: Ubuntu 24.04+ (24.04/26.04 tested), 2 vCPU, ~8 GB RAM, 50 GB+ disk. CPU-only is fine.
- [ ] Two GitHub repos: this one (thoth) and your own empty `pkm-vault` (your knowledge).
- [ ] API keys (created in step 6): Anthropic (required, used by both thoth and the index),
      Exa + Firecrawl (optional, for research/URL ingest), a GitHub PAT (to push the vault).
- [ ] A Slack workspace where you can create an app ({doc}`slack-setup`).
```

## 1. System packages and `uv`

As **root**:

```bash
apt-get update
apt-get install -y --no-install-recommends ffmpeg git curl ca-certificates
```

`ffmpeg` is Whisper's audio decoder in step 5, and `git`, `curl` and `ca-certificates` are for cloning and HTTPS.

Install [`uv`](https://docs.astral.sh/uv/) so that it is on the system `PATH`, because the systemd units and cron all expect `uv` at `/usr/local/bin`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
uv --version
```

## 2. Create the `pkm` user

```bash
useradd --create-home --shell /bin/bash pkm
```

Every step below that says "as `pkm`" runs as that user, for example through `sudo -u pkm -i bash -lc '…'`, `machinectl shell pkm@` or `su - pkm`.

## 3. Clone thoth and the vault

thoth lives at `/opt/thoth` and the vault at `/opt/pkm-vault`, both owned by `pkm`.

```bash
install -d -o pkm -g pkm /opt/thoth /opt/pkm-vault
```

As **`pkm`**, clone this repo, which is public and needs no auth:

```bash
git clone https://github.com/gilesknap/thoth.git /opt/thoth
cd /opt/thoth
uv sync --extra runtime      # builds .venv, installs runtime clients + editable thoth
.venv/bin/thoth --version
```

Now the vault. You need a GitHub repo to hold it, because it is your durable backup. If you do not have one yet, create an empty private repo named `pkm-vault`, then:

```bash
# as pkm, with the GitHub token from step 6 exported as GH_PKM (or use `gh repo clone`):
git clone https://github.com/<owner>/pkm-vault.git /opt/pkm-vault
cd /opt/pkm-vault

# FRESH vault only: seed the spine (index.md / SCHEMA.md) and push it:
PKM_VAULT=/opt/pkm-vault /opt/thoth/.venv/bin/thoth init
git add -A && git commit -m "seed vault spine" && git push origin main
```

The clone's `origin` must be the HTTPS GitHub URL, because thoth pushes back to that same remote using the token from step 6, never over SSH and never with a token in the URL.

If you are recovering rather than starting fresh, skip `thoth init` and follow {doc}`recovery`.

## 4. Install the semantic index (Hindsight)

[Hindsight](https://hindsight.vectorize.io) is the rebuildable vector index. thoth talks to it as a standalone `hindsight-api` HTTP server, so install it as `pkm` with `uv tool`, which lands it in `~/.local/bin`:

```bash
# as pkm
uv tool install hindsight-api
hindsight-api --version
```

The server reads its backend config from the environment. Fact-extraction runs on Anthropic Claude, the same vendor thoth uses, so reuse your `ANTHROPIC_API_KEY` value. Embeddings run locally on the box with no external embedding calls, and the database is hindsight-api's own embedded Postgres addressed by the `pg0://` scheme, so there is no separate DB service to run.

Put the `HINDSIGHT_API_*` env in a locked-down EnvironmentFile that only the service reads. The `thoth-hindsight.service` unit references `<THOTH_HOME>/hindsight-api.env`, so these keys never land in thoth's own `.env`.

Copy the template and fill it in:

```bash
# as pkm: installs at <THOTH_HOME>/hindsight-api.env, chmod 600
install -m600 /opt/thoth/deploy/hindsight-api.env.example /home/pkm/.thoth/hindsight-api.env
$EDITOR /home/pkm/.thoth/hindsight-api.env   # set HINDSIGHT_API_LLM_API_KEY = your Anthropic key
```

The template ships the live values `HINDSIGHT_API_LLM_PROVIDER=anthropic`, `HINDSIGHT_API_LLM_MODEL=claude-haiku-4-5`, `HINDSIGHT_API_EMBEDDINGS_PROVIDER=local` and `HINDSIGHT_API_DATABASE_URL=pg0://hindsight-embed-thoth`. Only the API key needs filling in.

You do not start the server by hand. The `thoth-hindsight.service` unit from step 7 owns its lifecycle and runs it as `hindsight-api --host 127.0.0.1 --port 8888`. The bank id is `thoth`, which is a path segment on every retain, recall and forget.

```{note}
The provider, model and embeddings strings above follow the live deployment, and the LLM
provider is configurable (check the [Hindsight docs](https://hindsight.vectorize.io) for
alternatives). thoth requires only three things: `hindsight-api` on PATH, bank `thoth`, and
the server reachable at the `THOTH_HINDSIGHT_BASE_URL` (default `http://127.0.0.1:8888`).
```

## 5. Install audio transcription (Whisper), optional

Voice memos are transcribed by shelling out to a local `whisper` binary. Skip this if you never send audio, because thoth raises a clean `TranscriptionError` when `whisper` is absent.

Whisper is not a thoth dependency, so install it in its own venv and put it on the daemon's `PATH`. The CPU-only torch build is correct for a VPS with no GPU.

As **root**:

```bash
# Use a SHARED managed Python so the unprivileged pkm daemon can exec the venv.
# (A uv-managed Python defaults under /root, which is 0700, so pkm cannot read it.)
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv python install 3.12
uv venv --python 3.12 --python-preference only-managed /opt/whisper
uv pip install --python /opt/whisper/bin/python --torch-backend=cpu openai-whisper

# Make it readable/executable by pkm and put `whisper` on the daemon PATH.
chmod -R o+rX /opt/uv-python /opt/whisper
ln -sf /opt/whisper/bin/whisper /usr/local/bin/whisper
```

Pre-download the model as `pkm`. The Slack daemon runs with `ProtectHome=read-only` and cannot download at run time, so the model must already be in `pkm`'s cache:

```bash
sudo -u pkm /opt/whisper/bin/python -c 'import whisper; whisper.load_model("base")'
ls -lh /home/pkm/.cache/whisper/base.pt     # ~139 MB
```

```{note}
System Python on a current Ubuntu may be too new for torch wheels, 3.14 for example, which
is why we pin a uv-managed 3.12. The two foot-guns above, the managed interpreter under
`/root` and the read-only-home model download, are the whole reason this is more than
`pip install openai-whisper`.
```

## 6. Configure secrets in `~/.thoth/.env`

thoth reads its configuration from the environment, and the systemd units load it from `/home/pkm/.thoth/.env` via `EnvironmentFile`.

Copy the template and lock it down:

```bash
# as pkm
install -d -m700 /home/pkm/.thoth
install -m600 /opt/thoth/deploy/.env.example /home/pkm/.thoth/.env
$EDITOR /home/pkm/.thoth/.env        # fill in real values, then keep it OUT of git
```

Fill in these variables, and here is where to get each key:

| Variable | Required? | Where to get it / what it is |
| --- | --- | --- |
| `PKM_VAULT` | yes | The vault path, `/opt/pkm-vault`. |
| `OBSIDIAN_VAULT_NAME` | yes | The vault folder name (`pkm-vault`), used to build `obsidian://` links. |
| `THOTH_HOME` | yes | `/home/pkm/.thoth` (state, manifest). |
| `THOTH_HINDSIGHT_BASE_URL` | no | Base URL of the `hindsight-api` server (step 4). Default `http://127.0.0.1:8888`. |
| `THOTH_HINDSIGHT_BANK` | yes | `thoth`. |
| `ANTHROPIC_API_KEY` | **yes** | [console.anthropic.com](https://console.anthropic.com), under **Settings → API Keys**. Powers classify and curate, and the **same value** goes in Hindsight's `hindsight-api.env` (step 4) for fact-extraction. |
| `ANTHROPIC_MODEL` | no | Override the default model for all calls. `THOTH_ANALYSE_MODEL` (vision), `THOTH_DIAGRAM_MODEL` (Excalidraw, worth an Opus) and `THOTH_INTENT_MODEL` (the intent gate, a cheap Haiku) override per call. |
| `THOTH_IMAGE_RESIZE_THRESHOLD_BYTES` | no | Captured images larger than this are downscaled, with the longest edge capped at about 1568px and the aspect ratio preserved, before they are stored in `raw/assets/` *and* before they reach the vision model. Default `2097152` (2 MB); a non-positive value disables resizing. |
| `EXA_API_KEY` | no | [exa.ai](https://exa.ai), dashboard, API keys. Web search for the blended `research:` path. Blank means vault-only. |
| `FIRECRAWL_API_KEY` | no | [firecrawl.dev](https://www.firecrawl.dev), dashboard, API keys. URL to Markdown **extraction** for URL ingest. Blank means URLs are stored without fetched content. |
| `GITHUB_PKM_VAULT_TOKEN` | yes | A GitHub **fine-grained PAT** scoped to the `pkm-vault` repo with **Contents: Read and write** ([github.com/settings/tokens](https://github.com/settings/tokens)). thoth feeds it to git as an `x-access-token` HTTPS credential to push the vault. |
| `SLACK_BOT_TOKEN` | yes | `xoxb-…`, see {doc}`slack-setup`. |
| `SLACK_APP_TOKEN` | yes | `xapp-…` with scope `connections:write`, see {doc}`slack-setup`. |
| `SLACK_CAPTURE_CHANNEL` | yes | The private channel id (`C…` or `G…`) the bot listens in, see {doc}`slack-setup`. |
| `SLACK_ALLOWED_USERS` | yes | Your Slack **member id** (`U…`, *not* a `D…` or `C…`). It is fail-closed, so blank denies everyone. |
| `SLACK_SUMMARY_CHANNEL` | for `summary` | The channel or DM id the daily and weekly digest posts to. |

The Slack variables have their own guide, so do {doc}`slack-setup` now. It creates the app from a manifest, enables Socket Mode, mints both tokens and creates the capture channel.

```{warning}
`~/.thoth/.env` holds every secret. It is `chmod 600`, owned by `pkm`, and **must never be
committed** to either repo. The only other place a secret lives is Hindsight's
`<THOTH_HOME>/hindsight-api.env`, the Anthropic key for the index server from step 4. Keep a
copy of both in your password manager, because that is the *only* backup of your secrets
({doc}`recovery`).
```

## 7. Install and enable the systemd units and cron

The unit files and crontab ship in `deploy/`. As **root**:

```bash
cp /opt/thoth/deploy/thoth-hindsight.service /etc/systemd/system/
cp /opt/thoth/deploy/thoth-slack.service     /etc/systemd/system/
systemctl daemon-reload

# REQUIRED: let pkm's IPC survive logout. Hindsight's embedded Postgres runs as
# pkm, and systemd's RemoveIPC=yes (the default) deletes ALL IPC owned by a user
# the moment their last login session ends, including Postgres's /dev/shm
# shared-memory segment, out from under the still-running postmaster. Every new DB
# connection then fails (`could not open shared memory segment "/PostgreSQL.NNNN":
# No such file or directory`) and Slack ingests fail at the indexing phase. A
# lingering user is exempt from RemoveIPC, so enable it before starting the unit:
loginctl enable-linger pkm

# Index first (the Slack daemon orders after it and only TALKS to it).
systemctl enable --now thoth-hindsight.service
systemctl enable --now thoth-slack.service

systemctl status thoth-hindsight.service thoth-slack.service --no-pager
```

The units are pre-hardened with `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp` and a narrow `ReadWritePaths`, and they run as `pkm`. They read secrets only from `/home/pkm/.thoth/.env`, so nothing sensitive is in the tracked unit files.

```{important}
`loginctl enable-linger pkm` above is not optional. Without it the embedded Postgres keeps
running but stops accepting new connections as soon as anyone who `su`'d or SSH'd in as
`pkm` logs out, and the only visible symptom is Slack ingests silently failing to index.

Belt and braces, you can also disable the behaviour host-wide with a drop-in,
`printf '[Login]\nRemoveIPC=no\n' > /etc/systemd/logind.conf.d/10-removeipc.conf` then
`systemctl restart systemd-logind`, but lingering alone is sufficient.

If you ever hit the error on a running box, recover with
`systemctl restart thoth-hindsight.service`, which makes the postmaster recreate its
segment, then run `thoth reindex` to backfill any pages that failed to index.
```

Install the cron jobs for reindex, summaries and config backup for `pkm`, and create their logs:

```bash
crontab -u pkm /opt/thoth/deploy/crontab
install -d -o pkm -g pkm /var/log     # logs: /var/log/thoth-*.log (paths in deploy/crontab)
```

| When (Europe/London) | Job |
| --- | --- |
| 06:30 daily | `thoth reindex` (incremental) plus an optional Hindsight snapshot |
| 07:00 daily | `thoth summary daily`, posted to Slack |
| 07:00 Monday | `thoth summary weekly`, posted to Slack |
| every 6 h | `config-backup.sh`, which pushes the thoth repo while `.env` stays gitignored |

## 7a. The MCP server

Set up the bearer-authenticated MCP HTTP socket (`thoth-mcp.service`) so that Claude Code and claude.ai can reach the vault's `pkm_*` tools.

That has its own recipe in {doc}`mcp-server-setup`, covering the systemd unit, the bearer key, and the cloudflared and Cloudflare Access wiring for the claude.ai connector.

## 8. First light

The first time the box hits the real services is the first time those seams run for real.

Work through {doc}`first-light`, which is one happy-path check per boundary (Anthropic, Hindsight, Slack, MCP, Firecrawl, cron) plus the one-command live-smoke suite. Post a note, a URL and a voice memo in the capture channel and watch them land in the vault.

## Upgrading or redeploying a change

To move the box to a new commit, or to a branch you are verifying, run this as `pkm` in `/opt/thoth`:

```bash
git pull                       # or: git fetch && git checkout <branch>
uv sync --extra runtime        # no-op if deps unchanged; rebuilds editable metadata
sudo systemctl restart thoth-slack.service
```

```{warning}
Confirm the deploy by the source-tree git HEAD, not by the startup-log version string, which
can lag until `uv sync` rebuilds the editable metadata.
```

For verifying boundary and SDK changes against the real services before merge, see the `thoth-testing` skill.

## See also

- {doc}`slack-setup` to create the Slack app and wire the tokens.
- {doc}`first-light` to verify every live boundary after a deploy.
- {doc}`recovery` to rebuild the box from the two git repos and your secrets.
- {doc}`../explanations/architecture` for how the pieces fit and why.
