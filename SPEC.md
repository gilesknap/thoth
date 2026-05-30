# PKM Thin-App — Design Sketch

> **Status:** supersedes the *architecture* of `PKM-AGENT-SPEC.md` (the Hermes deployment). The
> vault model, frontmatter contract, ingest/retrieve flows, sync protocol, Hindsight tuning, and
> life-admin/summary/lint specs in that document remain the **reference**; this document re-homes
> them onto a small, owned application instead of the Hermes framework. Where a section here says
> "carried forward (§N)", the verbatim detail lives in `PKM-AGENT-SPEC.md §N`.

> **Executive summary.** Drop Hermes. The PKM is a **single canonical Obsidian vault** (markdown +
> assets, two-way git-synced) fronted by two small things we own: (1) an **unattended appliance**
> — a Slack-bot daemon plus a few cron jobs — that captures, files, syncs, summarises, and reindexes;
> and (2) a **stdio MCP server** exposing the vault as `pkm_*` tools. The **general-agent role is
> deliberately external**: Claude Code (on the pay-as-you-go API key) and claude.ai (subscription)
> are the conversational brain, composing the PKM MCP with web search, sub-agents, and other MCP
> connectors (Calendar/Gmail). The appliance's LLM has **no shell and a closed tool surface**, so it
> cannot reconfigure or wedge itself — the property Hermes structurally could not give us. Target:
> ~1.5–2k lines of Python we fully understand, prototype-able in an afternoon.

---

## 1. Why thin (the decision, settled)

The Hermes route was workable but oversized, and three facts decided it:

1. **Self-configuration is the operational pain, and Hermes can't fully fix it.** The live config has
   `curator.enabled: true` (auto-prunes the agent's own skills weekly), `skills.creation_nudge_interval: 15`
   with `guard_agent_created: false` (the agent is *nudged to author its own skills*, unguarded), and
   `approvals.mode: auto` over a `terminal.backend: local`. Because the PKM needs the agent to run `git`,
   it has a shell — and a shell at auto-approval is a universal self-modification primitive. You can
   disable the curator and nudges, but you can't remove the shell without breaking vault git. So
   "stop it breaking itself" is *mitigation, never elimination*. In a thin app it's **elimination by
   construction** (§4).
2. **MCP collapses the "general agent" need.** Exposing the vault over MCP makes claude.ai / Claude Code
   the general agent — a *better* one than the Hermes loop (the chat experience was the original tell).
   Every "keep Hermes" reason re-homes cleanly: general chat → Claude Code/claude.ai; *delegation across
   web + knowledge graph* → Claude Code does this **natively** (web search + sub-agents + vault-over-MCP,
   one orchestrator); Hindsight → standalone product called directly; email/calendar → their own MCP
   connectors composed at the Claude layer (already present in the user's claude.ai today).
3. **The June-15 Anthropic budget funds the MCP path.** The heaviest Claude consumer here is *Claude Code
   driving the PKM MCP + web + sub-agents* for rich Q&A — point Claude Code at the **API key** (never the
   Max OAuth; keeps the two billing streams separate, ToS-clean) and that work *is* the budget burn,
   spent where it's most valuable. Embeddings stay on Gemini/Voyage (Anthropic has no first-party embed
   model), so Hindsight-on-Gemini is unchanged.

**What we give up:** a single always-on conversational agent that is *ours*. We trade it for best-of-breed
Anthropic agents over our data, plus least-privilege on the unattended box. Reversible anyway: the vault is
files+git, so any agent (incl. Hermes) can be dropped back in front of the same vault later.

---

## 2. Architecture

```
                         ISOLATED VPS
 ┌──────────────────────────────────────────────────────────────────┐
 │  APPLIANCE (we own, unattended)                                   │
 │  ┌────────────────┐    ┌───────────────────────────┐             │
 │  │ Slack bot      │    │ system cron               │             │
 │  │ (Bolt, Socket  │    │  06:30 reindex            │             │
 │  │  Mode daemon)  │    │  07:00 daily summary      │             │
 │  │ message.im /   │    │  Mon 07:00 weekly         │             │
 │  │ file_shared    │    │  Mon 08:00 lint           │             │
 │  └──────┬─────────┘    │  every 6h config-backup   │             │
 │         │              └─────────────┬─────────────┘             │
 │         │  capture / retrieve        │ compose-from-vault        │
 │         ▼                            ▼                           │
 │  ┌───────────────────────────────────────────────┐  ┌─────────┐ │
 │  │ ingest.py / query.py / summary.py / lint.py    │  │HINDSIGHT│ │
 │  │   ── all call ──>  vault.py (closed surface)   │─▶│local_   │ │
 │  │   Anthropic API (PAYG key)   Gemini (via HS)   │◀─│embedded │ │
 │  └──────────────────────┬────────────────────────┘  │ Postgres│ │
 │                         │ git_sync (pull→write→commit)└────▲────┘ │
 │  ┌──────────────────────▼────────────────────────┐  reindex│     │
 │  │  MCP SERVER  (stdio, `mcp serve`)              │  from   │     │
 │  │  pkm_ingest / pkm_search / pkm_todos / pkm_recent│  vault │     │
 │  └──────────────────────┬────────────────────────┘         │     │
 │                         │            ┌────────────────────────────┘
 │                  ┌──────▼─────────────────┐                       │
 │                  │   CANONICAL VAULT      │  raw/ entities/ ...    │
 │                  │   (/opt/pkm-vault)     │  SCHEMA index log      │
 │                  └──────────┬─────────────┘                       │
 └─────────────────────────────┼─────────────────────────────────────┘
                               │ git pull --rebase / push (gh helper)
                               ▼
                 ┌──────────────────────────┐      ┌──────────────────────────┐
                 │ pkm-vault  (private repo)│◀────▶│ WORKSTATION — Obsidian    │
                 └──────────────────────────┘      │ + Obsidian Git plugin     │
                                                    └──────────────────────────┘

  GENERAL AGENT (external, not on the box):
    Claude Code  ── API key ──▶  PKM MCP (stdio)  +  web search  +  sub-agents
    claude.ai    ── subscription ▶ PKM MCP (remote) + Calendar/Gmail/Drive MCP
```

**Reading it:** the vault is the source of truth; everything else is a projection. The appliance and
workstation Obsidian are the two writers, reconciled through the private `pkm-vault` repo (sync protocol
unchanged — §10 here, full detail `PKM-AGENT-SPEC.md §12`). Hindsight is a rebuildable index. The general
agent lives *outside* the box and reaches the vault through MCP. No Hermes anywhere.

---

## 3. The core design principle — closed tool surface, no LLM shell

This is the whole reason the thin app fixes the self-config pain, so it's a hard rule:

- **The appliance LLM never gets a shell or arbitrary file access.** Capture/curation runs as **bounded,
  validated passes**, not an open agentic loop. The model's *output* is a JSON file-plan; the Python
  harness validates every field and path, then writes. The model proposes; the harness disposes.
- **Git is never an LLM tool.** `git_sync.commit()` runs deterministically in Python *after* the model has
  produced its file-plan. The model cannot `push --force`, cannot touch config, cannot run `bash`.
- **Path confinement.** Every write helper rejects paths outside `/opt/pkm-vault` and enforces the folder
  ⨉ `type` contract and slug format before touching disk.
- **The MCP path may be more agentic** because the orchestrator there is *Claude Code* (smart, supervised,
  external) — but it still only ever calls the same validated `pkm_*` functions. There is no code path by
  which any model edits the appliance's own configuration.

Concretely, the appliance model is handed exactly this surface (Python functions, schema-validated):

```
search_vault(query) -> [page paths + titles]      # create-vs-update decision (read-only)
read_page(path)     -> frontmatter + body          # read-only, vault-scoped
# ...then it RETURNS a file-plan; the harness executes:
write_page(folder, slug, frontmatter, body)        # folder∈allowed, slug ok, schema ok
write_raw(subdir, slug, frontmatter, body)
save_asset(tmp, slug)                              # move a downloaded binary into raw/assets/
append_index(section, wikilink, summary)
append_log(action, subject, files)
retain(path, facts)                               # Hindsight, vault-path-keyed
```

No `run_shell`, no `open(arbitrary_path)`, no `git`. Least privilege by construction.

---

## 4. Component map & rough effort

Single language end-to-end (appliance + MCP + reindex) keeps the surface tiny. **Python 3.12 via `uv`**
(`uv run --no-project --python 3.12`) — **decided** (the user's language).

| Module | Responsibility | ~LOC | Phase |
|---|---|---|---|
| `config.py` | load vault path, tokens, model ids from `.env`/`config.toml` (~a dozen vars, not 350) | 40 | 0 |
| `vault.py` | frontmatter read/write, slug/path helpers, `obsidian://` links, `index.md`/`log.md` edits, asset embed | 280 | 1 |
| `bin/vault-pull`, `bin/vault-commit` (bash) + `git_sync.py` | pull-before-write / commit+push wrappers (carried fwd verbatim, §10) + thin shell-out | 80 + 40 | 1 |
| `llm.py` | Anthropic client + prompt caching; the system persona; the file-plan / answer schemas | 180 | 1 |
| `extract.py` | URL→markdown (Exa find / Firecrawl extract), PDF, image save, STT hook (local whisper) | 160 | 2 |
| `ingest.py` | INGEST: classify → capture raw → curate (bounded passes) → nav → retain → commit (§6) | 350 | 2 |
| `query.py` | structural (index/grep) + Hindsight recall → compose answer + canonical links (§7) | 180 | 2 |
| `hindsight.py` | direct retain/recall wrappers over the installed client/CLI | 90 | 2 |
| `reindex_from_vault.py` | nightly incremental + full rebuild (carried fwd, §8 — mostly drafted already) | 140 | 3 |
| `slack_app.py` | Bolt Socket-Mode daemon: `message.im`, `file_shared`, allow-list, mrkdwn rendering | 260 | 2 |
| `mcp_server.py` | FastMCP stdio: `pkm_ingest`/`pkm_search`/`pkm_todos`/`pkm_recent` (+ low-level `pkm_write_page`) | 140 | 3 |
| `summary.py` | daily/weekly digest composed from vault frontmatter + `chat.postMessage` (§9) | 200 | 3 |
| `lint.py` | the 13 maintenance checks (§11) | 250 | 4 |
| `bin/config-backup.sh` | push-only backup of the **app config** repo (carried fwd, §10) | 40 | 3 |
| `pkm-slack.service` + crontab | one systemd unit (daemon) + system cron lines | — | 3 |

**Totals:** core (everything but lint) ≈ **~1,800 LOC Python + ~160 bash**; with lint ≈ 2,050.
**Afternoon** = Phases 0–2 (skeleton + capture + retrieve over Slack against the real vault). The rest is a
few focused sessions. Every line is yours.

### Dependencies (the entire stack)

`slack_bolt` (Socket Mode) · `anthropic` · `mcp` (FastMCP) · `hindsight-client` *or* shell to `hindsight-embed`
· `python-frontmatter` + `pyyaml` · `httpx` · `exa-py` / Firecrawl REST · local `whisper` (optional, voice) ·
PostgreSQL (Hindsight subprocess, as today). Gemini is reached *through* Hindsight, so no separate embed code
unless we later bypass it.

### Repo layout (transition + steady state)

| Repo | Was | Becomes |
|---|---|---|
| `hermes-planning` | planning docs | keep (optionally rename `pkm-planning`); this doc lives here |
| `hermes-agent` | Hermes home + config-backup | **retire**; new code repo **`thoth`** replaces it (also the config-backup target) |
| `pkm-vault` | (to be created) | canonical vault — unchanged plan |

Keep Hermes running against the same vault while `thoth` is built beside it — files are canonical, so
there is no migration risk and nothing canonical is ever in flight. Stop the Hermes gateway only once the
appliance + MCP prove out.

---

## 5. The vault (canonical store) — carried forward (§6)

Unchanged from `PKM-AGENT-SPEC.md §6`; reproduced in brief so this doc builds standalone. The vault root is
the git root **and** the Obsidian vault root. Name `pkm-vault`, VPS path `/opt/pkm-vault`.

```
pkm-vault/
├── index.md   SCHEMA.md   log.md            # Home / conventions / append-only action log
├── raw/        articles/ papers/ transcripts/ assets/   # LAYER 1, immutable (read, never edit)
├── entities/ concepts/ comparisons/ queries/            # LAYER 2, curated knowledge pages
├── actions/ media/ memories/ people/        # life-admin pages (frontmatter `type` is the contract)
├── _bases/ _meta/ _archive/ inbox/          # dashboards / nav aids / superseded / unfiled
└── .obsidian/                               # plugin config (Obsidian Git, Metadata Menu, Bases/Dataview)
```

**Frontmatter contract (common, required everywhere):**

```yaml
---
title: Human Readable Title
type: entity            # entity|concept|comparison|query|summary|action|media|memory|inbox
created: 2026-05-30
updated: 2026-05-30
source: slack           # slack | mcp | web | manual | cron
tags: [kebab-case, from-taxonomy]
---
```

Knowledge add-ons: `sources` (list, required if any raw exists), `confidence`, `contested`, `contradictions`,
`aliases`. Life-admin add-ons: `status`, `priority`, `due_date`, `recurrence`, `project` (action);
`media_type`, `creator`, `url` (media); `people`, `location`, `memory_date` (memory). `raw/` block:
`source_url`, `ingested`, `sha256` (digest of body). Full field semantics, naming rules, SCHEMA.md text,
and worked examples: **`PKM-AGENT-SPEC.md §6 + §18`** (carry verbatim — the schema is framework-independent).

**Images:** embed-and-describe on the owning page (`![[slug-hash.ext]]`), binary in `raw/assets/`, no
sidecar, never base64.

**Dashboards:** Bases-if-it-validates, else Dataview (still an open item, §12). The appliance does the date
math for summaries regardless, so dashboards are not load-bearing for the daily briefing.

---

## 6. Capture / ingest — `ingest.py` (recast of §7)

Same operation as the Hermes spec, minus Hermes tool names, run as **bounded validated passes** (§3):

```
0. ORIENT + PULL      git_sync.pull()  (pull --rebase --autostash) so we write onto current state.
1. CLASSIFY           one cheap Claude call: type (entity|concept|…|action|media|memory|inbox),
                      named entities/concepts, and for life-admin the parsed fields (due/priority/…).
2. CAPTURE RAW        extract.py by kind:
                        URL -> Exa find / Firecrawl extract -> raw/articles/<slug>.md
                        PDF/arxiv -> extract -> raw/papers/<slug>.md  + keep <slug>.pdf
                        transcript/voice -> (whisper) -> raw/transcripts/<slug>.md
                        image -> raw/assets/<slug>-<hash>.<ext>  (binary, never base64)
                      raw frontmatter: source_url, ingested, sha256(body). Skip if sha256 exists.
3. FETCH CANDIDATES   search_vault() for every named entity/concept -> the existing pages to maybe update.
4. CURATE (file-plan) second Claude call: given SCHEMA + candidate pages, RETURN a validated plan of
                      pages to create/update (full frontmatter + body, >=2 wikilinks each, image embeds,
                      confidence/provenance). Harness validates + writes via write_page/write_raw/save_asset.
5. NAVIGATION         append_index() for new knowledge pages (life-admin is surfaced by Bases, no index edit);
                      append_log() listing every file touched.
6. RETAIN             hindsight.retain(path, facts) per curated page (vault-path-keyed); probe it landed.
7. COMMIT             git_sync.commit("<subject>")  -> add -A, commit, pull --rebase, push (never --force).
8. REPORT             reply: files touched + obsidian:// link(s) + vault path + [[wikilink]].
```

> **Capture surfaces & binaries (resolved).** Binary bytes can only enter where the *server* can read them.
> Under the **VPS deployment** (appliance + MCP server on the VPS), the channels are:
> 1. **Slack — primary.** Phone/desktop upload → the appliance (VPS) downloads the bytes from Slack's API and
>    files them. Zero client context; vision runs server-side on the API key. Works from anywhere.
> 2. **A URL — sidesteps the problem.** Papers/articles captured by URL are fetched *server-side* (the
>    appliance downloads the PDF), so no client→server transfer happens. "Binary ingest" really only means
>    *a local file with no URL* (photos, screenshots, scans).
> 3. **Obsidian drag-drop — desk-side complement.** The workstation already holds a synced vault clone;
>    dropping a file into `raw/assets` makes Obsidian Git push it, the VPS pulls it, and an **adopt-orphan-asset**
>    step (the §16 orphan-jpeg flow, made ongoing in the reindex/lint pass) vision-describes + files it. No
>    agent transport needed.
> 4. **Claude Code path-passing — only when co-located with the MCP server.** A *file path* resolves only if
>    the client shares a filesystem with the server: Claude Code **on the VPS** (file must already be there),
>    or a **workstation-local stdio MCP** run against the local vault clone. A workstation Claude Code talking
>    to a *remote* VPS MCP **cannot** pass a local path (no shared FS).
>
> **claude.ai web cannot push binaries at all** — the upload is trapped in the chat and the server can't reach
> it; it is for text/URL capture and retrieval. So `pkm_ingest` arguments accept **text, a URL, or a
> server-resolvable path — never base64 image blobs** (a model can't re-emit a *viewed* image as base64, and
> forcing it would double the client context cost). See §15 Q2.

Routing table (signal → `type` → folder), disambiguation rules, and the persona text are unchanged —
**`PKM-AGENT-SPEC.md §7`**. The persona that was `SOUL.md` becomes the system-prompt string in `llm.py`
(vault is canonical; Hindsight is a derived index; always return `obsidian://` links; concise tone).

> **Note the simplification:** with Hermes gone there is no memory subsystem auto-harvesting chatter. We
> simply `retain()` when we file a page. The entire `auto_retain: false` / `retain_context` /
> `retain_every_n_turns` tuning battle from §9 **disappears** — we never turn on auto-harvest because there
> is none. The index is a function of explicit retain calls + the reindex job, by construction.

---

## 7. Retrieval — `query.py` (recast of §8)

Cost-ordered, structure-first; the harness (not the model) attaches the canonical links so paths can't be
fabricated:

```
query
  -> read index.md (cheap, authoritative)
  -> known term/acronym?  -> grep the vault (search_files)
  -> known page?          -> follow [[wikilinks]]
  -> phrasing-independent? -> hindsight.recall(query) -> vault page paths
  -> COMPOSE answer from page(s); harness appends obsidian:// link + path + [[wikilink]]
```

`obsidian://` link format (unchanged, **§8**): `obsidian://open?vault=pkm-vault&file=<URL-ENCODED vault-rel path>`.
Slack renders `mrkdwn` `<url|label>`; MCP returns markdown `[label](url)` + raw path + wikilink (host may not
make the custom scheme clickable, so always include the plain path). Slack file-upload is a last-resort
fallback only.

---

## 8. Semantic index — Hindsight, called directly (recast of §9)

Hindsight stays exactly as specced (`local_embedded`, `bank_id: hermes`, Gemini extraction +
`text-embedding-004` embeddings, local Postgres subprocess) — we just call it **directly** instead of through
Hermes' `memory.provider`. `hindsight.py` wraps `retain` / `recall`; `reindex_from_vault.py` is carried
forward almost verbatim from §9 (body-`sha256` idempotency, `SOURCE:`-sentinel path attachment, prune-on-
delete, `--full-rebuild`). Three triggers, unchanged: **per-ingest incremental** (primary), **nightly
catch-up** for out-of-band Obsidian edits (cron 06:30), **full rebuild** on recovery.

The Hermes-integration keys (`auto_recall`, `auto_retain`, `memory_mode`, `recall_budget`) were memory-
*provider* wiring; in the thin app we drive retain/recall explicitly, so `hindsight/config.json` shrinks to
what the engine itself needs (mode, bank, `llm_provider`, `llm_model`). **Open items unchanged (§12):** the
exact client surface (`reference=` vs the `SOURCE:` sentinel, the `hindsight-embed` subcommand spellings,
the embedding-model/dimension) must be verified against the installed package — the CLI/subprocess variant
is the safe default until then.

---

## 9. Life-admin & proactive summaries — carried forward (§10, §11)

**Life-admin** (`actions/`, `media/`, `memories/`, `people/`) is unchanged — ordinary vault pages keyed by
frontmatter `type`, surfaced by Bases/Dataview. Recurrence reopen, media `to_consume` aging, people links:
all agent behaviour driven by frontmatter, now implemented in `vault.py` helpers + the ingest curate pass.

**Summaries** become `summary.py` invoked by **system cron** (not a Hermes scheduler): daily 07:00 and weekly
Mon 07:00 Europe/London, composed *from the vault* (actions due/overdue, deadlines, recent ingests from
`log.md`/git, media nudges, review-flagged pages; weekly may use Hindsight `reflect` over curated pages),
delivered via Slack `chat.postMessage` to `D0B61LKA3NV`. Content checklists and the worked example are
unchanged — **§11**. The model id is now a real one we own (`claude-sonnet-4-6`, verify with the model
catalog; dated `claude-sonnet-4-20250514` is the proven fallback) — the bare `claude-sonnet-4`/`gemini-pro`
404s die with Hermes' cron.

---

## 10. Sync, repos, backup — carried forward verbatim (§12)

The two-way git design is framework-independent and carries over **unchanged**: vault is a normal git tree on
both ends; workstation runs the **Obsidian Git** plugin (10-min pull/commit/push, "Detect all file changes"
ON); the appliance wraps every mutation in `vault-pull` (before write) and `vault-commit` (after ingest),
using `gh`'s credential helper + `GIT_CONFIG_GLOBAL=/dev/null` per the user's global git rule, never SSH,
never `--force`. Conflict strategy (raw/ immutable, one-file-per-topic, fail-loud on rebase collision,
surface the path over Slack) is unchanged.

`bin/vault-pull` and `bin/vault-commit` (full bodies in **§12**) move into `thoth/bin/` as-is. The only edit:
`vault-commit`'s push target and `config-backup.sh` now point at the **`thoth`** config repo instead of
`hermes-agent`. `config-backup.sh` still snapshots the transient DBs *if any remain* — but note most of what
it backed up (Hermes `state.db`/`kanban.db`) **ceases to exist** in the thin app: there is no session DB as a
store. The appliance keeps a small **transient state DB** — single-writer, `~/.thoth/state.db`,
gitignored, **never a knowledge store** (the P1 guardrail: only transport bookkeeping + in-flight buffers +
optional TTL'd chat context; the instant knowledge exists, it is a vault file). Tables:
`processed_events(event_id, ts)` (Slack redelivery dedupe; prune >1h); `captures(id, channel, slack_ts, kind,
status, summary, vault_paths, error, created)` (pending→filed→failed — crash-safe ingest + the "did it land?"
report); `conversations(channel, role, content, ts)` (optional; TTL ~30 min for Slack follow-ups, never a
transcript). Single-writer ⇒ no git / two-writer surface; disposable ⇒ **not** part of recovery and **not**
backed up (on VPS loss, start fresh — you lose only dedupe history + mid-flight captures, both cheap). It is
the *only* state outside the vault, kept tiny and pruned. **Backup model:** the `pkm-vault` repo *is* the durable knowledge backup; `thoth` repo backs up
code+config; secrets live only in `.env` (chmod 600) and a password manager. Full recovery (§12) simplifies to:
clone `thoth`, clone `pkm-vault`, restore `.env`, `reindex --full-rebuild`, start the systemd unit.

---

## 11. Maintenance / lint — `lint.py` (carried forward §13)

The 13 checks (orphans, broken wikilinks/embeds, index completeness, frontmatter validation, stale content,
contradictions, source drift, quality signals, page size, tag audit, image hygiene, log rotation, report+log)
are a pure markdown scan — framework-independent, carried verbatim from **§13**. Runs weekly via cron and on
demand after a bulk migration/restore. Independent of the reindex job. Phase 4 (not needed for first light).

---

## 12. Security — simpler than §14

Most of Hermes' guard-rail apparatus existed to contain a powerful general agent we no longer run:

- **Least privilege replaces most controls.** No shell for the LLM ⇒ **no Tirith, no `command_allowlist`, no
  dangerous-command approval flow** needed. The appliance can only call validated vault functions; git/sync
  are deterministic Python. This is a stronger guarantee than scanning a capable agent's output.
- **Billing separation, intact and simpler.** The appliance uses `ANTHROPIC_API_KEY` only. Claude Code on the
  MCP can burn the API budget (console key) or run on the Max subscription — either way that's *normal product
  use of Anthropic's own agent*, not the app wiring an OAuth token, so the ban-risk surface essentially
  vanishes. Keep the rule: the box never holds the Max OAuth credential.
- **Secrets:** `.env` chmod 600, gitignored, never in any repo; redact secret-looking strings in `vault.py`
  *before* filing (so a pasted token never lands in a page or the index).
- **MCP exposure:** local **stdio** for Claude Code = no network surface (v1 target). Remote for claude.ai web
  must sit behind a **Cloudflare Tunnel** (TLS + identity), never bare HTTP — unchanged, and still optional/
  deferred (§13 open items). `allow_private_urls: false` for the web extractors (SSRF guard).
- **Isolated single-tenant VPS**, ideally a dedicated unprivileged `pkm` user for the systemd unit.

---

## 13. Build plan (phased, afternoon-first)

| Phase | Deliverable | Proves |
|---|---|---|
| **0** | `thoth` repo, `config.py`, `.env`, deps installed, Phase-A/B prereqs (vault repo created + cloned, tokens) | scaffolding |
| **1** | `vault.py` + git wrappers + `llm.py`; write/read a curated page by hand, commit+push, see it in Obsidian | the closed surface + sync round-trip |
| **2** | `ingest.py` + `extract.py` + `query.py` + `slack_app.py`: **throw a URL/photo/thought at the Slack DM → filed page + obsidian link back; ask a question → answer + link** | *the afternoon goal* |
| **3** | `mcp_server.py` (Claude Code config), `reindex_from_vault.py`, `summary.py`, cron + `pkm-slack.service`, `config-backup.sh` | unattended + MCP + budget-ready |
| **4** | `lint.py`; Bases-or-Dataview decision; remote MCP (Cloudflare) if wanted | hardening |
| **Migrate** | `documents/` → vault (assets + curated pages + spine), then stop the Hermes gateway | cut-over (§14) |

---

## 14. Migration from current state

The vault-content migration is unchanged from **`PKM-AGENT-SPEC.md §16`** (3 images + 2 sidecars + 1 orphan →
`raw/assets/` + curated embed-and-describe pages + the `SCHEMA.md`/`index.md`/`log.md` spine + `_bases`). What
changes is the *cut-over*: instead of rewriting `SOUL.md` and re-pointing Hermes' Hindsight/cron, you:

1. Build `thoth` Phases 0–3 against the **same** `/opt/pkm-vault` (Hermes can keep running — files are
   canonical, no contention beyond the normal two-writer protocol).
2. Run the §16 content migration once, into `pkm-vault`.
3. Point Claude Code's `~/.claude/settings.json` at the `thoth` MCP server; verify `pkm_search` returns
   vault pages.
4. Switch summaries/reindex to the `thoth` cron; confirm the 07:00 Slack digest fires from the new path.
5. **Stop and disable the Hermes gateway** (`hermes gateway stop`), archive the `hermes-agent` repo. Tirith,
   `state.db`, the 96 skills, the 350-var config — all retired. The vault and the `obsidian://` deep links are
   untouched throughout.

---

## 15. Open questions

**Carried forward (still real, verify against installed software):**
1. **Hindsight client surface** — `reference=` vs `SOURCE:` sentinel; real class/CLI subcommands; per-page
   `forget`/upsert; embedding model + dimension. CLI/subprocess variant is the safe default. (§8; spec §17 Q4/5)
2. **Bases vs Dataview** — confirm the installed Obsidian ships Bases and the date syntax parses; else Dataview.
   Summaries do their own date math regardless. (spec §17 Q6)
3. **`obsidian://` path form** + **Slack custom-scheme** rendering — verify on the actual devices. (spec §17 Q2/3)
4. **Recurring-action reopen** semantics; **dedicated unprivileged user**; **remote MCP** (Cloudflare) yes/no.

**Resolved (this session):**
5. **Language → Python 3.12** (via `uv`). Settled — the user's language.
6. **MCP ingest → server-side primary.** `pkm_ingest(text | url | path)` curates **server-side on the API
   key** — spends the budget *and* keeps the client conversation lean (only a short confirmation returns, not
   the full extracted doc). Also expose low-level `pkm_write_page(...)` for when Claude Code prefers to curate
   itself. Slack is always server-side. **Binaries: text/url/path only, never base64** (§6 capture note).
7. **Name → `thoth`** — repo/app name **and** the MCP server key (tools surface as `mcp__thoth__pkm_ingest`,
   etc.); the vault stays `pkm-vault`. The name never has to be spoken to invoke a tool — Claude Code routes
   by tool name + description, so the server name is just the namespace prefix / a disambiguation handle.
8. **State → keep a small transient single-writer SQLite** (`~/.thoth/state.db`) — gitignored, pruned,
   not backed up, never a knowledge store (schema + rationale in §10).
```
