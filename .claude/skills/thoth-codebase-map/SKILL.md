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

## Finding your way around `src/thoth/`

Most filenames say what they are (`slack_app.py`, `query.py`, `summary.py`,
`lint.py`, `budget.py`, `config.py`, `state.py`, `git_sync.py`, `mcp_server.py`) —
`ls src/thoth/` plus each module's docstring is the authoritative, never-stale
map, so this skill deliberately does **not** restate it (a hand-kept file list
just drifts). Only the non-obvious ones, where the name doesn't reveal the role:

- `extract.py` vs `analyse.py` — `extract.py` *fetches* external content (Exa
  search, Firecrawl page→Markdown, Whisper transcription); `analyse.py` does
  vision / OCR / PDF analysis. Easy to reach for the wrong one.
- `llm.py` — the **single Anthropic seam** (`LLM.complete`); also owns the
  classify/curate prompts and the curate file-plan JSON schema + validation.
  Change LLM behavior here, via the prompt — not with output post-processing.
- `hindsight.py` — thin wrapper around the **external Hindsight CLI** (see the
  foot-gun below); `reindex_from_vault.py` rebuilds that index from the vault.
- `ingest.py` — the bounded **8-pass capture pipeline** (persist raw → classify →
  capture_raw → fetch_candidates → curate → retain → commit → report); raw is
  persisted *before* any LLM call so nothing is lost on restart.
- `intent.py` — the cheap Haiku **intent gate** (see behavior note below).

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

### The two retrieval modalities are complementary, not redundant

`QueryEngine.answer` (behind `pkm_search`, and reused by `ResearchEngine.ask`
for `pkm_ask`) runs cost-ordered passes with a short-circuit: **grep** over the
curated folders → **wikilink** graph-follow → **semantic recall** via Hindsight,
where recall fires **only when the cheap passes returned fewer than `max_pages`
candidates** (the #107 "thin top-up" — grep hits always lead the rank; recall
appends to fill). So a query that gets enough grep hits never consults Hindsight
at all (`used_recall: false`).

Do **not** assume "#98 put title+body in Hindsight, so grep is now redundant."
It isn't, for durable reasons:

- **grep queries the store of record (the vault bytes); Hindsight is a derived,
  lossy, eventually-consistent index.** `retain` runs **LLM fact-extraction**, so
  Hindsight holds *extracted facts*, not the text — exact tokens (policy numbers,
  IDs, filenames, exact names) may not survive into a retrievable fact, and
  embedding recall is weak at rare exact strings anyway. grep matches the actual
  bytes.
- **Freshness/coverage windows where a filed page is grep-only:** a retain
  deferred on a budget trip (left for the next reindex), the reindex/rebuild
  window, a just-written page, or Hindsight down / subprocess failure (recall is a
  `subprocess`-spawned CLI, ~120 s timeout, retried).

So grep = exact / authoritative / always-fresh; Hindsight = semantic /
vocabulary-bridging but derived and lossy. #98 closed the *coverage* gap (every
curated page now retains ≥1 recallable unit); it did **not** make Hindsight
lossless or synchronous. The open design direction (issue #143) is to **blend**
both candidate sets (e.g. Reciprocal Rank Fusion, `k=60` — fuse on rank so grep's
token-count score and Hindsight's similarity needn't be normalised) rather than
keep grep as a gate that suppresses recall.
