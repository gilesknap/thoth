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
    ing --> llm["llm.py<br>Claude Sonnet API<br>classify · curate · vision"]
    ing --> va["vault.py<br>path-confined writes<br>schema validation"]
    ing --> hs["hindsight.py<br>Hindsight CLI<br>semantic index"]
    va --> gs["git_sync.py<br>vault-pull · vault-commit"]
    gs --> ov[("Obsidian vault<br>git-backed Markdown")]
    hs -.-> ov
```

**The eight passes:** `persist_inbound` (durable raw hold before any LLM call) →
`classify` → `capture_raw` → `fetch_candidates` (Exa web search for URLs) →
`curate` (Sonnet emits a schema-validated JSON file-plan) → `retain` (Hindsight
fact-extraction) → `commit` (git pull/push) → `report` (Slack reply).

The **intent gate** (one cheap Haiku call) routes bare free-text to *capture*,
*query*, or *ask*. Explicit prefixes (`capture:`, `note:`, raw URLs, or file
uploads) skip the gate entirely and go straight to ingest.

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

`query.py` uses a cost-ordered search strategy: `index.md` summaries → grep →
wikilink traversal → Hindsight semantic recall — cheapest first, LLM only as
a last resort. `research.py` (the `pkm_ask` tool) runs Claude Sonnet with
read-only vault tools and optional live web calls; the model decides when to
reach for the web and can offer to save the composed answer back to the vault
as a `notes/` page.

## The stack

| Component | Role |
|---|---|
| **Slack Bolt** | Socket-Mode event handling — the inbound capture channel |
| **Anthropic Claude API** | Intent gate (Haiku), classification, curation, vision analysis, and blended Q&A (Sonnet) |
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
