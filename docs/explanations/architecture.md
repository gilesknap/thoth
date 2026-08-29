# Architecture overview

thoth is a self-hostable personal knowledge-management appliance. You send messages and files to a Slack bot, and an LLM pipeline classifies, curates and files them as clean Markdown notes into an Obsidian vault backed by git.

Claude Code, or claude.ai, then queries that knowledge base through an MCP server. There is no separate web UI, no proprietary database and no lock-in.

Two principles drive every design decision:

- **The vault is canonical.** The semantic index, the SQLite state and the LLM calls are all disposable, rebuildable projections of the plain-Markdown git repository. Lose the index and you rebuild it, lose the process and you restart it. The knowledge is always in the vault.
- **The tool surface is closed.** The appliance LLM has no shell and no arbitrary filesystem access, only a fixed set of validated tools. Prompt-injection from an ingested web page can at worst mis-classify a note, and cannot execute commands or exfiltrate data. This is security by construction rather than by guard-rails.

## Slack ingest pipeline

When you drop a message or file into Slack, thoth runs it through eight bounded, validated passes.

The raw content is persisted to the vault *before* any LLM call, so nothing is lost if the process restarts mid-flight.

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

The eight passes are `persist_inbound` (the durable raw hold before any LLM call), `classify`, `capture_raw`, `fetch_candidates` (fetch the URLs found in the message), `curate` (Sonnet emits a schema-validated JSON file-plan), `retain` (Hindsight fact-extraction), `commit` (git pull and push) and `report` (the Slack reply).

The intent gate is one cheap Haiku call. It routes bare free-text to *capture* or *query*, with query as the safe fallback.

Explicit prefixes (`capture:`, `note:`, a raw URL, or a file upload) skip the gate entirely and go straight to ingest.

A binary capture, meaning an image or a PDF, passes through the analyse seam (`thoth.analyse`) during `capture_raw`. One vision call returns the extracted text, a routing hint, entities and concepts, and an image *kind* of `diagram`, `document`, `screenshot` or `photo`.

That kind drives best-effort, kind-specific handling. A diagram becomes an editable `.excalidraw.md` saved alongside the original, from a second vision call pinned to Opus by default because reconstructing layout into valid Excalidraw JSON needs spatial reasoning. A document gets a faithful structured-Markdown transcription in its body.

The original is always kept, and a derivation failure never defers the capture (ADR-0009). See [Models](models) for the per-call model strategy.

### What you can capture

A single Slack message, or `thoth capture <path>`, accepts any of:

| Input | Handling |
|---|---|
| **Text** | Filed as a note; the intent gate routes bare free-text to capture or query. |
| **URL** | Fetched server-side and extracted to clean Markdown (Firecrawl), SSRF-guarded. |
| **Image** (PNG/JPG/…) | One vision call: OCR text, routing hint, entities, and an image *kind*. Images over 2 MB are downscaled first ([`THOTH_IMAGE_RESIZE_THRESHOLD_BYTES`](../reference/configuration.md)). |
| **PDF** | Vision analysis giving text plus a structured-Markdown transcription in the page body. |
| **Audio / voice** | Transcribed locally via the Whisper CLI, then filed as text (the title comes from the speech). |
| **Hand-drawn diagram** | Reconstructed into an editable `.excalidraw.md` scene alongside the original (ADR-0009). |
| **Multi-image batch** | All images in one message become one curated page with a shared summary and tags, every image embedded (capped per call by [`THOTH_MAX_ANALYSE_IMAGES`](../reference/configuration.md)). |

## MCP query pipeline

Claude Code and claude.ai reach the vault through seven `pkm_*` tools served over a bearer-authenticated FastMCP HTTP socket. That is `thoth-mcp.service` on loopback `127.0.0.1:8765`, fronted by a cloudflared tunnel, as described in {doc}`../how-to/mcp-server-setup` and ADR {doc}`decisions/0011-mcp-http-transport-and-tiered-auth`.

The same path-confinement and schema-validation rules apply here as in ingest, so the MCP surface cannot escape the vault either.

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

`thoth.query` blends two retrieval sources and fuses them with Reciprocal Rank Fusion (RRF, `K=60`): a *structural* pass of grep plus wikilink traversal, and a *semantic* pass of Hindsight recall.

The semantic pass always gets a vote when it is enabled. There is no "only when results look thin" gate, and it runs concurrently in a worker thread so its latency overlaps grep rather than serialising after it.

Each unique page scores `Σ 1/(60+rank)` across the sources that surfaced it. The top `max_pages` are cited, each tagged with the *provenance* (grep, wikilink or recall) that found it.

A Hindsight failure degrades gracefully to structural-only.

grep scans the whole file including frontmatter, so a page's one-line `summary:` gloss is matched there, and a caller can pass `search_keywords` to seed the whole-word grep with de-pluralised or synonym terms.

`_bases/index.md` is a static set of Bases dashboards that retrieval never reads, and the root `index.md` is a thin OKF stub linking to it. See ADR {doc}`decisions/0012-blend-grep-and-semantic-retrieval-rrf`.

## Code layout

`src/thoth/` is layered, and imports point strictly downward. A lower layer never imports a higher one.

1. **Shared leaf modules.** `_time.py` (the persona timezone and the injectable UTC clock), `filetypes.py` (the capture-kind extension sets), `fmfields.py` (tolerant frontmatter scalar coercions) and `render.py` (the one Slack `mrkdwn` formatter for a vault-page reference, plus the shared `SlackPoster` protocol). These are stdlib-only and import nothing from the rest of `thoth`, so any module can use them with no risk of an import cycle.
2. **Domain modules**, single-file collaborators with one responsibility each. `extract.py` (SSRF-guarded URL fetch and Whisper transcription), `git_sync.py` (the deterministic git wrapper, stdlib-only by contract so the vault sync can never grow a third-party dependency), `hindsight.py` (the HTTP client to `hindsight-api`), `intent.py` (the Haiku intent gate), `images.py` (downscaling), `alerts.py` (errors to Slack), `templates.py` (the packaged vault spine), `capture_walk.py` and `inbox_drain.py` (bulk import and the held-capture sweep), and `mcp_auth.py` with `mcp_oauth.py` (bearer and OAuth 2.1 auth for the MCP HTTP transport). These stay single files deliberately, because splitting them would bury a seam that tests patch directly, such as `extract.py`'s SSRF helpers, or add package ceremony a small module does not need.
3. **Boundary packages**, the larger subsystems, each a package of focused submodules behind one public `__init__` (the table below).
4. **Entry points.** `__main__.py` is the `thoth` CLI dispatch, with `cli_parser.py` and `cli_capture.py` split out. `wiring.py` holds `build_collaborators`, the single place the ingest and query collaborator graph is constructed, called by both the CLI/daemon and the MCP server so the two wirings cannot drift.

### What each package's submodules own

| Package | Submodules |
|---|---|
| `config/` | `model.py`: the frozen `Config` dataclass and `ConfigError`. The package `__init__` owns env loading and validation (`load_config`). |
| `state/` | `_db.py`: shared SQLite plumbing (WAL, short-lived connections). `events.py`: Slack redelivery dedupe. `markers.py`: liveness and heartbeat markers. |
| `budget/` | `store.py`: persistent per-day call counters. `guard.py`: the fail-safe circuit-breaker and its notification seams. |
| `vault/` | `contract.py`: the canonical page-type, source and folder vocabulary and the slug grammar. `redact.py`: secret redaction before filing. `core.py`: page records, errors, and the path-confined `Vault` facade. |
| `llm/` | `client.py`: the injectable Anthropic wrapper and prompt-caching kwargs. `persona.py`: the PKM persona system prompt. `contract.py`: the curate file-plan contract. `validation.py`: its validator, reusing `vault`'s disk-write validators. `responses.py`: response-shape helpers. |
| `analyse/` | `analyser.py`: the injectable vision `Analyser`. `prompts.py`: the analyse and Excalidraw prompts. `result.py`: the structured `Analysis` parse. `excalidraw.py` and `excalidraw_elements.py`: deterministic `.excalidraw.md` scene assembly. |
| `ingest/` | One submodule per pass group: `raw_capture.py` (durable hold and raw capture), `analyse.py` (the binary-analysis pass), `classify.py`, `curate.py` (candidate fetch and file-plan), `finalise.py` (retain, commit, report). Plus `assets.py` (the idempotent `raw/assets` store), `_shared.py` (pass types and vocabulary) and `pipeline.py` (the composed `Ingestor`). |
| `query/` | `_retrieval.py`: the grep, wikilink and recall passes as pure functions. `_blend.py`: RRF fusion with the recall thread overlapped. `_compose.py`: citation minting and prose composition. `_engine.py`: the `QueryEngine` facade. `_shared.py`: types and constants. |
| `slack_app/` | `daemon.py`: Bolt build and serve. `handlers.py`: allow-list and routing. `events.py`: pure readers over raw Slack events. `files.py`: upload staging. `dedupe.py`: redelivery dedupe. `rendering.py`: `mrkdwn` renderers. `responder.py`: the placeholder-then-edit reply seam. |
| `mcp_server/` | `server.py`: FastMCP construction and the `thoth mcp` entry. `http.py`: the auth-gated HTTP transport. `context.py`: the `ToolContext` injection bundle. `tools_query.py`, `tools_pages.py` and `tools_ingest.py`: the tool bodies as plain testable functions. `render.py`: MCP Markdown rendering. |
| `summary/` | `types.py`: the frozen digest item types. `engine.py`: frontmatter scans and digest composition. `render.py`: sorting and `mrkdwn` rendering. |
| `lint/` | `model.py`: severities, findings, the report. `parse.py`: pure markdown extractors. `checks_links.py`, `checks_metadata.py` and `checks_freshness.py`: the checks by theme. `engine.py`: the vault walk and `LintEngine`. |
| `reindex_from_vault/` | `_model.py`: reindex vocabulary and pure helpers. `reindexer.py`: the `Reindexer` walk, retain and prune engine. |

### Package conventions

- **The package `__init__` is the public surface.** Each package re-exports its public names, listed in `__all__`, from its `__init__`, whose docstring is the authoritative map of the package. Callers import from the package (`from thoth.vault import Vault`) and never from a submodule, and underscore-prefixed submodules such as `query/_engine.py` and `state/_db.py` make the privacy explicit.
- **Heavy SDKs import lazily.** `anthropic`, `slack_bolt` and `mcp` are imported only inside the functions that need them, in `llm`'s client factory, `slack_app`'s daemon and `mcp_server`'s server and transport, never at module top level. Importing any `thoth` package therefore needs only the base dependencies, so pytest collection and CI run without the runtime extra installed. `wiring.py` follows the same rule for the whole collaborator graph, which also keeps test patches on a collaborator's defining module effective.
- **One logger per package.** A package that logs defines a single `logging.getLogger("thoth.<package>")` in its shared submodule and the other submodules import it, so log filtering follows responsibilities rather than file boundaries.

(models)=
## Models

thoth is multi-model by design. Each LLM call runs on the cheapest tier that can do its job, and the three jobs that justify a stronger or weaker model than the default are pinned independently.

Every model id is configurable through the environment, and `deploy/.env.example` documents the keys, so a deployment re-tiers without code changes.

| Call | Default model | Env override | Why this tier |
|---|---|---|---|
| **Intent gate** (`intent.py`) | Claude Haiku (`claude-haiku-4-5`) | `THOTH_INTENT_MODEL` (unset means the default Haiku, not `ANTHROPIC_MODEL`) | A one-shot routing guess between capture and query, where fast and cheap is the whole point |
| **Classify and curate** (`thoth.ingest` into `thoth.llm`) | Claude Sonnet (`claude-sonnet-4-6`) | `ANTHROPIC_MODEL` | The pipeline workhorse: schema-validated classification and the curate file-plan |
| **Analyse and transcribe** (`thoth.analyse`) | Sonnet, the default, since Sonnet is multimodal | `THOTH_ANALYSE_MODEL` | One vision call for OCR text, routing hint, kind and document transcription. It can drop to Haiku for cheaper A/B work |
| **Excalidraw reconstruction** (`thoth.analyse`) | **Opus** (`claude-opus-4-8`) | `THOTH_DIAGRAM_MODEL` | Rebuilding a hand-drawn diagram into valid Excalidraw JSON needs spatial reasoning, which is worth a stronger model than the default |

`ANTHROPIC_MODEL` sets the default for every call that does not pin its own model. `THOTH_ANALYSE_MODEL` and `THOTH_DIAGRAM_MODEL` are per-call overrides that fall back to `ANTHROPIC_MODEL` when unset, while `THOTH_INTENT_MODEL` falls back to its own cheap Haiku default instead.

The default deployment ships `THOTH_DIAGRAM_MODEL=claude-opus-4-8` and leaves the rest on Sonnet.

```{warning}
The configured model ids are used as-is. A wrong id surfaces as an API error rather than a silent substitution.
```

A daily call-count budget (`thoth.budget`) guards every model chokepoint against redelivery storms.

## The stack

| Component | Role |
|---|---|
| **Slack Bolt** | Socket-Mode event handling, the inbound capture channel |
| **Anthropic Claude API** | The multi-model LLM backend: intent gate (Haiku), classify, curate and analyse (Sonnet), Excalidraw reconstruction (Opus). See [Models](models) |
| **Hindsight** | Semantic search backend: fact-extraction rather than token-chunking, and recall over the vault. The `hindsight.py` seam is an **HTTP client** (`httpx`) to a standalone `hindsight-api` server ([`THOTH_HINDSIGHT_BASE_URL`](../reference/configuration.md), default `http://127.0.0.1:8888`); the bank is a URL path segment and a page's vault-relative path round-trips as the memory `document_id`. A standalone server rather than an embedded library is the foundation for moving the index to its own scaled deployment later. On the appliance the server is loopback; on Kubernetes (following #157) it is a networked Service reached over `THOTH_HINDSIGHT_BASE_URL` (`http://<release>-hindsight:8888`), with its index data on its own disposable PVC, rebuildable from the vault. See {doc}`../how-to/deploy-kubernetes`. |
| **Firecrawl** | Web page extraction to clean Markdown during ingest |
| **Whisper** | Local CLI for audio and voice message transcription |
| **FastMCP** | MCP server framework, exposing the `pkm_*` tool surface to Claude Code and claude.ai |
| **git** | Vault version control and two-way sync (the Obsidian Git plugin plus appliance bash wrappers) |
| **Obsidian** | Markdown vault viewer and editor on the workstation |
| **python-frontmatter** | YAML frontmatter parsing for vault page metadata |
| **tenacity** | Retry hardening around transient Hindsight HTTP failures (5xx and transport errors) |
