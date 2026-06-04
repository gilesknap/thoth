# First-light smoke checklist

CI exercises every external boundary against an injected fake -- no real Slack,
Anthropic, Hindsight, MCP, Exa, Firecrawl, Postgres, or git remote is touched (SPEC
section 12). So the **first time** the appliance runs against the real services (on the
VPS, after deploy) is the first time those seams are exercised for real. This page is the
runbook for that *first light*: one happy-path check per real boundary, plus the single
command that runs the opt-in live smoke suite.

Work through it top-to-bottom on the VPS, as the `pkm` user, with `~/.thoth/.env`
populated and the venv on `PATH` (`source /opt/thoth/.venv/bin/activate`). Each step is a
**one-shot** check; none of them mutate canonical knowledge beyond a single throwaway page
you can delete afterwards.

## 0. Prerequisites

```text
- [ ] /opt/thoth checked out, `uv sync --extra runtime` run (the runtime clients installed)
- [ ] /opt/pkm-vault cloned and PKM_VAULT points at it
- [ ] ~/.thoth/.env present (chmod 600) with the real keys/tokens
- [ ] `thoth --version` prints a version
```

## 1. Anthropic -- a trivial classify returns valid JSON

The cheapest real LLM round-trip is the ingest *classify* call: one Claude message that
must come back as a JSON routing object. If the model id, key, or system prompt is wrong
this fails loud here rather than mid-capture.

```console
$ THOTH_LIVE_SMOKE=1 uv run pytest -m live -k anthropic
```

Expected: the live test sends a one-line note, gets back a `Classification` with a
non-empty `type` and `slug`, and passes. A `ConfigError` means `ANTHROPIC_API_KEY` is
unset; a 404 means `ANTHROPIC_MODEL` is a retired id (use the dated fallback).

## 2. Hindsight -- retain then recall round-trips, with tag-provenance recoverable

Provenance is **tag-keyed** (SPEC section 8): `retain` carries the vault-relative path as
a `rel` tag (the in-band `SOURCE:` sentinel is only a fallback, because LLM
fact-extraction can split a page into atomic facts and strand the sentinel). This check
confirms the round-trip *and* that recall recovers the path.

```console
$ hindsight memory retain thoth "SOURCE: concepts/first-light.md

first light smoke probe" --tags concepts,concepts/first-light.md
$ hindsight memory recall thoth "first light smoke probe" -o json
```

Expected: the recall JSON contains a hit whose tags include
`concepts/first-light.md`, so `thoth.hindsight.parse_recall` recovers that path
(tags-first). Confirm the installed binary name/verbs match (`THOTH_HINDSIGHT_BINARY`,
`THOTH_HINDSIGHT_BANK`) -- the VPS may still have `hindsight-embed` installed. The live
suite does the same round-trip through `thoth.hindsight.Hindsight`:

```console
$ THOTH_LIVE_SMOKE=1 uv run pytest -m live -k hindsight
```

## 3. Slack -- Socket Mode connects and a channel post round-trips capture+reply

This step assumes the Slack app already exists, the tokens are in `~/.thoth/.env`, and the
bot has been `/invite`d to the private capture channel (`SLACK_CAPTURE_CHANNEL`). If you
have not set that up yet, do {doc}`slack-setup` first (it creates the app from an embedded
manifest, turns on Socket Mode, creates the channel, and lists the env vars thoth reads).

Start the daemon, then post in the capture channel from an allow-listed account.

```console
$ thoth slack
```

Then, in the capture channel (from a `SLACK_ALLOWED_USERS` account):

```text
- [ ] post "capture: first light test" -> bot replies IN A THREAD with an obsidian:// link
- [ ] the page lands in the vault (check `git log` in /opt/pkm-vault)
- [ ] post "research: what is first light" -> bot replies in-thread and offers to save
- [ ] reply "y" IN THAT THREAD -> the answer is filed as a notes/ page
- [ ] logs show "connected" (Socket Mode) and no auth errors
```

Expected: a threaded reply within a few seconds. A `ConfigError` for `SLACK_BOT_TOKEN` /
`SLACK_APP_TOKEN` / `SLACK_CAPTURE_CHANNEL` means that variable is missing; silence usually
means the app token lacks Socket Mode, the bot was not invited to the channel, or
`SLACK_CAPTURE_CHANNEL` is not the channel you posted in. Stop with `Ctrl-C` once it works.

## 4. MCP -- the pkm_* tools list and one executes over the HTTP socket

The MCP server is the bearer-authenticated HTTP socket from
{doc}`mcp-server-setup` (`thoth-mcp.service`, `127.0.0.1:8765`). With the unit
running and a `THOTH_MCP_API_KEYS` bearer set, point a connected client (Claude
Code via `claude mcp add --transport http …`, or claude.ai through the tunnel)
at it:

```text
- [ ] tools/list returns the nine pkm_* tools (pkm_search, pkm_ask, pkm_ingest,
      pkm_save_answer, pkm_todos, pkm_recent, pkm_write_page, pkm_read_page,
      pkm_edit_page)
- [ ] pkm_recent (days=7) executes and returns recent pages
```

Expected: the nine tools enumerate and `pkm_recent` returns without error. The live suite
builds the server in-process and asserts the registered tool set:

```console
$ THOTH_LIVE_SMOKE=1 uv run pytest -m live -k mcp
```

## 5. Exa + Firecrawl -- one search and one extract

The blended research path uses Exa for discovery and Firecrawl for extraction.

```console
$ THOTH_LIVE_SMOKE=1 uv run pytest -m live -k "exa or firecrawl"
```

Expected: the Exa search returns at least one `WebHit`, and the Firecrawl extract returns
non-empty markdown for a stable public URL. An `ExtractError` mentioning a missing key
means `EXA_API_KEY` / `FIRECRAWL_API_KEY` is unset.

## 6. Cron entrypoints -- incremental reindex and a summary post

The two cron-driven entrypoints (SPEC section 9, the deploy crontab) are one-shot console
commands; run them by hand once.

```console
$ thoth reindex
$ thoth summary daily
```

Expected: `reindex` prints a `changed=/skipped=` line and is near-instant on a
quiet vault (it is incremental -- unchanged pages are skipped via the body-`sha256`
manifest); on success it chains the optional `bin/hindsight-backup.sh` snapshot (a no-op
unless `THOTH_HINDSIGHT_BACKUP=1`). `summary daily` composes the digest from the vault and
posts it to `SLACK_SUMMARY_CHANNEL`; check the channel for the post and the heartbeat
"still alive" line.

```text
- [ ] `thoth reindex` exits 0 with a changed=/skipped= line
- [ ] a full rebuild also works: `thoth reindex --full-rebuild`
- [ ] `thoth summary daily` posts to the summary channel (heartbeat line present)
```

## Running the whole live suite

All of the per-boundary tests above live in one opt-in module that is **skipped offline**
(so CI stays green) and only runs when `THOTH_LIVE_SMOKE=1` is set. To run every live
smoke test in one go on the VPS and get a pass/fail report:

```console
$ THOTH_LIVE_SMOKE=1 uv run pytest -m live
```

Without the env flag the module is collected but every test is skipped, so the same
command in CI (or on a dev box) reports all-skipped and passes. Clean up the throwaway
`concepts/first-light.md` page afterwards if you created one.
