# Architecture overview

thoth is a self-hostable personal knowledge-management appliance. You send messages
and files to a Slack bot; an LLM pipeline classifies, curates, and files them as
clean Markdown notes into an Obsidian vault backed by git. Claude Code (or
claude.ai) can then query that knowledge base through an MCP
server — no separate web UI, no proprietary database, no lock-in.

Two principles drive every design decision:

- **The vault is canonical.** Everything else — the semantic index, the SQLite
  state, the LLM calls — is a disposable, rebuildable projection of the
  plain-Markdown git repository. Lose the index; rebuild it. Lose the process;
  restart it. The knowledge is always in the vault.
- **The tool surface is closed.** The appliance LLM has no shell and no arbitrary
  filesystem access — only a fixed set of validated tools. Prompt-injection
  from ingested web pages can, at worst, mis-classify a note; it cannot execute
  commands or exfiltrate data. Security by construction, not by guard-rails.

## Slack ingest pipeline

When you drop a message or file into Slack, thoth runs it through eight bounded,
validated passes. Critically, the raw content is persisted to the vault *before*
any LLM call — so nothing is lost if the process restarts mid-flight.

```{mermaid}
flowchart TB
    slack(["Slack<br>(Bolt Socket-Mode)"])
    slack --> sa["slack_app/<br>handlers · dedupe"]
    sa --> ig{"intent.py<br>Claude Haiku<br>classify intent"}
    ig -->|capture| ing["ingest/<br>8-pass pipeline"]
    ig -->|query| qry["query/"]
    ing --> ext["extract.py<br>Firecrawl · Whisper<br>fetch URLs · transcribe audio"]
    ing --> an["analyse/<br>vision: kind · transcribe<br>Excalidraw via Opus"]
    ing --> llm["llm/<br>Claude Sonnet API<br>classify · curate"]
    an --> llm
    ing --> va["vault/<br>path-confined writes<br>schema validation"]
    ing --> hs["hindsight.py<br>hindsight-api HTTP<br>semantic index"]
    va --> gs["git_sync.py<br>vault-pull · vault-commit"]
    gs --> ov[("Obsidian vault<br>git-backed Markdown")]
    hs -.-> ov
```

**The eight passes:** `persist_inbound` (durable raw hold before any LLM call) →
`classify` → `capture_raw` → `fetch_candidates` (fetch URLs found in the message)
→ `curate` (Sonnet emits a schema-validated JSON file-plan) → `retain` (Hindsight
fact-extraction) → `commit` (git pull/push) → `report` (Slack reply).

The **intent gate** (one cheap Haiku call) routes bare free-text to *capture* or
*query*, with query as the safe fallback. Explicit prefixes (`capture:`, `note:`,
raw URLs, or file uploads) skip the gate entirely and go straight to ingest.

A binary capture (image or PDF) passes through the **analyse seam**
(`thoth.analyse`) during `capture_raw`: one vision call returns the extracted
text, a routing hint, entities/concepts, and an image *kind* (`diagram` /
`document` / `screenshot` / `photo`). That kind drives best-effort, kind-specific
handling — a diagram becomes an editable `.excalidraw.md` saved alongside the
original (a second vision call, pinned to **Opus** by default because
reconstructing layout into valid Excalidraw JSON needs spatial reasoning), and a
document gets a faithful structured-markdown transcription in its body. The
original is always kept and a derivation failure never defers the capture
(ADR-0009). See [Models](models) for the per-call model strategy.

### What you can capture

A single Slack message (or `thoth capture <path>`) accepts any of:

| Input | Handling |
|---|---|
| **Text** | Filed as a note; the intent gate routes bare free-text to capture / query. |
| **URL** | Fetched server-side and extracted to clean Markdown (Firecrawl), SSRF-guarded. |
| **Image** (PNG/JPG/…) | One vision call: OCR text, routing hint, entities, and an image *kind*. Images over 2 MB are downscaled first ([`THOTH_IMAGE_RESIZE_THRESHOLD_BYTES`](../reference/configuration.md)). |
| **PDF** | Vision analysis → text + a structured-Markdown transcription in the page body. |
| **Audio / voice** | Transcribed locally via the Whisper CLI, then filed as text (the title comes from the speech). |
| **Hand-drawn diagram** | Reconstructed into an editable `.excalidraw.md` scene alongside the original (ADR-0009). |
| **Multi-image batch** | All images in one message → one curated page, shared summary/tags, every image embedded (capped per call by [`THOTH_MAX_ANALYSE_IMAGES`](../reference/configuration.md)). |

## MCP query pipeline

Claude Code and claude.ai reach the vault through seven `pkm_*` tools served
over a bearer-authenticated FastMCP HTTP socket (`thoth-mcp.service`, loopback
`127.0.0.1:8765`, fronted by a cloudflared tunnel — see
{doc}`../how-to/mcp-server-setup` and ADR
{doc}`decisions/0011-mcp-http-transport-and-tiered-auth`). The same
path-confinement and schema-validation rules apply here as in ingest — the MCP
surface cannot escape the vault either.

```{mermaid}
flowchart TB
    cc(["Claude Code<br>or claude.ai"])
    cc --> mcp["mcp_server/<br>FastMCP · 7 pkm_* tools"]
    mcp --> qry["query/<br>vault-only retrieval<br>grep ∪ recall · RRF blend"]
    qry --> va["vault/<br>read-only"]
    qry --> hs["hindsight.py<br>hindsight-api recall"]
    va --> ov[("Obsidian vault<br>git-backed Markdown")]
    hs -.-> ov
```

`thoth.query` blends **two retrieval sources** and fuses them with **Reciprocal
Rank Fusion** (RRF, `K=60`): a *structural* pass (grep + wikilink traversal) and
a *semantic* pass (Hindsight recall). The semantic pass **always gets a vote**
when enabled — there is no "only when results look thin" gate — and runs
**concurrently** in a worker thread so its latency overlaps grep rather than
serialising after it. Each unique page scores `Σ 1/(60+rank)` across the sources
that surfaced it; the top `max_pages` are cited, each tagged with its
*provenance* (which method — grep / wikilink / recall — found it). A Hindsight
failure degrades gracefully to structural-only. grep scans the whole file
including frontmatter, so a page's one-line `summary:` gloss is matched there,
and a caller can pass `search_keywords` to seed the whole-word grep with
de-pluralised/synonym terms; `index.md` is a static set of Bases dashboards that
retrieval never reads. See ADR
{doc}`decisions/0012-blend-grep-and-semantic-retrieval-rrf`.

## Code layout

`src/thoth/` is layered, and imports point strictly downward — a lower layer never
imports a higher one:

1. **Shared leaf modules** — `_time.py` (the persona timezone and the injectable
   UTC clock), `filetypes.py` (the capture-kind extension sets), `fmfields.py`
   (tolerant frontmatter scalar coercions), and `render.py` (the one Slack `mrkdwn`
   formatter for a vault-page reference, plus the shared `SlackPoster` protocol).
   These are stdlib-only and import nothing from the rest of `thoth`, so any module
   can use them with no risk of an import cycle.
2. **Domain modules** — single-file collaborators with one responsibility each:
   `extract.py` (SSRF-guarded URL fetch and Whisper transcription), `git_sync.py`
   (the deterministic git wrapper — stdlib-only by contract, so the vault sync
   can never grow a third-party dependency), `hindsight.py` (the HTTP client to
   `hindsight-api`), `intent.py` (the Haiku intent gate), `images.py` (downscaling),
   `alerts.py` (errors-to-Slack), `templates.py` (the packaged vault spine),
   `capture_walk.py` and `inbox_drain.py` (bulk import and the held-capture sweep),
   and `mcp_auth.py` / `mcp_oauth.py` (bearer and OAuth 2.1 auth for the MCP HTTP
   transport). These stay single files deliberately: splitting them would bury a
   seam that tests patch directly (`extract.py`'s SSRF helpers, for example) or
   add package ceremony a small module doesn't need.
3. **Boundary packages** — the larger subsystems, each a package of focused
   submodules behind one public `__init__` (the table below).
4. **Entry points** — `__main__.py` (the `thoth` CLI dispatch, with `cli_parser.py`
   and `cli_capture.py` split out) and `wiring.py`, whose `build_collaborators` is
   the single place the ingest/query collaborator graph is constructed — both the
   CLI/daemon and the MCP server call it, so the two wirings cannot drift.

### What each package's submodules own

| Package | Submodules |
|---|---|
| `config/` | `model.py` — the frozen `Config` dataclass and `ConfigError`; the package `__init__` owns env loading and validation (`load_config`). |
| `state/` | `_db.py` — shared SQLite plumbing (WAL, short-lived connections); `events.py` — Slack redelivery dedupe; `markers.py` — liveness/heartbeat markers. |
| `budget/` | `store.py` — persistent per-day call counters; `guard.py` — the fail-safe circuit-breaker and its notification seams. |
| `vault/` | `contract.py` — the canonical page-type/source/folder vocabulary and slug grammar; `redact.py` — secret redaction before filing; `core.py` — page records, errors, and the path-confined `Vault` facade. |
| `llm/` | `client.py` — the injectable Anthropic wrapper and prompt-caching kwargs; `persona.py` — the PKM persona system prompt; `contract.py` — the curate file-plan contract; `validation.py` — its validator (reusing `vault`'s disk-write validators); `responses.py` — response-shape helpers. |
| `analyse/` | `analyser.py` — the injectable vision `Analyser`; `prompts.py` — the analyse and Excalidraw prompts; `result.py` — the structured `Analysis` parse; `excalidraw.py` / `excalidraw_elements.py` — deterministic `.excalidraw.md` scene assembly. |
| `ingest/` | One submodule per pass group: `raw_capture.py` (durable hold + raw capture), `analyse.py` (the binary-analysis pass), `classify.py`, `curate.py` (candidate fetch + file-plan), `finalise.py` (retain · commit · report), plus `assets.py` (the idempotent `raw/assets` store), `_shared.py` (pass types and vocabulary) and `pipeline.py` (the composed `Ingestor`). |
| `query/` | `_retrieval.py` — the grep/wikilink/recall passes as pure functions; `_blend.py` — RRF fusion with the recall thread overlapped; `_compose.py` — citation minting and prose composition; `_engine.py` — the `QueryEngine` facade; `_shared.py` — types and constants. |
| `slack_app/` | `daemon.py` — Bolt build/serve; `handlers.py` — allow-list and routing; `events.py` — pure readers over raw Slack events; `files.py` — upload staging; `dedupe.py` — redelivery dedupe; `rendering.py` — `mrkdwn` renderers; `responder.py` — the placeholder-then-edit reply seam. |
| `mcp_server/` | `server.py` — FastMCP construction and the `thoth mcp` entry; `http.py` — the auth-gated HTTP transport; `context.py` — the `ToolContext` injection bundle; `tools_query.py` / `tools_pages.py` / `tools_ingest.py` — the tool bodies as plain testable functions; `render.py` — MCP Markdown rendering. |
| `summary/` | `types.py` — the frozen digest item types; `engine.py` — frontmatter scans and digest composition; `render.py` — sorting and `mrkdwn` rendering. |
| `lint/` | `model.py` — severities, findings, the report; `parse.py` — pure markdown extractors; `checks_links.py` / `checks_metadata.py` / `checks_freshness.py` — the checks by theme; `engine.py` — the vault walk and `LintEngine`. |
| `reindex_from_vault/` | `_model.py` — reindex vocabulary and pure helpers; `reindexer.py` — the `Reindexer` walk/retain/prune engine. |

### Package conventions

- **The package `__init__` is the public surface.** Each package re-exports its
  public names (listed in `__all__`) from its `__init__`, whose docstring is the
  authoritative map of the package. Callers import from the package
  (`from thoth.vault import Vault`), never from a submodule; underscore-prefixed
  submodules (`query/_engine.py`, `state/_db.py`, …) make the privacy explicit.
- **Heavy SDKs import lazily.** `anthropic`, `slack_bolt` and `mcp` are imported
  only inside the functions that need them (`llm`'s client factory, `slack_app`'s
  daemon, `mcp_server`'s server/transport) — never at module top level. Importing
  any `thoth` package therefore needs only the base dependencies: pytest
  collection and CI run without the runtime extra installed. `wiring.py` follows
  the same rule for the whole collaborator graph, which also keeps test patches
  on a collaborator's defining module effective.
- **One logger per package.** A package that logs defines a single
  `logging.getLogger("thoth.<package>")` in its shared submodule and the other
  submodules import it, so log filtering follows responsibilities, not file
  boundaries.

(models)=
## Models

thoth is multi-model by design: each LLM call runs on the cheapest tier that can
do its job, and the three jobs that justify a stronger (or weaker) model than the
default are pinned independently. Every model id is configurable through the
environment (`deploy/.env.example` documents the keys), so the deployment can
re-tier without code changes.

| Call | Default model | Env override | Why this tier |
|---|---|---|---|
| **Intent gate** (`intent.py`) | Claude Haiku (`claude-haiku-4-5`) | `THOTH_INTENT_MODEL` (unset = the default Haiku, not `ANTHROPIC_MODEL`) | A one-shot routing guess (capture / query) — fast and cheap is the whole point |
| **Classify · curate** (`thoth.ingest` → `thoth.llm`) | Claude Sonnet (`claude-sonnet-4-6`) | `ANTHROPIC_MODEL` | The pipeline workhorse: schema-validated classification and the curate file-plan |
| **Analyse / transcribe** (`thoth.analyse`) | Sonnet (the default — Sonnet is multimodal) | `THOTH_ANALYSE_MODEL` | One vision call for OCR text, routing hint, kind, and document transcription; can drop to Haiku for cheaper A/B work |
| **Excalidraw reconstruction** (`thoth.analyse`) | **Opus** (`claude-opus-4-8`) | `THOTH_DIAGRAM_MODEL` | Rebuilding a hand-drawn diagram into valid Excalidraw JSON needs spatial reasoning — worth a stronger model than the default |

`ANTHROPIC_MODEL` sets the default for every call that does not pin its own model;
`THOTH_ANALYSE_MODEL` and `THOTH_DIAGRAM_MODEL` are per-call overrides that fall
back to `ANTHROPIC_MODEL` when unset; `THOTH_INTENT_MODEL` overrides the intent gate
and, when unset, falls back to its own cheap Haiku default rather than `ANTHROPIC_MODEL`.
The default deployment ships
`THOTH_DIAGRAM_MODEL=claude-opus-4-8` and leaves the rest on Sonnet. The configured
model ids are used as-is — a wrong id surfaces as an API error rather than a silent
substitution; a daily call-count budget (`thoth.budget`) guards every model
chokepoint against redelivery storms.

## The stack

| Component | Role |
|---|---|
| **Slack Bolt** | Socket-Mode event handling — the inbound capture channel |
| **Anthropic Claude API** | Multi-model LLM backend — intent gate (Haiku), classify/curate/analyse (Sonnet), Excalidraw reconstruction (Opus). See [Models](models) |
| **Hindsight** | Semantic search backend: fact-extraction (not token-chunking) and recall over the vault. The `hindsight.py` seam is an **HTTP client** (`httpx`) to a standalone `hindsight-api` server ([`THOTH_HINDSIGHT_BASE_URL`](../reference/configuration.md), default `http://127.0.0.1:8888`); the bank is a URL path segment and a page's vault-relative path round-trips as the memory `document_id`. A standalone server (vs an embedded library) is the foundation for moving the index to its own scaled deployment later. On the appliance the server is loopback; on Kubernetes (following #157) it is a networked Service reached over `THOTH_HINDSIGHT_BASE_URL` (`http://<release>-hindsight:8888`), with its index data on its own disposable PVC — rebuildable from the vault. See {doc}`../how-to/deploy-kubernetes`. |
| **Firecrawl** | Web page extraction to clean Markdown during ingest |
| **Whisper** | Local CLI for audio/voice message transcription |
| **FastMCP** | MCP server framework — exposes the `pkm_*` tool surface to Claude Code and claude.ai |
| **git** | Vault version control and two-way sync (Obsidian Git plugin + appliance bash wrappers) |
| **Obsidian** | Markdown vault viewer and editor on the workstation |
| **python-frontmatter** | YAML frontmatter parsing for vault page metadata |
| **python-slugify** | Unicode-correct slug generation for vault file names |
| **tenacity** | Retry hardening around transient Hindsight HTTP failures (5xx + transport errors) |
