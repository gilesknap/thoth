---
name: thoth-codebase-map
description: >-
  Orientation map for the thoth codebase — what to read first, what each module
  does, the vault page-type model, and the Hindsight semantic-index reality. Use
  when starting work on thoth and you need to know where things live or which file
  owns a responsibility, before diving into a feature or bug.
---

# thoth codebase map

thoth is a self-hostable personal knowledge-management appliance: messages/files
go to a Slack bot, an LLM pipeline classifies + curates + files them as Markdown
notes in an Obsidian vault (git-backed), and Claude reaches the vault through an
MCP server.

## Read these first

- `CLAUDE.md` — the working rules (notably: no backward-compat / migration prose).
- `SPEC.md` — the authoritative spec; sections are referenced throughout the code.
- `docs/explanations/architecture.md` — the high-level pipeline diagrams.
- `docs/adr/` (a.k.a. `docs/explanations/decisions/`) — the binding design
  decisions. ADR 0005 = the folder/page-type model; check here before changing
  classification, folders, or the curate contract.

## Two invariants you must preserve

1. **The vault is canonical.** The semantic index, the SQLite state, the LLM
   calls are all disposable, rebuildable projections of the plain-Markdown git
   repo. Never make the vault depend on a derived store; anything authored must
   live in the vault, not only in an index. (This is why a page's one-line
   `summary` lives in page frontmatter, not only in `index.md`.)
2. **The tool surface is closed.** The appliance LLM has no shell and no arbitrary
   filesystem access — only a fixed set of validated, path-confined tools. Don't
   add a tool that can escape the vault or run commands.

## Module map (`src/thoth/`)

- `slack_app.py` — Slack Socket-Mode handler: dedup, channel gate, allow-list,
  routing, threaded replies, save-confirm.
- `intent.py` — the cheap Haiku **intent gate** that routes bare free text to
  capture / query / ask.
- `ingest.py` — the bounded **8-pass capture pipeline** (persist raw → classify →
  capture_raw → fetch_candidates → curate → retain → commit → report).
- `extract.py` — external fetch: Exa (web search), Firecrawl (page→Markdown),
  Whisper (audio transcription).
- `analyse.py` — vision / OCR / PDF analysis (Anthropic vision).
- `llm.py` — the single Anthropic seam (`LLM.complete`); classify/curate prompts +
  the curate file-plan JSON schema and its validation.
- `vault.py` — path-confined reads/writes, frontmatter + schema validation, the
  page-type contracts.
- `hindsight.py` — wrapper around the external Hindsight CLI (semantic index).
- `reindex_from_vault.py` — rebuilds the Hindsight index from the canonical vault.
- `query.py` — vault-only retrieval, **cost-ordered** (grep → wikilink traversal →
  Hindsight recall; cheapest first, recall only as a last resort).
- `research.py` — blended web+vault Q&A (`pkm_ask`); model decides when to hit the
  web, can offer to save the answer back as a note.
- `mcp_server.py` — FastMCP server exposing the `pkm_*` tools (stdio).
- `summary.py` — composes + posts the Slack digest.
- `lint.py` — scans the vault for maintenance issues (the canonical invariants).
- `budget.py` — the daily cost circuit-breaker (`THOTH_DAILY_LLM_BUDGET`).
- `config.py` — frozen `Config` from env; `state.py` — SQLite dedupe/markers/budget.
- `git_sync.py` — vault pull/commit wrappers. `alerts.py` — Slack alerting.
- `templates/` — the seed spine (`index.md`, `SCHEMA.md`, `log.md`) + `_bases/*.base`
  dashboards, loaded via importlib resources.

## Vault page-type model (ADR 0005)

Four flat content folders plus machinery:

- **Reference pages** — `entities/`, `notes/`, `memories/` (lifecycle-free).
  These carry a one-line `summary:` in frontmatter, authored by the curate LLM.
- **Action pages** — `actions/` (todos + the to-consume media queue). Carry
  `status`/`due`; surfaced by the `_bases` dashboards, **not** glossed with a
  summary.
- `inbox/` — durable pre-curate holding pages. `raw/` — immutable sources.
- `index.md` is a **static** Home dashboard (title + `.base` embeds); agents never
  write to it. `log.md` is the activity log.

**Intent-gate behavior (useful when testing):** a plain declarative fact / concept
/ person / memory classifies as a reference page (and gets a `summary`); phrasing
like "read / watch / buy / book / meet / remember to X" classifies as an `action`
(no summary). To exercise the reference/summary path, the test capture must be
declarative, not a task.

## Hindsight reality (foot-gun)

Hindsight is a **fact-extraction** engine (entities/observations/chunks for a
query), **not** token-chunking, and it exposes **no page-level embeddings or
pairwise cosine**. Features framed as "reuse Hindsight embeddings + a cosine
threshold" (near-duplicate detection, idea-mining similarity) are therefore not
buildable as written — they need a separate embedding-access decision first. The
index is a rebuildable projection; recall is the last/most-expensive query pass.
