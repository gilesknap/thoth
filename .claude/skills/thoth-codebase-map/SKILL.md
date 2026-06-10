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

Most names say what they are (`slack_app/`, `query/`, `summary/`, `lint/`,
`budget/`, `config/`, `state/`, `git_sync.py`, `mcp_server/`) — `ls src/thoth/`
plus each module's docstring is the authoritative, never-stale map, so this skill
deliberately does **not** restate it (a hand-kept file list just drifts). The
larger subsystems are **packages**: the package `__init__` is the public surface
(import from the package, never a submodule) and its docstring is the map of the
submodules; underscore-prefixed submodules are private. The "Code layout" section
of `docs/explanations/architecture.md` has the full layering and per-package
submodule table. Only the non-obvious ones, where the name doesn't reveal the role:

- `extract.py` vs `analyse/` — `extract.py` *fetches* external content
  (Firecrawl page→Markdown, Whisper transcription); the `analyse/` package does
  vision / OCR / PDF analysis. Easy to reach for the wrong one — and note
  `ingest/analyse.py` is a third thing: the ingest *pass* that calls the
  `analyse/` package's `Analyser`.
- `llm/` — the **single Anthropic seam** (`LLM.complete` in `llm/client.py`);
  also owns the PKM persona and the curate file-plan contract + validation.
  The classify/curate *prompts* live with their passes in `ingest/classify.py`
  and `ingest/curate.py`. Change LLM behavior via the prompt — not with output
  post-processing.
- `hindsight.py` — **HTTP client** to the standalone `hindsight-api` server (see
  the foot-gun below); the `reindex_from_vault/` package rebuilds that index
  from the vault.
- `ingest/` — the bounded **8-pass capture pipeline** (persist raw → classify →
  capture_raw → fetch_candidates → curate → retain → commit → report); raw is
  persisted *before* any LLM call so nothing is lost on restart. One submodule
  per pass group (`raw_capture`, `classify`, `curate`, `finalise`), composed by
  `pipeline.py`.
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
threshold" (near-duplicate detection, idea-mining similarity) are not buildable as
written — they need a separate embedding-access decision first. The index is a
rebuildable projection; recall is the last/most-expensive query pass.

**The two retrieval modalities are complementary, not redundant.**
`QueryEngine.answer` (behind `pkm_search`) blends three passes: **grep** over
curated folders, **wikilink** graph-follow, and **semantic recall** via Hindsight.
Recall always gets a vote when enabled — it runs concurrently in a worker thread
and the candidate sets are fused by Reciprocal Rank Fusion (`RRF_K=60`, fuse on
rank; #143 / ADR 0012, implemented in `query/_blend.py`) — there is no
"only when grep looks thin" gate any more. grep = exact / authoritative /
always-fresh on the vault bytes; Hindsight = semantic / vocabulary-bridging but
**derived, lossy, eventually-consistent** (it holds LLM-extracted *facts*, not
text — exact tokens like IDs/filenames may not survive, and a just-written or
budget-deferred page is grep-only until reindex). Don't treat grep as redundant.
