# PKM Thin-App — Design Sketch

> **Status:** this document is fully self-contained (originally derived from an earlier Hermes-framework
> deployment spec, now superseded). The vault model, frontmatter contract, ingest/retrieve flows, sync
> protocol, Hindsight tuning, and life-admin/summary/lint specs are all reproduced here — the body gives the
> thin-app design and the **Appendix** at the end carries the verbatim framework-independent detail. Where a
> section says "carried forward", the full detail lives in the matching **Appendix** subsection; no external
> document is needed.

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
 ┌────────────────────────────────────────────────────────────────────┐
 │  APPLIANCE (we own, unattended)                                    │
 │  ┌────────────────┐  ┌───────────────────────────┐                 │
 │  │ Slack bot      │  │ system cron               │                 │
 │  │ (Bolt, Socket  │  │  06:30 reindex            │                 │
 │  │  Mode daemon)  │  │  07:00 daily summary      │                 │
 │  │ message.im /   │  │  Mon 07:00 weekly         │                 │
 │  │ file_shared    │  │  Mon 08:00 lint           │                 │
 │  └──────┬─────────┘  │  every 6h config-backup   │                 │
 │         │            └─────────────┬─────────────┘                 │
 │         │  capture / retrieve      │  compose-from-vault           │
 │         ▼                          ▼                               │
 │  ┌──────────────────────────────────────────────────┐  ┌─────────┐ │
 │  │ ingest.py / query.py / summary.py / lint.py      │  │HINDSIGHT│ │
 │  │   ── all call ──▶  vault.py (closed surface)     │─▶│local_   │ │
 │  │   Anthropic API (PAYG key)   Gemini (via HS)     │◀─│embedded │ │
 │  └────────────────────────┬─────────────────────────┘  │Postgres │ │
 │                           │ git_sync (pull→commit)     └───▲─────┘ │
 │                           │                                │       │
 │  ┌──────────────────────────────────────────────────┐      │       │
 │  │ MCP SERVER  (stdio, `mcp serve`)                 │      │       │
 │  │ pkm_ingest / pkm_search / pkm_todos / pkm_recent │      │       │
 │  └────────────────────────┬─────────────────────────┘      │       │
 │                           │                                │       │
 │                  ┌──────────────────────┐                  │       │
 │                  │   CANONICAL VAULT    │── reindex ───────┘       │
 │                  │   (/opt/pkm-vault)   │                          │
 │                  └──────────┬───────────┘                          │
 └─────────────────────────────┼──────────────────────────────────────┘
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
unchanged — §10 here, full detail in Appendix → Git wrappers & .gitignore + Backup/recovery). Hindsight is a rebuildable index. The general
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
| `bin/vault-pull`, `bin/vault-commit` (bash) + `git_sync.py` | pull-before-write / commit+push wrappers (carried fwd verbatim — Appendix → Git wrappers) + thin shell-out | 80 + 40 | 1 |
| `llm.py` | Anthropic client + prompt caching; the system persona; the file-plan / answer schemas | 180 | 1 |
| `extract.py` | URL→markdown (Exa find / Firecrawl extract), PDF, image save, STT hook (local whisper); read-only `web_search`/`web_extract` reused by `pkm_ask` | 160 | 2 |
| `ingest.py` | INGEST: classify → capture raw → curate (bounded passes) → nav → retain → commit (§6; Appendix → Routing & persona) | 350 | 2 |
| `query.py` | structural (index/grep) + Hindsight recall → compose answer + canonical links (§7; Appendix → Retrieval & obsidian links) | 180 | 2 |
| `research.py` | `pkm_ask`: blended web+vault Q&A — Sonnet w/ read-only vault + Exa/Firecrawl tools, model-decides web, cites both, offer-to-save as `queries/` page (§7.1) | 130 | 3 |
| `hindsight.py` | direct retain/recall wrappers over the installed client/CLI | 90 | 2 |
| `reindex_from_vault.py` | nightly incremental + full rebuild (carried fwd — Appendix → Reindex job; mostly drafted already) | 140 | 3 |
| `slack_app.py` | Bolt Socket-Mode daemon: `message.im`, `file_shared`, allow-list, mrkdwn rendering | 260 | 2 |
| `mcp_server.py` | FastMCP stdio: `pkm_ingest`/`pkm_search`/`pkm_ask`/`pkm_todos`/`pkm_recent` (+ low-level `pkm_write_page`) | 140 | 3 |
| `summary.py` | daily/weekly digest composed from vault frontmatter + `chat.postMessage` (§9) | 200 | 3 |
| `lint.py` | the 13 maintenance checks (§11; Appendix → Lint checks) | 250 | 4 |
| `bin/config-backup.sh` | push-only backup of the **app config** repo (carried fwd — Appendix → Backup/recovery) | 40 | 3 |
| `pkm-slack.service` + crontab | one systemd unit (daemon) + system cron lines | — | 3 |

**Totals:** core (everything but lint) ≈ **~1,930 LOC Python + ~160 bash**; with lint ≈ 2,180.
**Afternoon** = Phases 0–2 (skeleton + capture + retrieve over Slack against the real vault). The rest is a
few focused sessions. Every line is yours.

### Dependencies (the entire stack)

`slack_bolt` (Socket Mode) · `anthropic` · `mcp` (FastMCP) · shell to the `hindsight` CLI (env-overridable
binary; VPS still has `hindsight-embed`) · `python-frontmatter` + `pyyaml` · `httpx` · `tenacity` (bounded retry
around the Hindsight subprocess) · `exa-py` / Firecrawl REST · local `whisper` (optional, voice) ·
PostgreSQL (Hindsight subprocess, as today). Gemini is reached *through* Hindsight, so no separate embed code
unless we later bypass it.

### Repo layout (transition + steady state)

| Repo | Was | Becomes |
|---|---|---|
| planning repo | planning docs | keep (optionally rename `pkm-planning`); this doc lives here |
| `hermes-agent` | Hermes home + config-backup | **retire**; new code repo **`thoth`** replaces it (also the config-backup target) |
| `pkm-vault` | (to be created) | canonical vault — unchanged plan |

Keep Hermes running against the same vault while `thoth` is built beside it — files are canonical, so
there is no migration risk and nothing canonical is ever in flight. Stop the Hermes gateway only once the
appliance + MCP prove out.

---

## 5. The vault (canonical store) — carried forward (Appendix → Vault schema)

Summarised here; the full field semantics, naming rules, SCHEMA.md body, and worked examples are in
Appendix → Vault schema & SCHEMA.md. The vault root is
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
and worked examples: **Appendix → Vault schema & SCHEMA.md** (the schema is framework-independent).

**Images:** embed-and-describe on the owning page (`![[slug-hash.ext]]`), binary in `raw/assets/`, no
sidecar, never base64.

**Dashboards:** Bases-if-it-validates, else Dataview (still an open item, §15; examples in Appendix → Dashboards). The appliance does the date
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
>    step (the Appendix → Migration orphan-jpeg flow, made ongoing in the reindex/lint pass) vision-describes + files it. No
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

Routing table (signal → `type` → folder), disambiguation rules, and the persona text are in
**Appendix → Routing & persona**. The persona that was a `SOUL.md` file becomes the system-prompt string in `llm.py`
(vault is canonical; Hindsight is a derived index; always return `obsidian://` links; concise tone).

> **Note the simplification:** with Hermes gone there is no memory subsystem auto-harvesting chatter. We
> simply `retain()` when we file a page. The entire `auto_retain: false` / `retain_context` /
> `retain_every_n_turns` tuning battle (see Appendix → Reindex job) **disappears** — we never turn on auto-harvest because there
> is none. The index is a function of explicit retain calls + the reindex job, by construction.

---

## 7. Retrieval — `query.py` (carried fwd: Appendix → Retrieval & obsidian links)

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

`obsidian://` link format (full detail in **Appendix → Retrieval & obsidian links**): `obsidian://open?vault=pkm-vault&file=<URL-ENCODED vault-rel path>`.
Slack renders `mrkdwn` `<url|label>`; MCP returns markdown `[label](url)` + raw path + wikilink (host may not
make the custom scheme clickable, so always include the plain path). Slack file-upload is a last-resort
fallback only.

### 7.1 Blended web + vault Q&A — `pkm_ask` / `research.py`

`query.py` above answers from the vault alone. **`pkm_ask`** is the second retrieval mode: a general
question answered by **Claude Sonnet** with *both* the vault **and** the web as read-only sources, citing
each. It re-homes the Exa + Firecrawl capability we had pre-configured under Hermes onto a tool we own, so a
blended answer is reachable from **Slack/phone alone** — without opening Claude Code.

> **Scope note (deliberate).** §1 pushed "web + knowledge-graph together" out to the external Claude Code.
> `pkm_ask` knowingly re-internalizes a *read-only slice* of that for the appliance's own surfaces (Slack DM,
> the MCP). It is **not** an open agent: it gets a closed, read-only tool set (vault read + web search/extract),
> never writes, never a shell — fully inside the §3 rule. The one writing action (§4 below) is the explicit,
> user-confirmed "save this answer" step, which routes through the same validated `write_page`.

```
pkm_ask(question, force_web=false)
  1. VAULT pass    hindsight.recall + index/grep  -> candidate pages (read_page, read-only)
  2. WEB decision  Sonnet is handed both tools and DECIDES if web is needed
                   (force_web / a "research:" prefix forces it; pure-personal Qs like
                    "what are my todos" stay vault-only and cheap):
                     web_search(q)        Exa  -> ranked URLs + snippets   (semantic discovery)
                     web_extract(url...)  Firecrawl -> clean markdown      (full read of top N)
  3. COMPOSE       Sonnet answers from {vault excerpts + web excerpts}, citing BOTH:
                     vault -> obsidian:// link + path + [[wikilink]]   (harness-attached, unfabricable)
                     web   -> source URL(s)
  4. OFFER SAVE    reply ends with "save this to the vault? (y)"; on confirm, write a
                   queries/<slug>.md page: the answer + web `sources:` list + vault [[wikilinks]]
                   via the validated write_page (closes the loop — web knowledge becomes a
                   curated `queries/` second-brain page). Declined answers stay ephemeral.
```

**Exa ↔ Firecrawl split:** Exa is **semantic discovery** (find the right pages by meaning); Firecrawl is
**extraction** (pull clean full-text of the top hits to actually read). Both clients already live in
`extract.py` for the ingest path; `pkm_ask` reuses them via a thin read-only `web_search`/`web_extract`
surface. SSRF guard `allow_private_urls: false` applies (§12). Cost (Sonnet + Exa/Firecrawl credits) is the
high-value Q&A burn §1.3 explicitly earmarks; the model-decides gate keeps it off purely personal lookups.

Exposed as the MCP tool **`pkm_ask`** and the default Slack free-text question path (`pkm_search` remains the
fast vault-only lookup for when you want *only* your own pages).

---

## 8. Semantic index — Hindsight, called directly (carried fwd: Appendix → Reindex job)

Hindsight stays exactly as specced (`local_embedded`, **`bank_id: thoth`** (renamed off the Hermes-era
`hermes`; overridable via `THOTH_HINDSIGHT_BANK`), Gemini extraction + `text-embedding-004` embeddings, local
Postgres subprocess) — we just call it **directly** instead of through the Hermes-era `memory.provider` wiring.
`hindsight.py` wraps `retain` / `recall`; `reindex_from_vault.py` is carried forward almost verbatim (Appendix →
Reindex job: body-`sha256` idempotency, **tag-based** path attachment with the `SOURCE:` sentinel as fallback,
prune-on-delete, `--full-rebuild`). Three triggers, unchanged: **per-ingest incremental** (primary), **nightly
catch-up** for out-of-band Obsidian edits (cron 06:30), **full rebuild** on recovery.

**CLI surface (corrected to the official `hindsight` CLI — https://hindsight.vectorize.io/sdks/cli).** The binary
is **`hindsight`** (not `hindsight-embed`; overridable via `THOTH_HINDSIGHT_BINARY` so the VPS, which currently
has `hindsight-embed` installed under the hermes user, can reconcile). `-p <profile>` is the **named CLI
profile** (optional, `THOTH_HINDSIGHT_PROFILE`), **not** the bank. The **bank id is a positional argument** of
each subcommand, and the verbs are **two tokens** under `memory`:
`hindsight [-p <profile>] memory retain <bank_id> "<text>" [--context …] [--async]` and
`hindsight [-p <profile>] memory recall <bank_id> "<query>" -o json [--tags <rel> --tags-match all]`. Recall is
parsed from its **`-o json`** output (we never scrape pretty stdout). Bulk retain is `memory retain-files
<bank_id> notes.txt`. The exact binary/flag/verb spellings and the per-hit tag round-trip are **confirmed
against the installed binary at VPS-time**; everything that could differ is env- or constant-overridable.

**Provenance survives LLM fact-extraction.** Hindsight runs LLM fact-extraction (not token chunking), so a
whole-page `retain` may be split into several atomic facts and the in-band `SOURCE: <rel-path>` sentinel can
attach to only one of them or none. **Tags are therefore the primary provenance channel:** `retain` passes the
vault-relative path as a tag (alongside `page_type`), and recall recovers the path from each hit's `rel` tag,
falling back to the `SOURCE:` sentinel only when tags are absent. Both channels are kept; tags are preferred.

**Resilience.** The checked subprocess calls (`retain`, `recall`) are wrapped in a bounded `tenacity` retry
(default 3 attempts, exponential backoff with a short cap) that re-attempts only **transient** signals (non-zero
exit / spawn error / daemon-not-ready) and **fails fast** on permanent ones (bad arguments / auth, exit 2).
`forget` and `git_sync` are *not* retried.

The Hermes-integration keys (`auto_recall`, `auto_retain`, `memory_mode`, `recall_budget`) were memory-
*provider* wiring; in the thin app we drive retain/recall explicitly, so `hindsight/config.json` shrinks to
what the engine itself needs (mode, bank, `llm_provider`, `llm_model`). **Open items unchanged (§15):** the
embedding-model/dimension still wants confirming against the installed package; the CLI/subprocess variant is
the implemented path.

---

## 9. Life-admin & proactive summaries — carried forward (Appendix → Vault schema; Dashboards)

**Life-admin** (`actions/`, `media/`, `memories/`, `people/`) is unchanged — ordinary vault pages keyed by
frontmatter `type`, surfaced by Bases/Dataview. Recurrence reopen, media `to_consume` aging, people links:
all agent behaviour driven by frontmatter, now implemented in `vault.py` helpers + the ingest curate pass.

**Summaries** become `summary.py` invoked by **system cron** (not a Hermes scheduler): daily 07:00 and weekly
Mon 07:00 Europe/London, composed *from the vault* (actions due/overdue, deadlines, recent ingests from
`log.md`/git, media nudges, review-flagged pages; weekly may use Hindsight `reflect` over curated pages),
delivered via Slack `chat.postMessage` to `D0B61LKA3NV`. Content checklists and the worked daily-summary example are
in **Appendix → Routing & persona** (summary content list). The model id is now a real one we own (`claude-sonnet-4-6`, verify with the model
catalog; dated `claude-sonnet-4-20250514` is the proven fallback) — the bare `claude-sonnet-4`/`gemini-pro`
404s die with Hermes' cron.

---

## 10. Sync, repos, backup — carried forward verbatim (Appendix → Git wrappers & .gitignore; Backup/recovery)

The two-way git design is framework-independent and carries over **unchanged**: vault is a normal git tree on
both ends; workstation runs the **Obsidian Git** plugin (10-min pull/commit/push, "Detect all file changes"
ON); the appliance wraps every mutation in `vault-pull` (before write) and `vault-commit` (after ingest),
using `gh`'s credential helper + `GIT_CONFIG_GLOBAL=/dev/null` per the user's global git rule, never SSH,
never `--force`. Conflict strategy (raw/ immutable, one-file-per-topic, fail-loud on rebase collision,
surface the path over Slack) is unchanged.

`bin/vault-pull` and `bin/vault-commit` (full bodies in **Appendix → Git wrappers & .gitignore**) move into `thoth/bin/` as-is. The only edit:
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
code+config; secrets live only in `.env` (chmod 600) and a password manager. Full recovery (Appendix → Backup/recovery) simplifies to:
clone `thoth`, clone `pkm-vault`, restore `.env`, `reindex --full-rebuild`, start the systemd unit.

**Optional fast-restore snapshot (the index stays disposable).** `bin/hindsight-backup.sh` may, after a
*successful* nightly reindex, take a **best-effort, config-gated** snapshot of the Hindsight bank — a **logical
`pg_dump`** of the bank's Postgres database (not a data-dir copy) plus a copy of `reindex-manifest.json` —
retaining ~3 generations and pruning older. It is **disabled by default** (`THOTH_HINDSIGHT_BACKUP=1` to
enable) and no-ops cleanly when `pg_dump`/the `local_embedded` daemon socket is absent, so CI and a dev box stay
green. It is strictly **subordinate to `--full-rebuild`**: the index remains disposable (above) — a missing
snapshot is never an error, the snapshot only buys a faster cold start than a from-scratch re-embed. The exact
pg connection/socket is VPS-time, so the dump command is fully overridable
(`THOTH_HINDSIGHT_PG_DUMP`/`THOTH_HINDSIGHT_PG_DATABASE`/`THOTH_HINDSIGHT_PG_DSN`).

---

## 11. Maintenance / lint — `lint.py` (carried forward: Appendix → Lint checks)

The 13 checks (orphans, broken wikilinks/embeds, index completeness, frontmatter validation, stale content,
contradictions, source drift, quality signals, page size, tag audit, image hygiene, log rotation, report+log)
are a pure markdown scan — framework-independent, carried verbatim in **Appendix → Lint checks**. Runs weekly via cron and on
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
  deferred (§15 open items). `allow_private_urls: false` for the web extractors (SSRF guard).
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

The vault-content migration detail is in **Appendix → Migration** (3 images + 2 sidecars + 1 orphan →
`raw/assets/` + curated embed-and-describe pages + the `SCHEMA.md`/`index.md`/`log.md` spine + `_bases`). What
changes is the *cut-over*: instead of rewriting a persona file and re-pointing a framework's Hindsight/cron, you:

1. Build `thoth` Phases 0–3 against the **same** `/opt/pkm-vault` (Hermes can keep running — files are
   canonical, no contention beyond the normal two-writer protocol).
2. Run the Appendix → Migration content migration once, into `pkm-vault`.
3. Point Claude Code's `~/.claude/settings.json` at the `thoth` MCP server; verify `pkm_search` returns
   vault pages.
4. Switch summaries/reindex to the `thoth` cron; confirm the 07:00 Slack digest fires from the new path.
5. **Stop and disable the Hermes gateway** (`hermes gateway stop`), archive the `hermes-agent` repo. Tirith,
   `state.db`, the 96 skills, the 350-var config — all retired. The vault and the `obsidian://` deep links are
   untouched throughout.

---

## 15. Open questions

**Carried forward (still real, verify against installed software):**
1. **Hindsight client surface** — **mostly resolved** to the official CLI
   (https://hindsight.vectorize.io/sdks/cli): binary `hindsight` (env-overridable; VPS still has
   `hindsight-embed`), `-p` = profile, **bank id positional**, two-token `memory retain|recall <bank> …`,
   recall via `-o json`, **tags carry provenance** (`SOURCE:` sentinel as fallback). Still to **verify against
   the installed binary at VPS-time**: the exact binary/verb/flag spelling, the per-hit tag round-trip, a
   per-page `forget` (none in the official surface — full rebuild is the authoritative reset), the bank-reset
   subcommand, and the embedding model + dimension. (§8; see Appendix → Reindex job, which preserves the
   remaining UNVERIFIED-symbol warnings)
2. **Bases vs Dataview** — confirm the installed Obsidian ships Bases and the date syntax parses; else Dataview.
   Summaries do their own date math regardless. (see Appendix → Dashboards)
3. **`obsidian://` path form** + **Slack custom-scheme** rendering — verify on the actual devices. (see Appendix → Retrieval & obsidian links)
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
9. **Blended web+vault Q&A → `pkm_ask`** (§7.1). Re-homes the pre-configured Exa + Firecrawl onto an owned
   tool so general questions can be asked **from Slack/phone alone**, answered by Claude Sonnet over vault +
   web with citations to both. **Web is model-decided** (a `research:` prefix / `force_web` forces it; pure
   personal lookups stay vault-only). Answers are **offer-to-save** as a `queries/` page on confirm. Read-only
   tool surface — stays inside §3; the spend is the §1.3-earmarked high-value Q&A budget.

---

## Appendix — Carried-forward detail (self-contained)

> This appendix inlines the framework-independent detail that the body refers to, so no external document
> is needed. It was carried forward from an earlier Hermes-deployment spec; paths/ownership have been
> re-homed to the thin app (`~/.thoth`, the `thoth` repo, persona-as-`llm.py`-string, system cron, direct
> `hindsight.py` calls). Code that is genuinely framework-independent (git wrappers, SCHEMA.md, the reindex
> job, lint logic) is reproduced verbatim except for those substitutions. Where the source flagged something
> as UNVERIFIED (Hindsight client symbols), the warning is preserved.

### Vault schema & SCHEMA.md

#### Vault identity and on-disk location

| Property | Value |
|---|---|
| Vault name (for `obsidian://` links and `OBSIDIAN_VAULT_NAME`) | `pkm-vault` |
| VPS path | `/opt/pkm-vault`. Set `PKM_VAULT=/opt/pkm-vault` and `OBSIDIAN_VAULT_NAME=pkm-vault` in `~/.thoth/.env` (and the systemd unit's `Environment=`), since the app resolves the concrete absolute path before any file op. |
| Git remote | dedicated **private** repo `github.com/<owner>/pkm-vault` — separate from the `thoth` code/config-backup repo |
| Workstation sync | Obsidian + **Obsidian Git** community plugin (scheduled pull/commit/push) |
| Attachment folder (Obsidian setting) | `raw/assets` |
| New/default file location (Obsidian setting) | `raw/articles` |

The vault root **is** the git root and **is** the Obsidian vault root — no nesting mismatch. The vault **name**
(`pkm-vault`) must match the Obsidian vault registration on every device exactly, or `obsidian://` deep links
will not resolve on that device; store it once as `OBSIDIAN_VAULT_NAME` and use it verbatim in every reply.

#### Hybrid folder tree

The knowledge core uses the llm-wiki layers verbatim. Life-admin is **not** a rival folder tree —
Actions/Media/Memories are wiki pages distinguished by a frontmatter `type` field and surfaced as Obsidian
**Bases** dashboards (dynamic views). The only additional top-level life-admin folders are `inbox/` (the
ambiguous-capture fallback) and `people/` (a first-class entity sub-domain that life-admin pages link into).

```
pkm-vault/
├── index.md              # HOME landing page: unifies knowledge + life-admin, embeds Bases
├── SCHEMA.md             # Conventions, frontmatter contract, tag taxonomy, thresholds
├── log.md                # Append-only action log (rotated yearly + at 500 entries)
│
├── raw/                  # LAYER 1 — immutable sources (read, never edit)
│   ├── articles/         #   web clippings (Firecrawl/Exa extract -> markdown)
│   ├── papers/           #   papers/arxiv: extracted <slug>.md + the source <slug>.pdf alongside it
│   ├── transcripts/      #   meeting notes, voice memos, interview transcripts
│   └── assets/           #   binary images/diagrams embedded by curated pages (NOT paper PDFs)
│
├── entities/             # LAYER 2 — people, orgs, products, models, beamlines, devices
├── concepts/             # LAYER 2 — topics, techniques, how-tos, reference explainers
├── comparisons/          # LAYER 2 — side-by-side analyses (table-first)
├── queries/              # LAYER 2 — filed answers worth keeping
│
├── actions/              # life-admin: TODOs (type: action) — one file per task
├── media/                # life-admin: to-consume backlog (type: media)
├── memories/             # life-admin: personal memories/milestones (type: memory)
├── people/               # entity sub-domain that memories/actions link into
│
├── _bases/               # Obsidian Bases definition files (.base) — dashboards
│   ├── home.base
│   ├── actions.base
│   ├── media.base
│   ├── memories.base
│   └── inbox.base
│
├── _meta/                # navigation aids for large vaults (topic-map.md when index > 200)
├── _archive/             # superseded pages (mirrors original path; removed from index)
├── inbox/                # ambiguous captures awaiting classification (type: inbox)
└── .obsidian/            # plugin config: metadata-menu presets, obsidian-git, Bases
```

`actions/`/`media/`/`memories/`/`people/` stay real folders (different lifecycle from knowledge pages —
status churn, due dates, completion; predictable "new note here" and collision surface), but they are still
**pages with frontmatter `type`**, so Bases can union them with knowledge pages on the Home page — the folder
is an implementation detail, the `type` field is the contract. Because they are real folders, life-admin pages
have real vault-relative paths and therefore real `obsidian://` deep links.

The underscore-prefixed dirs (`_bases/`, `_meta/`, `_archive/`) are structural, not knowledge: they are
**excluded from the Hindsight reindex** and from global Bases `file.ext == "md"` knowledge views; `_archive/`
is dropped from `index.md`.

#### Frontmatter schema (knowledge + life-admin in one contract)

Every `.md` page begins with a YAML block. Fields split into **common** (all pages), **knowledge**
(`type: entity|concept|comparison|query|summary`), and **life-admin** (`type: action|media|memory|inbox`).
`raw/` files carry their own minimal block (see the raw/ frontmatter below).

Common (required on every page):

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

Knowledge pages add (all optional except where noted):

| Field | Type | Meaning |
|---|---|---|
| `sources` | list | `[raw/articles/foo.md]` — raw files this page synthesises (required if any raw exists) |
| `confidence` | `high\|medium\|low` | how well-supported; default unset = treat as medium. Lint flags `low` and single-source-without-confidence |
| `contested` | `true` | page has unresolved contradictions; surfaced by lint |
| `contradictions` | list | page slugs this one conflicts with |
| `aliases` | list | alternate names (Obsidian alias resolution for wikilinks) |

Life-admin pages add:

| Field | Applies to | Type / values | Meaning |
|---|---|---|---|
| `status` | action, media | `todo\|in_progress\|done\|completed\|cancelled` (action); `to_consume\|consuming\|consumed` (media) | Metadata Menu preset-backed |
| `priority` | action, media | `1 - Urgent\|2 - High\|3 - Medium\|4 - Low` | Metadata Menu preset-backed |
| `due_date` | action | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` or empty | for sort/filter and daily briefing |
| `recurrence` | action | `none\|daily\|weekly\|monthly\|yearly` (or RRULE-lite string) | repeating tasks; agent re-opens on completion |
| `project` | action | wikilink `"[[project-slug]]"` or empty | links task to its concept/entity page |
| `media_type` | media | `book\|film\|tv\|podcast\|article\|video\|music` | Metadata Menu preset-backed |
| `creator` | media | string | author/director/artist |
| `url` | media | URL or empty | link to the item |
| `people` | memory, action | list of names (each a `[[people/...]]` link where known) | who was involved |
| `location` | memory | string | where it happened |
| `memory_date` | memory | `YYYY-MM-DD` or empty | when it happened, if different from `created` |

Notes:
- `category` from the old 2ndBrain schema is **dropped** — `type` plus folder replace it. Legacy mapping:
  **`Reference` → `concepts/`**; **`Projects` → project *pages* in `entities/`** (a project is a named thing;
  use `concepts/` only if it is better modelled as a body of work). An action's `project:` is a wikilink to
  that page (e.g. `project: "[[home-maintenance]]"` → `entities/home-maintenance.md`). There is no separate
  `projects/` folder.
- `tokens_used` (old Gemini accounting field) is **dropped** from the page contract; cost telemetry lives in
  `log.md`/the transient state DB, not in knowledge.
- The Metadata Menu preset config (`status`, `priority`, `media_type` dropdowns) is preserved verbatim so
  in-Obsidian editing offers the same controlled vocabularies. Ship it at
  `.obsidian/plugins/metadata-menu/data.json`.

#### File-naming conventions

| Layer | Convention | Example |
|---|---|---|
| Curated pages (entities/concepts/comparisons/queries) | lowercase, hyphenated, one-topic-per-file, no dates in name | `entities/program-motion-controller.md` |
| Life-admin pages | same lowercase-hyphen rule; dates live in frontmatter only | `actions/fix-garden-fence.md` |
| `people/` | lowercase hyphen of the person's name | `people/jane-doe.md` |
| Raw sources | descriptive, hyphenated, source-typed; date suffix only when it disambiguates | `raw/articles/karpathy-llm-wiki-2026.md`, `raw/papers/attention-is-all-you-need.md` |
| Binary assets | `raw/assets/<slug>-<shorthash>.<ext>` — stable, collision-proof, descriptive | `raw/assets/motor-control-diagram-e4a408.png` |

This abandons the old 2ndBrain `Attachments/20260207_113000_photo.png` timestamp-prefix scheme: timestamps in
filenames are noise once frontmatter carries dates, and a descriptive slug makes wiki-embeds self-documenting
and survivable across re-ingests.

#### Images: embed-and-describe, not sidecar

1. Binary lands in `raw/assets/` with a descriptive slug.
2. The **curated page** (an entity/concept/memory page) **both embeds and describes** the image inline using
   an Obsidian wiki-embed: `![[motor-control-diagram-e4a408.png]]`.
3. No separate descriptive `.md` per image. The description is prose on the page that owns the image; the
   asset is referenced, never narrated in isolation.
4. Obsidian's attachment folder is set to `raw/assets`, so drag-drop in Obsidian and app writes converge on
   the same location. Binaries are committed as-is — **never base64**.

Because the embed uses the bare filename (Obsidian resolves `![[name.ext]]` vault-wide), curated pages don't
need the `raw/assets/` path prefix in the embed; the `sources:` frontmatter still records provenance with the
full path.

#### SCHEMA.md (PKM-tuned) — full body text

```markdown
# Vault Schema

## Domain
Personal knowledge management for one user. Two intertwined domains:
(1) a research/reference knowledge base (Karpathy LLM-Wiki layers), and
(2) life-admin: tasks, a media-to-consume backlog, and personal memories.
The vault is the single source of truth. Hindsight indexes it; it is never the store.

## Layers
- raw/      Immutable sources. The agent READS but NEVER edits these.
- entities/ concepts/ comparisons/ queries/   Curated, cross-linked knowledge pages.
- actions/ media/ memories/ people/   Life-admin pages (frontmatter `type` is the contract).
- index.md (Home) / SCHEMA.md / log.md   Navigational + structural backbone.

## Conventions
- File names: lowercase, hyphens, no spaces, no dates (dates live in frontmatter).
- Every page starts with YAML frontmatter (see Frontmatter).
- Link with [[wikilinks]]; every knowledge page needs >= 2 outbound links.
- Bump `updated` on every edit. Add every new page to index.md. Append every action to log.md.
- Images: embed inline with ![[asset.ext]] on the owning page AND describe them there.
  Binaries live in raw/assets/. No per-image sidecar files. Never base64.
- Provenance: on pages synthesising 3+ sources, append ^[raw/articles/source.md] to
  paragraphs whose claims trace to one source.

## Frontmatter
[the common + knowledge + life-admin contract above]

## raw/ Frontmatter
---
source_url: https://example.com/article   # if applicable
ingested: YYYY-MM-DD
sha256: <hex digest of the body below the closing --->
---
Compute sha256 over the body only. On re-ingest of the same URL: recompute, compare,
skip if identical, flag drift + update if changed.

## Tag Taxonomy
Add a tag HERE before using it (prevents sprawl). Seed set:
- Knowledge meta: entity, concept, comparison, query, summary, reference, how-to
- Domain (user-specific): embedded-systems, controls, accelerator, software, ai-ml, home
- People/Orgs: person, org, product, model
- Life-admin: task, media, memory, recurring, errand
- Quality: contested, prediction, controversy

## Page Thresholds
- CREATE a page when an entity/concept appears in 2+ sources OR is central to one.
- ADD to an existing page when a source mentions something already covered.
- DON'T create pages for passing mentions or out-of-scope detail.
- SPLIT a page over ~200 lines into sub-topics with cross-links.
- ARCHIVE fully-superseded pages to _archive/ and drop them from index.md.
- Life-admin pages are created on demand (one capture = one action/media/memory page)
  and do NOT need the 2-source threshold.

## Update Policy
On conflict: prefer newer dates; if genuinely contradictory, record both with dates
and sources, set `contradictions:` / `contested: true`, and flag in the lint report.
Never silently overwrite.
```

#### Provenance, confidence, and contradictions

- **Provenance.** `sources:` frontmatter lists the raw files a page synthesises. On pages that fuse 3+
  sources, claims carry inline footnote markers `^[raw/articles/source.md]` so a reader can trace a specific
  assertion to its origin.
- **Confidence.** `confidence: high|medium|low` (default unset ⇒ medium). Lint flags `low` and any
  single-source page with no `confidence`, prompting corroboration or demotion.
- **Contradictions.** When two sources genuinely conflict, record both with dates and sources, set
  `contested: true` and `contradictions: [other-slug]`, and never silently overwrite. Lint surfaces every
  contested page and same-topic pages stating different facts.

#### Worked example — knowledge page that embeds an image

`entities/program-motion-controller.md` (migrated from the proto-vault `img_e4a408e064c4.png` + its sidecar —
now one embed-and-describe page):

```markdown
---
title: Program Motion Controller (PMC)
type: entity
created: 2026-05-28
updated: 2026-05-30
source: slack
tags: [motor-control, embedded-systems, controls, product]
sources: [raw/assets/motor-control-diagram-e4a408.png]
confidence: medium
aliases: [PMC]
---

# Program Motion Controller (PMC)

Central coordinator in the motor-control stack: the PMC issues setpoints to the
[[drive-control-module]] (DCM), which drives the physical [[motor-rail-api]]. It
consumes **CS Demands** from the control system and exposes **CS Motor** state back.

![[motor-control-diagram-e4a408.png]]

The hand-drawn architecture above shows data flow PMC -> DCM -> Motor Rail/API, with
several components marked complete (red checkmarks) for progress tracking.
^[raw/assets/motor-control-diagram-e4a408.png]

## Key facts
- Role: central motion coordination.
- Talks to: [[drive-control-module]], [[motor-rail-api]].
- Inputs: CS Demands. Outputs: CS Motor state.

## Open questions
- Confidence is `medium`: single hand-drawn source; corroborate against a written spec.

## Related
- [[drive-control-module]] · [[ioc-network-architecture]]
```

The binary lives at `raw/assets/motor-control-diagram-e4a408.png`; there is **no** separate `img_*.md` file.

#### Worked example — Action / TODO page

`actions/fix-garden-fence.md`:

```markdown
---
title: Fix garden fence
type: action
created: 2026-05-30
updated: 2026-05-30
source: slack
tags: [task, home, errand]
status: todo
priority: 2 - High
due_date: 2026-06-07
recurrence: none
project: "[[home-maintenance]]"
people: ["[[people/jane-doe]]"]
---

# Fix garden fence

Two panels blown loose on the north side after the storm. Buy 2× featheredge
boards + galvanised nails; refit before the next forecast wind.

### Checklist
- [ ] Measure gap (approx 1.8 m)
- [ ] Buy boards + nails
- [ ] Refit and treat

### Notes
Linked to [[home-maintenance]]; coordinate with [[people/jane-doe]] for the weekend.
```

This page never needs an `index.md` entry — `_bases/actions.base` surfaces it under **Open Actions** / **Due
Soon**, and the daily 07:00 Slack briefing reads it via the same frontmatter.

#### Worked example — Media item

`media/ddia.md`:

```markdown
---
title: Designing Data-Intensive Applications
type: media
created: 2026-05-18
updated: 2026-05-30
source: slack
tags: [media, software, reference]
media_type: book
creator: Martin Kleppmann
url: https://dataintensive.net/
status: to_consume
priority: 3 - Medium
---

# Designing Data-Intensive Applications

Reference book on the architecture of data systems (replication, partitioning,
consistency, batch/stream processing). Captured for the to-consume backlog.

### Why
Background for the [[concepts/distributed-systems]] notes; covers the CAP
trade-offs filed at [[cap-theorem]].
```

### Spine templates — index.md / log.md

#### index.md — the Home landing page (seed template)

```markdown
---
title: Home
type: summary
cssclasses: dashboard-full-width
updated: 2026-05-30
---

# 🏠 PKM Vault — Home

> Source of truth for knowledge + life-admin. Total pages: N | Updated: YYYY-MM-DD
> Agents: read SCHEMA.md, this index, and recent log.md before any operation.

## 📥 Inbox (needs filing)
![[_bases/inbox.base]]

## ✅ Actions
![[_bases/actions.base]]

## 🎬 Media — to consume
![[_bases/media.base]]

## 🧠 Memories
![[_bases/memories.base]]

---

## Knowledge catalog
> One line per page: [[link]] — summary. Alphabetical within section.

### Entities
- [[program-motion-controller]] — central coordinator in the motor-control stack.

### Concepts
- [[ioc-network-architecture]] — RTEMS IOC boot/VLAN/TFTP design notes.

### Comparisons

### Queries

### People
- [[people/jane-doe]] — collaborator on home + controls work.
```

Scaling rule (from llm-wiki): split any knowledge section over 50 entries by first letter/sub-domain; once the
catalog passes 200 entries, add `_meta/topic-map.md` and link it here.

#### log.md (seed template)

```markdown
# Vault Log

> Chronological record of all agent actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, create, update, query, lint, archive, delete, reindex
> Rotate when this file exceeds 500 entries OR at year end: rename to log-YYYY.md, start fresh.

## [2026-05-30] create | Vault initialized
- Migrated from documents/ proto-vault
- Structure: raw/{articles,papers,transcripts,assets}, entities/, concepts/, comparisons/,
  queries/, actions/, media/, memories/, people/, _bases/, _archive/, inbox/
```

### Dashboards (Bases `.base` examples, Dataview fallback)

> **⚠️ Feasibility caveat — confirm Bases before treating it as load-bearing.** Obsidian **Bases** is a very
> new first-party feature with an evolving `.base` syntax. The dashboards, Home page, and *all* life-admin
> surfacing rest on Bases, so before shipping it as the navigation layer: (1) confirm the installed Obsidian
> version actually ships Bases, and (2) confirm the exact `.base` filter/date syntax below parses (especially
> the date arithmetic — see §15 open items).
>
> **Concrete fallback / v1 decision.** The **v1 target is Bases** *if it validates on the installed build*;
> otherwise fall back to **Dataview** — a `dataview` code block per view on the relevant index/Home page,
> e.g. open actions:
> ````
> ```dataview
> TABLE status, due_date, priority, project
> FROM "actions"
> WHERE status != "done" AND status != "completed" AND status != "cancelled"
> SORT priority ASC, due_date ASC
> ```
> ````
> A second fallback is **status-only Bases filters** (no date arithmetic) with the cron daily-briefing doing
> all date math (due/overdue/next-3-days), which it does anyway from frontmatter. Pick one and record it.

Bases definition files live in `_bases/` and are embedded by `index.md`. **Critical filter syntax: every
`filters:` block MUST be an object keyed by exactly one of `and:` / `or:` / `not:`. A bare YAML list is a
parse error; even a single condition is wrapped.** Nest `and`/`or`/`not` objects for compound logic.

`_bases/actions.base` — open tasks, then everything:

```yaml
filters:
  and:
    - file.inFolder("actions")
    - file.ext == "md"
properties:
  status:    { displayName: Status }
  due_date:  { displayName: Due }
  priority:  { displayName: Priority }
  project:   { displayName: Project }
  recurrence: { displayName: Repeat }
views:
  - type: table
    name: Open Actions
    filters:
      and:
        - status != "done"
        - status != "completed"
        - status != "cancelled"
    order: [file.name, due_date, priority, status, project, recurrence]
    sort:
      - { property: priority, direction: ASC }
      - { property: due_date, direction: ASC }
  - type: table
    name: Due Soon
    filters:
      and:
        - status != "completed"
        - due_date != ""
        - due_date < now() + "7 days"
    order: [file.name, due_date, priority]
  - type: table
    name: All Actions
    order: [file.name, due_date, priority, status, project]
```

`_bases/media.base` — backlog plus an OR view across the active states:

```yaml
filters:
  and:
    - file.inFolder("media")
    - file.ext == "md"
properties:
  media_type: { displayName: Type }
  creator:    { displayName: Creator }
  priority:   { displayName: Priority }
  status:     { displayName: Status }
  url:        { displayName: URL }
views:
  - type: table
    name: To Consume
    filters:
      and:
        - status == "to_consume"
    order: [file.name, media_type, creator, priority]
    sort: [{ property: priority, direction: ASC }]
  - type: table
    name: In Progress or Done
    filters:
      or:
        - status == "consuming"
        - status == "consumed"
    order: [file.name, media_type, status, creator]
```

`_bases/memories.base`:

```yaml
filters:
  and:
    - file.inFolder("memories")
    - file.ext == "md"
properties:
  people:      { displayName: People }
  location:    { displayName: Location }
  memory_date: { displayName: When }
  tags:        { displayName: Tags }
views:
  - type: table
    name: All Memories
    order: [file.name, people, location, memory_date, created, tags]
    sort: [{ property: memory_date, direction: DESC }]
```

`_bases/inbox.base` — surfaces unfiled captures using a `not:` filter to exclude noise:

```yaml
filters:
  and:
    - file.inFolder("inbox")
    - file.ext == "md"
properties:
  title:   { displayName: Title }
  created: { displayName: Captured }
  tags:    { displayName: Tags }
  source:  { displayName: Via }
views:
  - type: table
    name: Needs Filing
    filters:
      not:
        - file.name == "README"
    order: [file.name, created, source, tags]
    sort: [{ property: created, direction: DESC }]
```

`_bases/home.base` (optional union view embedded near the top of `index.md`) — recent activity:

```yaml
filters:
  and:
    - file.ext == "md"
views:
  - type: table
    name: Recent Captures (7d)
    filters:
      and:
        - file.mtime > now() - "7 days"
        - not:
            - file.inFolder("_archive")
    order: [file.name, type, created, file.mtime, tags]
    sort: [{ property: file.mtime, direction: DESC }]
```

### Routing & persona

#### Content-type / auto-categorization routing table (signal → type → folder)

The classifier maps each capture to exactly one `type`:

| Signal in the capture | type | Folder |
|---|---|---|
| Question directed at the agent | (no file) | answer inline; file to `queries/` only if non-trivial |
| Source to remember/synthesise (URL, PDF, pasted reference, how-to, explainer) | `concept` (or `entity` if it is one named thing) | `concepts/` / `entities/` |
| Audio / voice memo upload (Slack audio file) | (transcribe first, then route by content) | STT (`stt.provider: local` whisper) → `raw/transcripts/<slug>.md`, then curate as normal |
| A named person/org/product/model/device | `entity` | `entities/` (people → `people/`) |
| Explicit task / "remind me" / "I need to…" / `#project` task | `action` | `actions/` |
| Book/film/TV/podcast/article/video/music to consume later | `media` | `media/` |
| Personal/emotional moment, family, photo of people/places, milestone, holiday | `memory` | `memories/` |
| Side-by-side "X vs Y" analysis | `comparison` | `comparisons/` |
| Genuinely ambiguous | `inbox` | `inbox/` |

Disambiguation rules:
- Personal/emotional ⇒ `memory`, not `concept`, even if informational.
- For a YouTube/music URL captured for later, extract the real title from the page/URL for both `title` and
  the displayed name; never "YouTube Video".
- `#projectslug` in the message forces `project: "[[projectslug]]"` on an action and links it to that project
  page, which lives in `entities/` (a named project) or `concepts/` (a body of work) — there is no
  `projects/` folder. The legacy 2ndBrain `Reference` category maps to `concepts/`.
- An image alone is **not** a category — it attaches to whichever page owns it (entity/concept/memory),
  embedded and described there.

#### Persona — the `llm.py` system-prompt string

This is the persona text (formerly a `SOUL.md` file in the framework deployment) that becomes the
system-prompt string in `llm.py`. It makes the **vault canonical**, makes Hindsight a derived index, and bakes
in `obsidian://` retrieval and a concise tone:

```markdown
# PKM Agent Persona

You are a Personal Knowledge Management assistant — a second brain for one user
(Giles, Europe/London). You capture knowledge into a canonical Obsidian vault,
retrieve it with structural + semantic search, and always point the user back to
the real note in their own Obsidian.

## Source of truth
- The **Obsidian vault** (markdown files + binary assets in `raw/assets/`) is the
  ONLY canonical store. It is a git repo, two-way synced with the user's workstation.
- **Hindsight is a rebuildable index over the vault**, not a store. If it drifts,
  it gets reindexed from the vault.
- The small transient state DB is **working memory only** — never the knowledge base.
  Do NOT treat ingested content as "saved" because it is in a session or in Hindsight;
  it is saved only when it is a committed vault file.

## Capturing content (throw-it-and-forget)
1. Detect type: URL, markdown note, code, idea, quote, image, PDF, TODO/Action,
   media-to-consume, memory.
2. Pull the vault first (pull --rebase).
3. Immutable sources (uploaded articles/papers/transcripts/images) go to `raw/`
   (images to `raw/assets/`). Write the curated, cross-linked page in the right
   layer (entities / concepts / comparisons / queries) per SCHEMA.md.
4. Life-admin items (Actions/TODOs, media backlog, memories) are wiki pages with a
   frontmatter `type:` — never a rival folder tree. Set due/recurrence/priority on
   Actions from natural language.
5. Embed images inline with Obsidian wiki-embeds; the curated page describes AND
   embeds the asset. Never store base64. Never write a separate descriptive sidecar.
6. Auto-tag and cross-link. Never ask the user to file or tag.
7. Retain the page into Hindsight, attaching its vault path as a `rel` **tag** (the
   primary provenance channel, which survives LLM fact-extraction) with a
   `SOURCE: <path>` sentinel line as fallback; probe with recall that the page path
   comes back (auto_retain is off, so this is the only thing indexing the page). Append
   to `log.md`; then commit+push.
8. Confirm in 1–2 lines: what it is, where it landed, the tags applied.

## Retrieving content
1. Navigate structurally first (folders, `index.md`, wikilinks, Bases views), then
   use Hindsight semantic recall over CURATED pages to find by meaning.
2. Answer concisely from the vault, then ALWAYS offer the source:
   `obsidian://open?vault=pkm-vault&file=<url-encoded vault-relative path>`
   plus the plain vault-relative path and a `[[wikilink]]`.
3. Slack: render as mrkdwn `<url|title>`. MCP: markdown `[title](url)` + raw path +
   wikilink (the host may not make the custom scheme clickable).
4. Offer a Slack file upload ONLY if the user asks or clearly can't reach Obsidian.

## Proactive summaries (cron, Europe/London)
- Daily 07:00 and weekly Mon 07:00 to the user's Slack DM, composed FROM THE VAULT:
  due/overdue Actions, deadlines in the next 3 days, recent ingests, media-backlog
  nudges, emerging themes, review-flagged items. Use wikilinks as handles.

## Tone
- Concise. Acknowledge captures in 1–2 lines. Give retrieval results with their
  source links and nothing extra. You are an efficient, reliable tool, not a
  conversationalist. Prefer clean state — no cruft, no commented-out leftovers.

## Timezone: Europe/London (GMT/BST)
```

#### Summary content (daily / weekly), composed from the vault

**Daily:** (1) Due/overdue Actions (overdue flagged 🔴); (2) Upcoming deadlines next 3 days (next 72h
Europe/London); (3) Yesterday's ingests (new/changed `raw/` + curated pages from `log.md`/git, grouped by
kind); (4) Media-backlog nudge (one or two `to_consume` items); (5) Items flagged for review
(`review: true`/`status: review`).

**Weekly:** (1) Week-in-review of ingests with counts by kind; (2) Emerging themes (may use Hindsight
`reflect` over *curated* pages, never raw chatter); (3) Actions status (completion rate, still-open,
newly-overdue); (4) Next week's deadlines (all `due_date` in next 7 days); (5) Suggested review/archive (stale
or `review`-flagged pages, media going cold).

Worked example (delivered to Slack):

```
📋 Daily PKM Summary — Mon 2026-06-01 (Europe/London)

ACTIONS
  🔴 Overdue  — Reply to motor-control review     (due 2026-05-29)  [[actions/reply-motor-control]]
  🟡 Today    — Finish Q2 roadmap draft           (due today)       [[actions/q2-roadmap]]
  🟢 Next 3d  — Renew domain                       (due 2026-06-03)  [[actions/renew-domain]]

INGESTED YESTERDAY (4)
  • 2 articles  → distributed systems, event sourcing
  • 1 paper     → CAP theorem            [[cap-theorem]]
  • 1 image     → motor control diagram  [[program-motion-controller]]

MEDIA BACKLOG
  • Unread: "Designing Data-Intensive Applications" (added 12d ago)  [[media/ddia]]

FLAGGED FOR REVIEW
  • [[concepts/distributed-systems]] — 5 April cross-refs to re-check

Open any item in Obsidian via its wikilink, or ask me for a direct link.
```

### Retrieval & obsidian links

#### The QUERY operation (cost-ordered, structure-first)

```
query
  → read index.md (cheap, authoritative)            # structural orientation
  → known term or acronym?  → search_files            # lexical (FTS/grep over the vault)
  → known starting page?    → follow [[wikilinks]]    # graph navigation
  → phrasing-independent, "find me the page about…"   # semantic
        → hindsight.recall  →  returns vault page references
  → "connect / synthesize across pages"
        → hindsight.reflect
  → COMPOSE answer from the vault page(s)
  → RETURN answer + obsidian:// deep link(s) + vault-relative path(s) + [[wikilink]](s)
```

Structural navigation answers *"open the page I know exists"*; semantic recall answers *"find the page I can't
name."* The harness (not the model) attaches the canonical links so paths can't be fabricated.

#### `obsidian://` deep links — format detail

```
obsidian://open?vault=<VAULT_NAME>&file=<URL_ENCODED_VAULT_RELATIVE_PATH>
```

- **`vault=<VAULT_NAME>`** — the registered Obsidian vault name (`pkm-vault`), **not** a filesystem path. A
  fixed config value stored once as `OBSIDIAN_VAULT_NAME`; if the user renames the vault in Obsidian, this one
  value updates and nothing else changes.
- **`file=<path>`** — the **vault-relative** path (no leading slash, including the `.md` extension for notes;
  the file extension is **required** for non-markdown attachments), **URL-encoded** in full (path separators
  included).

Percent-encode space → `%20`, `/` → `%2F`, `#` → `%23`, `&` → `%26`, `?` → `%3F`, and other reserved
characters per RFC 3986.

| Vault-relative path | Encoded `file=` value |
|---|---|
| `entities/exa-search.md` | `entities%2Fexa-search.md` |
| `raw/papers/cap-theorem-2002.pdf` | `raw%2Fpapers%2Fcap-theorem-2002.pdf` |
| `raw/assets/motor-control-diagram-e4a408.png` | `raw%2Fassets%2Fmotor-control-diagram-e4a408.png` |
| `concepts/distributed systems.md` | `concepts%2Fdistributed%20systems.md` |

Full example link (single line):

```
obsidian://open?vault=pkm-vault&file=raw%2Fassets%2Fmotor-control-diagram-e4a408.png
```

Rendering per interface:

| Interface | Rendering | Notes |
|---|---|---|
| **Slack** | `mrkdwn` link `<obsidian://open?vault=pkm-vault&file=entities%2Fexa-search.md\|Open in Obsidian: exa-search>` renders as clickable text. | Slack shows a one-time confirmation on the non-http scheme (expected). If the workspace strips custom schemes, fall back to the raw `obsidian://` URI as plain text plus the vault-relative path. Keep link text short (the note title). |
| **MCP (Claude Code / claude.ai)** | Standard markdown `[Open in Obsidian: exa-search](obsidian://open?vault=pkm-vault&file=entities%2Fexa-search.md)` **and** the raw vault-relative path **and** a `[[wikilink]]`. | The host may not make a custom-scheme link clickable, so always include the **plain path** and the wikilink. |

Always include the **vault-relative path and `[[wikilink]]`** in both interfaces — they are scheme-independent
and survive copy/paste into Obsidian directly.

#### Returning originals (and when to upload)

The `obsidian://` link is always primary. Add a Slack file upload **only** when the user cannot reach Obsidian
for that artifact:
- The user explicitly asks to *see it here* / *send the file* / *upload it*.
- The artifact is a **binary attachment** (image/PDF) **and** the user is on a device where Obsidian may not
  be installed/synced — offer a one-line "want me to upload it here?" rather than dumping the file by default.
- Never upload large binaries pre-emptively (cost/clutter). For markdown notes, prefer the link + an inline
  excerpt over uploading the `.md`.

#### Worked example (Slack reply)

> **User:** what was that motor control diagram I saved, and the CAP theorem paper?
>
> **Agent:**
> Two hits from the vault:
>
> **1. Motor Control System Diagram** — hand-drawn architecture: PMC (Program Motion Controller) coordinates, DCM drives the hardware interface, CS Motor / CS Demands feed in, Motor Rail/API is the physical link. Several components are checked off as done. Filed under entities, image embedded inline.
> · Open note → **<obsidian://open?vault=pkm-vault&file=entities%2Fprogram-motion-controller.md|entities/program-motion-controller>**
> · Open image → **<obsidian://open?vault=pkm-vault&file=raw%2Fassets%2Fmotor-control-diagram-e4a408.png|raw/assets/motor-control-diagram-e4a408.png>**
> · Wikilink: `[[program-motion-controller]]`
>
> **2. CAP Theorem (Brewer/Gilbert-Lynch)** — paper on consistency/availability/partition-tolerance trade-offs; linked from your `concepts/distributed-systems` page.
> · Open note → **<obsidian://open?vault=pkm-vault&file=entities%2Fcap-theorem.md|entities/cap-theorem>**
> · Open PDF → **<obsidian://open?vault=pkm-vault&file=raw%2Fpapers%2Fcap-theorem-2002.pdf|raw/papers/cap-theorem-2002.pdf>**
> · Wikilink: `[[cap-theorem]]`
>
> Want either file uploaded here instead?

The MCP variant swaps the `mrkdwn` `<url|label>` syntax for markdown `[label](url)` links and keeps the bare
paths and wikilinks verbatim.

### Reindex job

This is the mechanism that makes "vault canonical, index disposable" real. The job walks **every** curated
markdown page in the vault and retains its salient facts into Hindsight, keyed by page path, **skipping pages
unchanged since the last run**.

**Idempotency contract.** One Hindsight reference == one vault-relative page path. A **per-page content hash**
over the page **body** (everything after the closing frontmatter `---`) decides whether to reprocess — reusing
the wiki's `sha256`-of-body convention. The hash for each curated page is tracked in an index-side manifest
**outside** the vault (`~/.thoth/hindsight/reindex-manifest.json`, `.gitignore`d), so reindex never churns
curated pages' `updated:` dates.

> **⚠️ Illustrative pending API verification.** The class/method names below (`Hindsight(...)`, `hs.forget`,
> `hs.forget_bank`, `hs.retain(reference=…)`) are **not attested** against any installed Python client. **The
> implemented path is the subprocess-over-CLI version (below)**, which drives the **official `hindsight` CLI**
> (https://hindsight.vectorize.io/sdks/cli) — binary `hindsight` (env-overridable; the VPS still has
> `hindsight-embed`), `-p <profile>`, **bank id positional**, two-token `memory retain|recall <bank> …`, recall
> via `-o json`, **tags carry provenance**. **Do not ship this Python sketch verbatim.** The body-hash
> idempotency, path attachment, and prune-on-delete logic are identical between the two; the real code is
> `src/thoth/reindex_from_vault.py` + `src/thoth/hindsight.py`.

```python
#!/usr/bin/env python3
# reindex_from_vault.py  — ILLUSTRATIVE (verify client symbols first)
# Rebuilds / refreshes the Hindsight index from the canonical Obsidian vault.
# Idempotent: unchanged pages (by body sha256) are skipped.
#
# WARNING: `Hindsight`, `.forget`, `.forget_bank`, and retain(reference=) are NOT a real
# Python client. The IMPLEMENTED path is the CLI/subprocess variant below (official
# `hindsight` CLI: binary `hindsight`, bank positional, `memory <verb>`, recall -o json).

import hashlib, json, os, re
from pathlib import Path
from datetime import datetime, timezone
# from hindsight_client import Hindsight   # <-- UNVERIFIED symbol; confirm before importing

VAULT     = Path(os.environ["PKM_VAULT"])           # /opt/pkm-vault
MANIFEST  = Path.home() / ".thoth/hindsight/reindex-manifest.json"
BANK      = "thoth"                                 # renamed off hermes; env THOTH_HINDSIGHT_BANK

# Only curated, fact-bearing pages are indexed. raw/ is immutable source bytes;
# navigational/meta files are structure, not facts; underscore dirs are excluded.
INDEXED_DIRS = ("entities", "concepts", "comparisons", "queries")
SKIP_FILES   = {"SCHEMA.md", "index.md", "log.md"}   # index.md IS the Home landing page; there is no separate Home.md

def _now(): return datetime.now(timezone.utc).isoformat()

def body_hash(md: str) -> str:
    body = re.sub(r"^---\n.*?\n---\n", "", md, count=1, flags=re.DOTALL)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

def page_type(md: str) -> str:
    m = re.search(r"^type:\s*(\S+)", md, flags=re.MULTILINE)
    return m.group(1) if m else "page"

def retain_text(rel: str, md: str) -> str:
    # Portable path-pointer: prefix a parseable SOURCE: sentinel so recall results
    # carry the vault path even if the client has no first-class `reference` field.
    return f"SOURCE: {rel}\n\n{md}"

def main(full_rebuild: bool):
    hs = Hindsight(mode="local_embedded", bank_id=BANK)   # <-- UNVERIFIED constructor
    manifest = {} if full_rebuild else json.loads(MANIFEST.read_text() or "{}")
    if full_rebuild:
        hs.forget_bank(BANK)          # index lost / cold start: wipe and re-derive

    seen, changed, skipped = set(), 0, 0
    for d in INDEXED_DIRS:
        for path in (VAULT / d).rglob("*.md"):
            rel = str(path.relative_to(VAULT))      # the stable page path / pointer
            if path.name in SKIP_FILES:
                continue
            seen.add(rel)
            md  = path.read_text(encoding="utf-8")
            h   = body_hash(md)
            if manifest.get(rel, {}).get("sha256") == h and not full_rebuild:
                skipped += 1
                continue                            # unchanged -> skip (idempotent)

            hs.forget(reference=rel)                # drop stale facts (if upsert-by-ref unsupported)
            hs.retain(                              # re-extract & re-embed via Gemini
                text=retain_text(rel, md),          # SOURCE: sentinel carries the path
                tags=[page_type(md), rel],          # path also tagged for recall filtering
                # reference=rel,                    # add ONLY if verified to exist
                retain_context="a curated page filed in the Obsidian vault",
            )
            manifest[rel] = {"sha256": h, "retained_at": _now()}
            changed += 1

    for gone in set(manifest) - seen:               # prune deleted pages
        hs.forget(reference=gone)
        del manifest[gone]

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"reindex: {changed} updated, {skipped} unchanged, {len(seen)} live pages")
```

**Concrete CLI/subprocess variant (the IMPLEMENTED path — official `hindsight` CLI).** The binary is
`hindsight` (env `THOTH_HINDSIGHT_BINARY`; VPS still has `hindsight-embed`), `-p <profile>` is the optional
named profile (env `THOTH_HINDSIGHT_PROFILE`, **not** the bank), the **bank id is positional**, the verbs are
two tokens under `memory`, and recall is parsed from `-o json`. The exact binary/verb/flag spelling and the
per-hit tag round-trip are confirmed against the installed binary at VPS-time, but the shape is:

```python
import subprocess
PREFIX = ["hindsight"]            # + ["-p", profile] when a profile is configured
BANK   = "thoth"                  # positional bank id (env THOTH_HINDSIGHT_BANK)

def hs_retain(rel: str, md: str, ptype: str):
    # TAGS are the primary provenance channel (LLM fact-extraction can split a page and
    # strand the in-band SOURCE: sentinel); the sentinel is kept as a fallback.
    subprocess.run(PREFIX + ["memory", "retain", BANK, f"SOURCE: {rel}\n\n{md}",
                             "--tags", f"{ptype},{rel}"], check=True)   # wrap in tenacity retry

def hs_recall(query: str):
    # -o json: parse structured output, recover the path from each hit's `rel` tag.
    out = subprocess.run(PREFIX + ["memory", "recall", BANK, query, "-o", "json"],
                         check=True, capture_output=True, text=True).stdout
    return out   # parse_recall(out): tag-first, SOURCE: fallback

def hs_forget(rel: str):
    # No per-path forget in the official surface; best-effort, no retry — the
    # authoritative reset is a --full-rebuild wipe (see below).
    subprocess.run(PREFIX + ["memory", "forget", BANK, rel], check=False)
```

The official surface has no per-page `forget`, so: incremental runs *add* (duplicate facts deduped or
tolerated), and **`--full-rebuild` is the authoritative reset** — wipe the bank
(`hindsight … db reset <bank>` / equivalent, confirm name) and re-retain every live page. Because the vault is
canonical, a periodic full rebuild always converges the index regardless of upsert support. The checked
`retain`/`recall` calls are wrapped in a bounded `tenacity` retry (transient-only; fail-fast on bad args/auth).

Properties: **idempotent** (no changes ⇒ zero LLM/embedding work); **self-healing on index loss** (run
`--full-rebuild` to wipe and deterministically re-derive from the vault); **deletion-aware** (stale facts
pruned where per-page forget exists, otherwise cleared by full rebuild); **scoped to curated knowledge**
(`raw/` and underscore/structure files excluded); **client-agnostic** (path carried as a `rel` **tag** —
surviving LLM fact-extraction — with the in-band `SOURCE:` sentinel as a fallback).

**Three triggers.** (1) **Per-ingest, incremental (primary)** — the ingest retains exactly the pages it
touched and probes they landed; no separate job for the common case. (2) **Nightly catch-up (safety net)** —
system cron at 06:30, right after the daily pull and before the 07:00 summary, runs the incremental reindex to
pick up **out-of-band** edits (pages the user wrote directly in Obsidian and pushed via Obsidian Git, which the
appliance never saw); pin an explicit Gemini model (`gemini-2.5-flash`). (3) **Full rebuild, manual /
on-recovery** — `PKM_VAULT=/opt/pkm-vault python3 reindex_from_vault.py --full-rebuild`.

**Retain example — page references, not chatter.** For the curated page
`entities/program-motion-controller.md`, Hindsight retains (illustrative, every fact recoverable to the same
path — primarily via its `rel` **tag**, with the `SOURCE:` sentinel as fallback):

```text
path = "entities/program-motion-controller.md"
  (added to tags as the primary provenance channel — `rel:<path>` — so it survives the
   LLM fact-extraction that may split the page into atomic facts; each fact text also
   begins with a fallback sentinel line "SOURCE: entities/program-motion-controller.md")
 ├─ world fact: "PMC = Program Motion Controller, central coordinator of the motor-control stack"
 ├─ world fact: "PMC drives the DCM (Drive Control Module) hardware interface"
 ├─ world fact: "PMC consumes CS Demands from the control system"
 └─ (entity-graph edges: PMC—DCM, PMC—IOC network architecture)
```

A recall returns the **page path** (recovered from each hit's `rel` tag, with the `SOURCE:` line as fallback),
which the chat layer renders as a clickable `obsidian://` deep link plus the vault-relative path.

**Cost / tuning notes (Gemini free-tier sizing).** Both extraction (`gemini-2.5-flash`) and embeddings
(`text-embedding-004`, 3,072-dim) run on the **Gemini free tier** — ~60,000 `text-embedding-004`
embeddings/month; the Anthropic key is never used here. Initial full reindex of 100 curated pages ≈ free-tier
covered; per-ingest incremental (1 source → 5–15 page touches) ≈ negligible; nightly catch-up (only *changed*
pages) ≈ negligible (unchanged skipped); daily search (10 recalls) ≈ ~$0.002; weekly `reflect` summary ≈
~$0.03. Caveat: curated pages are the **churning layer** (the two-way edit model means the user edits pages
directly in Obsidian, bumping body + `updated:`), so nightly cost scales with the number of pages edited in
Obsidian since the last run — near-zero on quiet days, higher after a heavy editing session, **not** flat
~zero. To bound it if it bites: debounce re-embeds, or index a normalised fact-extract rather than the full
marked-up body. The genuinely expensive event remains a **full rebuild** (one embedding per indexed page) —
schedule those deliberately and rely on incremental runs day to day.

### Git wrappers & .gitignore

The vault is a normal git working tree on both ends — no rsync, no bespoke daemon, no cloud-drive mount, just
git. Both helper scripts use `gh`'s credential helper for auth (never SSH, never a PAT-in-URL) and
`GIT_CONFIG_GLOBAL=/dev/null` so any global `insteadOf` ssh-rewrite cannot hijack the HTTPS remote. They move
into `thoth/bin/` as-is.

`thoth/bin/vault-pull` (run before any write):

```bash
#!/usr/bin/env bash
set -euo pipefail
VAULT="${PKM_VAULT:-/opt/pkm-vault}"
GIT_CONFIG_GLOBAL=/dev/null git -C "$VAULT" \
    -c credential.helper='!gh auth git-credential' \
    pull --rebase --autostash origin main
```

`thoth/bin/vault-commit` (run after an ingest batch completes):

```bash
#!/usr/bin/env bash
set -euo pipefail
VAULT="${PKM_VAULT:-/opt/pkm-vault}"
MSG="${1:-ingest $(date -u +%Y-%m-%dT%H:%MZ)}"
cd "$VAULT"
git add -A
git diff --cached --quiet && { echo "nothing to commit"; exit 0; }
# Commit FIRST so the working tree is clean, THEN rebase onto any Obsidian pushes that
# landed during the batch. Committing before the rebase means no autostash is needed, so a
# conflicting Obsidian push surfaces as a clean rebase conflict (fail loudly) instead of a
# half-applied mid-stash-pop state. The agent never --force pushes.
git commit -m "agent: $MSG"
if ! GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
        pull --rebase origin main; then
    echo "VAULT CONFLICT during rebase — resolve in Obsidian; not pushing." >&2
    GIT_CONFIG_GLOBAL=/dev/null git rebase --abort || true
    exit 1   # wrapper surfaces the conflicting path over Slack; never clobber
fi
GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
    push https://github.com/<owner>/pkm-vault.git main
```

The ingest flow calls `vault-pull` at the start of any ingest/curation turn and `vault-commit "<topic>"` once
the page(s) and any `raw/assets/` binaries are written. Because the appliance is single-process and serialises
its turns, it never races itself; the only other writer is Obsidian on the workstation.

**Conflict strategy.** `raw/` is immutable (two writers can never edit the same raw file; new uploads land
under unique, content-addressed names); one file per topic; both ends pull with `--rebase --autostash` (agent)
or `merge` (Obsidian) so disjoint files auto-merge with zero intervention. On the occasional real conflict git
leaves standard markers in the one affected `.md`; `vault-pull`/`vault-commit` fail loudly (non-zero exit)
rather than clobbering, and the wrapper surfaces the path over Slack ("merge conflict in `concepts/raft.md`,
resolve in Obsidian"). The human resolves in Obsidian; the next auto-commit clears it. **Never** `--force`
push. Attachments/binaries under `raw/assets/` are immutable and uniquely named, so they only ever *add*.

**Workstation side — Obsidian Git plugin.** Install **Obsidian Git** (`denolehov/obsidian-git`). Recommended
settings: vault backup interval `10` min, auto-pull interval `10` min, pull-on-startup ON, push-on-backup ON,
commit message `vault backup {{date}} ({{hostname}})`, sync method `merge` (rebase acceptable), pull-before-
push ON, list changed files in commit body ON, notifications ON. In **Settings → Files & Links** enable
**"Detect all file changes"** — mandatory, or externally written files (the agent's `raw/assets/` commits, new
`.md` pages) are not detected until a manual reload. On mobile, set the backup interval to `15` and rely
primarily on app-foreground pull.

Vault `.gitignore`:

```gitignore
# pkm-vault/.gitignore — keep the vault clean and conflict-free
.obsidian/workspace.json        # per-device pane layout; churns constantly
.obsidian/workspace-mobile.json
.obsidian/cache
.trash/
.DS_Store
*.tmp
.obsidian/plugins/obsidian-git/.gitignore
```

Track the rest of `.obsidian/` (notably `plugins/obsidian-git/data.json` and `core-plugins.json`) so a freshly
cloned device inherits the same plugin configuration.

**GitHub auth.** HTTPS + a **fine-grained PAT** via `gh`'s credential helper:

```bash
echo "$PAT" | gh auth login --with-token   # fine-grained PAT
gh auth setup-git                            # install gh as git credential helper for github.com
```

The vault PAT is scoped to **only** `pkm-vault` (Contents: Read & write + Metadata: Read-only); the config
backup PAT is a separate token scoped to **only** `thoth`. A fine-grained PAT scoped to two *pre-existing*
repos **cannot create new repos**, so create `pkm-vault` once in the GitHub web UI (or use a broader token for
the one-time create). Fine-grained PATs expire — issue with a 90-day expiry and schedule a rotation reminder
(an `actions/` page with `recurrence: monthly`) ~1 week before the mark, since an expired PAT silently breaks
the unattended push and config backup.

### Backup/recovery

The backup model follows from the source-of-truth decision: **the `pkm-vault` repo *is* the durable knowledge
backup**, Hindsight is disposable, and app config has its own separate push-only backup.

| Asset | Backup mechanism | Recoverable from | RPO |
|---|---|---|---|
| Knowledge (vault markdown + `raw/assets/` binaries) | `pkm-vault` git history on GitHub + every cloned device | `git clone`; any commit = a point-in-time snapshot | ≤ minutes |
| Hindsight semantic index (local Postgres, `bank_id=thoth`) | **Rebuilt** (disposable); OPTIONAL gated `pg_dump` + manifest snapshot for faster cold start (`bin/hindsight-backup.sh`, ~3 generations) | the reindex job (`--full-rebuild`); or restore the latest snapshot | n/a (regenerated) |
| App code + config (`thoth` repo) | `thoth` repo, config-backup cron | `git clone` of `thoth` | 6h |
| Transient state (`~/.thoth/state.db`) | **Not backed up** (disposable) | start fresh — only dedupe history + mid-flight captures lost, both cheap | n/a |
| Secrets (`~/.thoth/.env`) | **Never** in any repo — password manager only | manual re-entry | n/a |

Losing the transient state DB loses nothing canonical. Knowledge is safe in the vault repo.

**`config-backup.sh`** (push-only backup of the **`thoth`** config repo; the only edit from the framework
version is the push target). Most of what the framework script backed up (session/kanban DBs) ceases to exist
in the thin app, so the DB-snapshot loop is optional and only runs if any transient DB is present:

```bash
#!/usr/bin/env bash
# thoth/bin/config-backup.sh
# Push-only backup of app config to the thoth repo.
# Secrets (.env) are NEVER committed — excluded via the repo .gitignore.
set -euo pipefail

THOTH_HOME="${THOTH_HOME:-$HOME/.thoth}"
TS="$(date -u +%Y-%m-%dT%H:%MZ)"

# Commit + push to the thoth config-backup repo (NOT the vault repo).
# .env / secrets stay gitignored; the vault is untouched (it has its own per-ingest push).
cd "$THOTH_HOME"
git add -A
if git diff --cached --quiet; then
  echo "[$TS] no config changes to back up"
else
  git commit -m "backup $TS"
  GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
    push https://github.com/<owner>/thoth.git main
  echo "[$TS] config backup pushed"
fi
```

Wired into system cron:

```cron
# system crontab — config-backup repo every 6h (push-only)
0 */6 * * * /opt/thoth/bin/config-backup.sh >> /var/log/thoth-config-backup.log 2>&1
```

**Full recovery from a lost VPS** (simplified — no session-DB-as-store to restore):
1. Provision a new VPS (Ubuntu 24.04+, 2 cores, 8 GB RAM, 50 GB+ disk); install prereqs + `uv`.
2. `echo "$PAT" | gh auth login --with-token`, then clone `thoth` with the inline `gh` helper + nulled global
   config (so the user's `insteadOf` ssh-rewrite cannot hijack the HTTPS URL):
   ```bash
   GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
     clone https://github.com/<owner>/thoth.git /opt/thoth
   ```
3. Clone the canonical vault (the knowledge restore — nothing else is needed):
   ```bash
   GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' \
     clone https://github.com/<owner>/pkm-vault.git /opt/pkm-vault
   ```
4. Re-add secrets manually from the password manager (`~/.thoth/.env`, `chmod 600`).
5. **Rebuild the index** from the vault: `PKM_VAULT=/opt/pkm-vault python3 reindex_from_vault.py --full-rebuild`.
6. Re-enable the systemd unit (`thoth-slack.service`) + system cron, then run the verification checklist.

Estimated recovery time ~1–2 h, dominated by package installs and the reindex pass; the knowledge itself is
restored the instant the vault clone completes. **Scaling note:** plain git is good to ~1 GB; when
`raw/assets/` growth pushes the repo toward ~1 GB, migrate binaries to **Git LFS** (10 GB free) or move the
asset tree to **restic → Backblaze B2** while keeping the markdown in plain git. Later optimisation, not an
upfront requirement.

### Lint checks

A pure programmatic markdown scan across all `.md` files, reporting grouped by severity and appending one
`log.md` entry. Runs weekly via cron and on demand after a bulk migration/restore; independent of the reindex
job.

| # | Check | Action |
|---|---|---|
| 1 | **Orphan pages** | Knowledge pages with zero inbound `[[wikilinks]]` (life-admin pages exempt — Bases surface them). Suggest a link or archive. |
| 2 | **Broken wikilinks** | `[[link]]` targets that resolve to no file (respect `aliases`). Highest severity. |
| 3 | **Index completeness** | Every knowledge page appears in `index.md`; flag missing and stale "Total pages". |
| 4 | **Frontmatter validation** | Required common fields present; `type` valid; type-specific required fields present (e.g. `action` has `status`); values match Metadata Menu vocab. |
| 5 | **Stale content** | Knowledge `updated` > 90 days older than the newest source touching the same entities; **life-admin**: `action` past `due_date` and not done/cancelled; `media` `to_consume` > 180 days. |
| 6 | **Contradictions** | Surface every page with `contested: true` or non-empty `contradictions:`; flag same-topic pages stating different facts. |
| 7 | **Source drift** | For each `raw/` file with `sha256:`, recompute over body; mismatch ⇒ raw edited (shouldn't happen) or source URL changed. Report, don't hard-fail. |
| 8 | **Quality signals** | List `confidence: low` and single-source pages with no `confidence` — corroborate or demote. |
| 9 | **Page size** | Knowledge pages > 200 lines ⇒ split candidates. |
| 10 | **Tag audit** | Every tag in use must exist in SCHEMA.md taxonomy; flag strays. |
| 11 | **Image hygiene** | Assets in `raw/assets/` with no `![[…]]` embed anywhere = orphan binaries; pages embedding a missing asset = broken embed; any surviving per-image sidecar `.md` (legacy pattern) flagged for merge-into-owner-page. |
| 12 | **Log rotation** | If `log.md` > 500 entries, rotate to `log-YYYY.md`. |
| 13 | **Report + log** | Group by severity (broken links/embeds > orphans > source drift > contested > stale/overdue > style). Append `## [YYYY-MM-DD] lint | N issues found`. |

### Migration

Today's state: a `documents/` proto-vault with `images/` holding three binaries and **separate sidecar `.md`
descriptions** (`img_e4a408e064c4.png` + `img_e4a408e064c4.md`; `img_ed814b035583.png` + `img_ed814b035583.md`);
the third image `img_85e18fa08300.jpeg` has **no sidecar** (orphan to fix). Migration converts each sidecar
into a curated page that *embeds* its image, relocates binaries into `raw/assets/`, generates the wiki spine,
and reindexes.

1. **Snapshot the current state** (tag `pre-migration` as a safety net).
2. **Create and clone the empty private `pkm-vault` repo.**
3. **Lay down the directory skeleton + `.gitignore`** (the folder tree above).
4. **Move binaries into `raw/assets/`** (immutable layer), renaming to descriptive slugs:
   ```
   img_e4a408e064c4.png  -> raw/assets/motor-control-diagram-e4a408.png
   img_ed814b035583.png  -> raw/assets/ioc-network-diagram-ed814b.png
   img_85e18fa08300.jpeg -> raw/assets/uncategorized-image-85e18f.jpeg   # orphan: no sidecar; neutral slug until described
   ```
   The two diagrams carry topic slugs; the orphan jpeg gets a neutral `uncategorized-image-` slug because it is
   **not** a motor-control diagram and its subject is unknown until step 5.
5. **Convert each sidecar `.md` into a curated page that embeds its image** (no base64, no surviving sidecar):

   | Old sidecar | New curated page | Transform |
   |---|---|---|
   | `img_e4a408e064c4.md` | `entities/program-motion-controller.md` | fold description into an entity page that **embeds** `motor-control-diagram-e4a408.png`; delete sidecar |
   | `img_ed814b035583.md` | `concepts/ioc-network-architecture.md` | embed `ioc-network-diagram-ed814b.png`; preserve its "Questions Noted" as an **Open questions** section; delete sidecar |
   | `img_85e18fa08300.jpeg` (orphan) → `raw/assets/uncategorized-image-85e18f.jpeg` | `inbox/uncategorized-image-85e18f.md` (`type: inbox` stub) | embed `![[uncategorized-image-85e18f.jpeg]]`, vision-describe it, then refile to the right `entities/`/`concepts/`/`memories/` page once the subject is known — so no asset is left undocumented. This is **not** PMC's diagram (that is `e4a408`). |

6. **Generate the llm-wiki spine** — `SCHEMA.md`, `index.md`, `log.md` (templates above) plus the
   `_bases/*.base` dashboards. `log.md` is seeded with the migration entries.
7. **Commit and push the migrated vault.**
8. **Run the initial Hindsight full-rebuild reindex** so the index reflects the new vault, not old chatter;
   verify a recall for "motor control" returns `entities/program-motion-controller.md`.

(In the thin-app cut-over there is no `SOUL.md` file to rewrite and no framework cron/Hindsight to re-point —
the persona lives in `llm.py`, the schedules are system cron, and Hindsight is called directly; see §14.)

**Post-migration:** run a full **lint** pass (image-hygiene clears once sidecars are folded in, index
completeness populated).
