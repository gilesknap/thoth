# Architecture overview

thoth is a self-hostable personal knowledge-management appliance. You send messages
and files to a Slack bot; an LLM pipeline classifies, curates, and files them as
clean Markdown notes into an Obsidian vault backed by git. Claude Code (or
claude.ai) can then query and research over that knowledge base through an MCP
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
    slack --> sa["slack_app.py<br>handler · dedup"]
    sa --> ig{"intent.py<br>Claude Haiku<br>classify intent"}
    ig -->|capture| ing["ingest.py<br>8-pass pipeline"]
    ig -->|query| qry["query.py"]
    ig -->|ask| res["research.py"]
    ing --> ext["extract.py<br>Exa · Firecrawl · Whisper<br>fetch URLs · transcribe audio"]
    ing --> an["analyse.py<br>vision: kind · transcribe<br>Excalidraw via Opus"]
    ing --> llm["llm.py<br>Claude Sonnet API<br>classify · curate"]
    an --> llm
    ing --> va["vault.py<br>path-confined writes<br>schema validation"]
    ing --> hs["hindsight.py<br>Hindsight CLI<br>semantic index"]
    va --> gs["git_sync.py<br>vault-pull · vault-commit"]
    gs --> ov[("Obsidian vault<br>git-backed Markdown")]
    hs -.-> ov
```

**The eight passes:** `persist_inbound` (durable raw hold before any LLM call) →
`classify` → `capture_raw` → `fetch_candidates` (Exa web search for URLs) →
`curate` (Sonnet emits a schema-validated JSON file-plan) → `retain` (Hindsight
fact-extraction, prepended with a synthetic page-record so even a fact-light page lands
a recallable unit — [ADR 0011](decisions/0011-page-level-index-record.md)) → `commit`
(git pull/push) → `report` (Slack reply).

The **intent gate** (one cheap Haiku call) routes bare free-text to *capture*,
*query*, or *ask*. Explicit prefixes (`capture:`, `note:`, raw URLs, or file
uploads) skip the gate entirely and go straight to ingest.

A binary capture (image or PDF) passes through the **analyse seam**
(`analyse.py`) during `capture_raw`: one vision call returns the extracted
text, a routing hint, entities/concepts, and an image *kind* (`diagram` /
`document` / `screenshot` / `photo`). That kind drives best-effort, kind-specific
handling — a diagram becomes an editable `.excalidraw.md` saved alongside the
original (a second vision call, pinned to **Opus** by default because
reconstructing layout into valid Excalidraw JSON needs spatial reasoning), and a
document gets a faithful structured-markdown transcription in its body. The
original is always kept and a derivation failure never defers the capture
(ADR-0009). See [Models](models) for the per-call model strategy.

## MCP query/research pipeline

Claude Code and claude.ai reach the vault through seven `pkm_*` tools served
over FastMCP (stdio). The same path-confinement and schema-validation rules
apply here as in ingest — the MCP surface cannot escape the vault either.

```{mermaid}
flowchart TB
    cc(["Claude Code<br>or claude.ai"])
    cc --> mcp["mcp_server.py<br>FastMCP · 7 pkm_* tools"]
    mcp --> qry["query.py<br>vault-only retrieval<br>cost-ordered search"]
    mcp --> res["research.py<br>blended web+vault Q&A"]
    qry --> va["vault.py<br>read-only"]
    qry --> hs["hindsight.py<br>Hindsight CLI recall"]
    res --> llm["llm.py<br>Claude Sonnet API"]
    res --> ext["extract.py<br>Exa · Firecrawl<br>web search · extract"]
    va --> ov[("Obsidian vault<br>git-backed Markdown")]
    hs -.-> ov
```

`query.py` uses a cost-ordered search strategy: grep → wikilink traversal →
Hindsight semantic recall — cheapest first, LLM only as a last resort. grep
scans the whole file including frontmatter, so a reference page's one-line
`summary:` gloss is matched there; `index.md` is a static set of Bases
dashboards that retrieval never reads. `research.py` (the `pkm_ask` tool) runs Claude Sonnet with
read-only vault tools and optional live web calls; the model decides when to
reach for the web and can offer to save the composed answer back to the vault
as a `notes/` page.

(models)=
## Models

thoth is multi-model by design: each LLM call runs on the cheapest tier that can
do its job, and the three jobs that justify a stronger (or weaker) model than the
default are pinned independently. Every model id is configurable through the
environment (`deploy/.env.example` documents the keys), so the deployment can
re-tier without code changes.

| Call | Default model | Env override | Why this tier |
|---|---|---|---|
| **Intent gate** (`intent.py`) | Claude Haiku (`claude-haiku-4-5`) | `ANTHROPIC_MODEL` is unrelated; the gate model is set in code | A one-shot routing guess (capture / query / ask) — fast and cheap is the whole point |
| **Classify · curate** (`ingest.py` → `llm.py`) | Claude Sonnet (`claude-sonnet-4-6`) | `ANTHROPIC_MODEL` | The pipeline workhorse: schema-validated classification and the curate file-plan |
| **Analyse / transcribe** (`analyse.py`) | Sonnet (the default — Sonnet is multimodal) | `THOTH_ANALYSE_MODEL` | One vision call for OCR text, routing hint, kind, and document transcription; can drop to Haiku for cheaper A/B work |
| **Excalidraw reconstruction** (`analyse.py`) | **Opus** (`claude-opus-4-8`) | `THOTH_DIAGRAM_MODEL` | Rebuilding a hand-drawn diagram into valid Excalidraw JSON needs spatial reasoning — worth a stronger model than the default |
| **Blended Q&A** (`research.py`, `query.py`) | Sonnet (`ANTHROPIC_MODEL`) | `ANTHROPIC_MODEL` | Reasoning over read-only vault tools plus optional live web calls |

`ANTHROPIC_MODEL` sets the default for every call that does not pin its own model;
`THOTH_ANALYSE_MODEL` and `THOTH_DIAGRAM_MODEL` are per-call overrides that fall
back to `ANTHROPIC_MODEL` when unset. The default deployment ships
`THOTH_DIAGRAM_MODEL=claude-opus-4-8` and leaves the rest on Sonnet. Bare aliases
that 404 fall back to a proven dated id (`llm.py`); a daily call-count budget
(`budget.py`) guards every model chokepoint against redelivery storms.

## The stack

| Component | Role |
|---|---|
| **Slack Bolt** | Socket-Mode event handling — the inbound capture channel |
| **Anthropic Claude API** | Multi-model LLM backend — intent gate (Haiku), classify/curate/analyse/Q&A (Sonnet), Excalidraw reconstruction (Opus). See [Models](models) |
| **Hindsight** | Semantic search backend: fact-extraction (not token-chunking) and recall over the vault |
| **Exa** | Web search for candidate pages during ingest and for live research |
| **Firecrawl** | Web page extraction to clean Markdown during ingest and research |
| **Whisper** | Local CLI for audio/voice message transcription |
| **FastMCP** | MCP server framework — exposes the `pkm_*` tool surface to Claude Code and claude.ai |
| **git** | Vault version control and two-way sync (Obsidian Git plugin + appliance bash wrappers) |
| **Obsidian** | Markdown vault viewer and editor on the workstation |
| **python-frontmatter** | YAML frontmatter parsing for vault page metadata |
| **python-slugify** | Unicode-correct slug generation for vault file names |
| **tenacity** | Retry hardening around transient Hindsight subprocess failures |
