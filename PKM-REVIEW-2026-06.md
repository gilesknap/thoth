# thoth — agentic PKM review (2026-06-13)

> **Scope, method & handling — read first.**
> This is a comprehensive, candid review of thoth and its companion `../pkm-vault`,
> commissioned to sanity-check the design against agentic-PKM / RAG / memory best
> practice. It was produced by a multi-agent pass (8 subsystem deep-dives over the real
> code + vault, 4 web-research sweeps on best practice, an adversarial verification
> stage that re-checked 39 factual claims against the code and **corrected 7 of them**,
> then synthesis). The highest-stakes claims below — the leaked secrets, the phantom
> Exa/“reply y” features, the retrieval embed divergence, the empty review queue — were
> then independently re-verified by hand against the source. Where a claim was refuted it
> has been dropped or corrected in place, not repeated.
>
> **Placement:** this file lives at the repo root, **not under `docs/`**, on purpose —
> `docs/` builds to the public Sphinx site, and this is a frank internal critique. It is
> committed to a side branch (`docs/pkm-review-2026-06`), not `main`. Earlier drafts named
> leaked credentials by location; those specifics have been removed (see the closing note),
> so the doc carries no secret values or locations.
>
> **One measurement caveat:** some whole-tree grep counts below were taken while the vault
> still had a gitignored `.claude/worktrees/vault-migrate/` mirror (since removed), so they
> can double-count. The *findings* hold; re-confirm exact counts with `git ls-files` scoping
> before acting on a specific number.

---

## Executive summary

thoth is a genuinely impressive solo build: a closed-tool-surface “agentic PKM” that
captures anything dropped into one Slack channel, runs a bounded multi-model LLM pipeline
(Haiku intent gate → Sonnet classify/curate → Sonnet/Opus vision) to file clean
cross-linked Markdown into a git-backed Obsidian vault, indexes it into a rebuildable
Hindsight semantic index, and answers questions *from the vault* with unfabricable
citations — the same vault exposed to Claude Code/claude.ai over a hardened, fixed MCP
surface. The parts you engineered carefully are at or above current best practice: the
persist-before-LLM ingest pipeline, the textbook RRF retrieval blend, the single-sourced
schema contract, the layered/constant-time auth, and the closed tool surface that bounds
prompt injection. The gaps that matter are not in those parts — they cluster into five
cross-cutting themes where the *current behaviour diverges from best practice in the same
way across several subsystems*, plus one privacy hole and a cluster of doc-vs-reality
drift.

**Verdict.**

- **What's great (preserve):** the closed validated tool surface; the single-source-of-truth
  schema contract; the durable, precisely-deferred ingest pipeline; hybrid RRF retrieval
  with unfabricable citations and graceful degradation; the vault-canonical /
  index-disposable recovery model; CI-enforced leak-scan with the public-repo / private-vault
  split.
- **Fix first (this week):** (1) rotate and scrub the live plaintext secrets sitting in the
  vault — the redactor empirically misses them and lint has no secret rule; (2) unify the
  Hindsight retain text and embed the page `summary` (highest retrieval impact-per-effort);
  (3) a batch of cheap honesty fixes — reconcile ADR 0011 with the shipped OAuth server,
  soften the README's web-search claims, delete the phantom “reply y to save” doc flow,
  regenerate the drifted persona prompt.
- **Then build (the real headroom):** the *compounding-value* half of a second brain that is
  almost entirely absent — resurfacing / on-this-day, a propose-only consolidation pass,
  read-before-write on updates and links, and a tiny golden-set retrieval eval so every
  future tweak is measurable.

Two verification corrections are worth stating up front because they change worklists. The
“entity-kind vs kind tag redundancy” finding was **refuted** (a `grep -o` substring artifact —
those two namespaces are disjoint) and is dropped entirely. And the conflict/quality
frontmatter fields (`contested`/`contradictions`/`confidence`/`sources`) are dead in **zero**
pages, not “about two” — fully dead schema, not nearly-dead.

---

## Architecture & principles

thoth rests on three load-bearing principles and realises all three faithfully in code.

**1. The vault is canonical; everything else is a disposable projection.** This is the
strongest single design decision and it shows up consistently: the recovery model treats
vault git as the backup, the Hindsight index as explicitly rebuildable, and `state.db` as
intentionally unbacked (`docs/how-to/recovery.md`). Provenance survives Hindsight's lossy
fact-extraction through three redundant channels where `document_id` = vault path = forget
key (`hindsight.py:165-182,342-422`), so round-trip and per-document delete stay coherent.
And `SPEC.md` is honestly de-rated to “Historical / not maintained,” pointing at the
ADRs/code/SCHEMA/tests as the living truth (`SPEC.md:1-10`) — which neutralises the single
biggest landmine for an AI assistant reading the repo. This is the bet that most cleanly
distinguishes thoth from the AI-native-notebook competitors (NotebookLM / Mem): it does the
same hard capture→curate→cite loop, but over a user-owned git-backed store rather than a
black box.

**2. The tool surface is closed and validated.** The appliance LLM never gets a shell or
arbitrary FS — only 7 fixed `pkm_*` tools (`mcp_server/server.py`), and the curate LLM's
*only* output is a schema-validated JSON file-plan via a forced `submit_file_plan` tool,
never an open agentic loop (`ingest/curate.py:39-118`). Write confinement genuinely bounds
prompt injection: `vault.resolve()` rejects `..`/`.`/absolute/symlink-escape and `write_page`
enforces slug grammar plus the folder-by-type contract (`vault/core.py:140-187,405-456`). The
curate output cannot write outside the vault or invent a tool.

**3. The general agent is external.** Claude Code / claude.ai drive the vault over MCP;
thoth's own appliance LLM is deliberately small and tool-bound. This is also why the 2025
“code-execution-with-MCP / progressive disclosure” pattern is correctly a non-goal here — it
pays off only at dozens-to-hundreds of tools and would punch a hole in the closed surface for
a 7-tool server.

**Layering and wiring.** Imports point strictly downward (leaf → domain → boundary-package →
entry-point) with no cycles, heavy SDKs import lazily so the whole package collects under
pytest with base deps only, and a single `wiring.build_collaborators` seam stops the daemon
and MCP graphs from diverging — its docstring even records the regression it prevents (MCP
wiring once dropped `schema_md`, blinding curate). The two structural wrinkles to watch are
maintainability rather than design: very high docstring density (several modules ~30-45%
prose — a second source of truth that can rot) and a couple of god-sized files
(`tests/test_ingest.py` is the largest; `vault/core.py` at 672 lines). A CI import-linter
contract asserting the downward-only graph is the one cheap reinforcement worth adding — it
mechanically protects the property an AI assistant is most likely to erode.

---

## The ingest pipeline

This is the strongest-engineered subsystem. It is a bounded 8-pass design — orient, durable
hold, analyse, classify, raw-capture, curate, navigation/retain, commit — where deterministic
Python owns idempotency, path confinement, git, asset storage and link generation, and the
LLM owns only classification, body authoring, and routing hints.

**What's genuinely careful here:**

- **Persist-before-LLM.** The inbound item lands in `inbox/` keyed on body-SHA before any
  model call, so an Anthropic outage can never lose a capture (`ingest/pipeline.py:118`).
- **Parse-stable idempotency.** Raw/holding writes compare against the stored
  redacted-body SHA via the *same* `Vault.stored_body_sha256` the writer stamps, so a trailing
  newline never spuriously reports drift; assets key on bytes-SHA
  (`ingest/raw_capture.py:462-484`).
- **A precise deferral taxonomy.** A permanent vision 400/413/422 files the binary *blind* (so
  an identical rejected payload isn't re-sent and re-burning budget each sweep), transient
  outages defer, validation failure aborts, and the fetched temp file is cleaned on every
  defer path (`ingest/analyse.py:104-136,302-334`). This precision is unusual and worth
  preserving.
- **Content-based text dedup exists.** The verification pass *corrected* an external claim
  here: text bodies are SHA-256-compared and an unchanged-curated short-circuit collapses
  re-sends (`pipeline.py:158-165`). A duplicate only arises when the non-deterministic
  classifier picks a *different* slug for identical text — so resends don't reliably
  duplicate.

**The soft spots — and they share one root cause: write-without-read.**

- **Blind-overwrite update (confirmed, data-loss vector).** The create-vs-update candidate
  finder `search_vault` is a raw case-insensitive substring scan, capped at 10, with no
  semantic ranking — even though Hindsight is already wired into the pipeline. Worse, curate is
  handed candidate **paths only**, never the candidate pages' bodies, and `write_page` fully
  replaces the page preserving only `created:` (and re-stamping `updated:`). So a false-positive
  candidate the model decides to “update” silently overwrites content the model never saw
  (`ingest/curate.py:552,571`; `vault/core.py:438-456`). The fix uses machinery you already
  own: rank candidates with a Hindsight recall (the same RRF recall the query path uses) *and*
  pass candidate page summaries/bodies into the curate prompt, so an “update” is an informed
  merge. Do **both halves in one change** — widening recall without read-before-write would
  *increase* the chance of a confident false-positive overwrite.

- **The cached persona has drifted from the schema (confirmed).** The persona — sent on every
  classify/curate/intent/analyse call — lists only `notes/actions/memories/raw` (omitting
  `entities/` and `media/`, both live types), still instructs “Set kind/… on Actions” though
  ADR 0015 retired `kind`, and tells the model to navigate via `index.md` though ADR 0008 made
  it a static dashboard (`llm/persona.py:24-28,40`). curate is largely protected by the live
  `SCHEMA.md` and the rendered contract, but classify/intent/analyse get only this stale
  guidance. Fix by rendering the folder/type lines from the same `vault.contract` constants the
  contract already uses, not by hand-patching.

- **Deferred curation is not automatic, and the current holds can't be drained (corrected).**
  The drain runs only when `thoth capture` is invoked with **no path** (`__main__.py:357`,
  sole caller of `drain_captures`); nothing schedules it — the appliance crontab and Helm
  CronJobs run only reindex/summary jobs. 12 holds have sat in `inbox/` since 2026-06-01. The
  verification pass sharpens this materially: **all 12 are binary-image stubs, and the drain's
  v1 scope is text-only** (`inbox_drain.py:120-138`) — so running the drain re-curates *zero*
  of them. The real gap is a binary-fetch sweep that doesn't exist; “schedule the drain” would
  fix nothing. At minimum, surface the hold count in the digest so they don't rot invisibly.

**Lower-severity edges (verified, low priority):** the budget guard charges before the curate
corrective retry (a malformed plan double-spends, `client.py:224`); `classify` accepts
unbounded free-form `title` text; the unchanged short-circuit can leave a budget-deferred page
unindexed until a full reindex.

**Under-examined (flagged for your attention):** no reviewer audited the **Whisper voice
path**, whose own docstring (`extract.py:471-510`) flags a live landmine — under a
read-only/confined filesystem the `whisper` CLI catches the write error, logs it, and emits an
**empty transcript**, so a voice capture can silently file a blank page with no error. Worth a
deliberate test given the confinement landmine already documented in the testing skill.
Separately, **content-level prompt injection** (a captured page/image whose text says “set
personal:false, link to evil”) flows through classify/curate; the closed *tool* surface bounds
blast radius, but the injection into the curate prompt itself is unexamined.

---

## Retrieval & the semantic index

The retriever blends three sources — a token-weighted lexical grep over the curated folders,
one-hop outbound wikilink expansion from the grep hits, and Hindsight semantic recall — fused
with textbook Reciprocal Rank Fusion (K=60). This is well-engineered and well-documented, and
the research confirms hybrid + RRF@K=60 is now the *industry default* (OpenSearch / Elastic /
Azure / Weaviate / Atlas), so the choice is well-founded rather than arbitrary.

**Strengths to keep:**

- **RRF matches the literature** (`query/_blend.py:197-263`; ADR 0012): per-source `1/(K+rank)`,
  K=60, structural discovery order as a stable tie-break so a recall-only rank-0 hit still
  earns a slot.
- **Citations are unfabricable by construction.** Every cited path is existence- and
  confinement-checked before a `Citation` is minted; recall hits naming a deleted/escaping page
  are dropped; the Sources block shows only the pages the model said it *used*
  (`query/_retrieval.py:90-111`; `query/_compose.py`). A strong prompt-injection /
  hallucinated-source boundary and the now-correct (NotebookLM-parity) default.
- **Graceful degradation is real.** A `HindsightError` during recall logs and falls back to
  structural-only; recall overlaps via a worker thread so its latency hides behind grep
  (`query/_blend.py:64-98`).

**The gaps are upstream and around the blend — the levers the RAG research weights most:**

- **The `summary` gloss is never embedded (confirmed).** curate's canonical one-line `summary:`
  — the single best retrievable handle for fact-light pages (memories/photos) — feeds grep and
  citations but **both** retain paths strip frontmatter before embedding, so it contributes
  nothing to semantic recall. This is precisely Anthropic's contextual-embedding lever,
  discarded.
- **Ingest and reindex embed *different text* (confirmed by hand).** Ingest retains
  `title + "\n\n" + body` (`ingest/finalise.py:104-108`); reindex — the “authoritative rebuild”
  — retains `body` only (`reindex_from_vault/reindexer.py:249-265`, whose comment says “only the
  body is retained”). The bodies are byte-identical; the sole divergence is the leading title
  line. So a page's embedded representation silently changes depending on which path last
  touched it, and the body-hash idempotency key doesn't detect it.
- **No reranking stage** between RRF fusion and compose.
- **recall sends no `top_k`** — only `{"query": query}` — then type-filters and caps
  client-side (`hindsight.py:416-422`). If the server caps its own result set, a type-scoped
  knowledge query can be starved before the client filter runs. (The server-side cap is
  external and unprovable from this repo, but sending no `top_k` is the wrong default
  regardless.)
- **No retrieval eval.** ADR 0012 itself admits the blend “nearly got reverted” for lack of a
  way to measure it; the Gemini-rate-limit regression (92% of ingests → 0 facts) was only
  caught by reading the daemon log live.
- **`raw/` (the largest folder) is excluded** from both grep and recall by design (ADR 0004),
  reachable only via a curated wikilink — a large blind spot, but a deliberate deferral.

**A strategic reframe the research raises and no review engaged (discussion point).** The
curated content folders total **~89K tokens — under Anthropic's ~200K “skip RAG, just
context-stuff” threshold** — yet thoth always routes through grep+recall+`max_pages=5`, leaning
on the operationally painful Hindsight/Postgres index for a corpus that would fit in a prompt.
This doesn't invalidate the retriever (you still want it for `raw/` and for cheap Slack
answers), but it's a genuine open question whether the embedding index is the right tool for
the *curated* half.

A related honesty note: no reviewer actually ran a real query against the live vault and judged
answer *quality* — the entire retrieval critique is architecture-by-inspection. Until a golden
set exists, “lexical does the real work” and “recall is fact-light” are well-reasoned
inference, not measurement. That is itself the argument for building the golden set first.

---

## The vault knowledge model

The schema is a 5-flat-folder, type-driven model: every content page carries a universal
frontmatter core (`title/type/created/updated/source/tags/summary/personal`) and one of five
types (`entity/note/memory/action/media`), with actionable types adding a `status/due`
lifecycle. It is coherent and unusually well-engineered for a solo project.

**Strengths:**

- **“View-critical facets are properties, not tags” (ADR 0013)** is an evidence-driven
  decision: lint enforces exactly what the Bases dashboards filter on, so a page can't satisfy
  the pipeline yet be invisible to every dashboard. Type-driven (not folder-driven) dashboards
  make recategorisation a single frontmatter edit, recovering sort order via a `prio_rank`
  formula instead of polluting the stored value.
- **Single-source-of-truth contract** (`vault/contract.py:21-113`): classify prompt, write
  gate, lint, summary scans, and the file-plan validator all import these constants, so the
  schema and its consumers cannot drift.
- **`summary` as a rebuildable one-line gloss** and a **pure, deterministic, no-LLM lint**
  (orphans, broken links, frontmatter/vocab, staleness, source-drift, tag-audit, image hygiene)
  are a maintenance backbone an agent can run and trust.

**The weaknesses cluster at the tag-taxonomy / graph layer, and several are honesty-of-record
problems:**

- **SCHEMA's note differentiator is fiction (confirmed).** `SCHEMA.md` says notes differ by a
  bare `concept/comparison/query` tag — those appear in **zero** pages — while **151/156**
  notes carry an undocumented-as-differentiator `kind/*` tag (`kind/how-to` ×63, `kind/concept`
  ×54, `kind/reference` ×39) doing the real sub-type job. This is the worst of both worlds: a
  load-bearing convention the schema lies about. Decide whether to promote `kind/*` to a real
  lint-enforced property or accept it as descriptive tags and rewrite the SCHEMA row. (Note: the
  `kind/*` *tag* namespace is legitimately distinct from the retired `kind` *property* of ADR
  0015 — it is the *un-documented note differentiator* that is the issue, not a duplication.)
- **The entity-kind/kind “redundancy” is refuted — dropped.** Verification showed
  `entity-kind/{org,person,product}` and `kind/*` are disjoint namespaces; the apparent
  duplicate was a `grep -o 'kind/...'` match *inside* `entity-kind/org`. There is nothing to
  collapse.
- **The conflict/quality fields are dead in zero pages (corrected).**
  `contested`/`contradictions`/`confidence`/`sources` are lint-checked (checks 6/8 in
  `checks_metadata.py:159-215`) but present in **zero** pages' frontmatter — the only grep hits
  are Helm YAML in page *bodies*, which the linter never reads. The schema advertises a
  conflict-handling capability the data never exercises. Either make curate emit them (and test
  a seeded conflict yields findings) or retire the checks and the SCHEMA “Update Policy” prose.
- **Graph-thin.** The only relationship primitive is the untyped wikilink; there are no typed
  relationships and retrieval uses only the outbound edge (no backlink hop). The lint *does*
  have three link/graph checks (orphans, broken wikilinks, image hygiene) but no
  typed-relationship or backlink-reciprocity check. This is a real gap versus personal-KG best
  practice, but the right-sized fix is a *tiny* frontmatter relation vocabulary, not a graph DB
  (see roadmap).
- **`index.md` declares `type: summary`**, a value retired from `VALID_TYPES` — by design (it
  survives via name-exclusion in `SPINE_FILES`), so polish not bug.

---

## The actual vault content (audit findings, with the numbers)

The live vault holds roughly **252 curated content pages** (~157 notes, 42 memories, 22
entities, 22 actions, 10 media) plus ~179 raw articles and ~135 assets. Curation quality is
genuinely good: pages are atomic, well-titled, synthesised (not raw OCR dumps), carry a
complete 8-field frontmatter (**0 pages missing any universal field**), and link into real
topic hubs (wikilink density peaks at 3-6 outbound links; only 4 reference pages fall below the
2-link minimum). As a browsable second brain it already works. But there are serious problems
and a set of hygiene issues.

| Finding | Number | Severity | Verdict |
|---|---|---|---|
| Credential-shaped strings filed verbatim (redactor missed them) | a few | **High → handled** | specifics omitted; creds removed from the vault, history scrub planned (see closing note). Durable fix = secret/PII lint |
| Broken wikilinks (links to never-created slugs, Title-Case names, typos) | ~434 | **High** | confirmed (BROKEN bucket ≈562 total: ~434 wikilink + ~59 embed + ~43 orphan-binary + ~26 sidecar) |
| Orphan pages | ~71 | Medium | confirmed (separate ORPHAN bucket — *not* part of the 562) |
| Source-drift findings | ~97 | Medium | **corrected**: these are *real edits* (a `priority:` normalization commit changed body bytes), **not** a hashing bug — check 7 is working correctly |
| Identical-SHA raw duplicate pairs (same content, different slug) | 11 | Medium | confirmed |
| action/media summaries that copy the title verbatim | 28 of 32 | Medium | **corrected** from “all 32” — 4 carry genuine distinct summaries |
| Pages ending in a low-value “Extracted text” OCR block | ~75 of 252 (~30%) | Low | confirmed |
| Inbox holds stuck since 2026-06-01 | 12 | Low | confirmed — but **all binary stubs the drain can't process** |

**The redaction gap (the durable finding; specifics omitted).** Redaction *does* run in general
(~92 `[REDACTED]` markers exist across the vault), but it missed several credential-shaped strings
because its labelled-secret regex needs a `:`/`=` delimiter and the leaks used a space or a
Markdown table cell. The specific leaked credentials have been **removed from the vault** — the
owner has decided the vault is **not** a secret store (Discussion point #1) — and the git history
will be scrubbed (see the closing note); their locations are intentionally not listed here. What
remains worth fixing is the gap itself: add a secret/PII lint and push redaction into curate so a
credential-shaped string is flagged/blocked at capture time rather than filed verbatim (roadmap
**#10**), and narrow the over-broad regex so it stops eating real content (roadmap **#6**).

**Two corrections change the remediation.** The **source-drift** finding is *not* a format bug to
“fix” — the bulk `priority:` migration edited raw bodies, so the real question is whether `raw/`
should be immutable-after-import (and migrations must never touch it). And the **inbox** finding's
fix is not “schedule the drain” (which re-curates zero of these binary stubs) but a binary-fetch
sweep or at least a digest count.

**Privacy framing to document either way:** redaction is a vault-at-rest control only; raw capture
text — including any pasted secret — already reaches Anthropic and Hindsight/Gemini *before*
redaction. And the redactor's broad `[0-9a-fA-F]{32,}` rule has the opposite failure too: it ate a
real git commit SHA inside a GitHub blob URL, breaking the permalink irreversibly. So it both leaks
passwords and corrupts legitimate content — see the security section.

---

## MCP & tool surface

A deliberately small, closed surface of seven `pkm_*` tools (ingest, search, todos, recent,
write_page, read_page, edit_page) backed by an injected `ToolContext`. This is mature,
security-conscious work and it matches Anthropic's own “Writing effective tools for agents”
guidance on most axes.

**Strengths:**

- **Few high-impact, consolidated tools** — `pkm_ingest` runs classify→curate→file→commit→index
  in one call; `pkm_search` returns a composed answer + citations + per-page RRF provenance, not
  raw hits.
- **Tools never raise into the runtime** — every typed collaborator error becomes
  `ToolResult(ok=False)`, so a calling agent always gets a recoverable outcome.
- **Stable handles** — vault-relative path + wikilink + `obsidian://` URI round-trip into the
  next tool call.
- **Teaching error affordances** — ambiguous-slug lists candidate paths; base64 rejection says
  “send text/URL/path”; non-unique edit asks for more context. Textbook agent-experience design.
- **Auth is layered and correctly hardened** — constant-time bearer compare over the full key set
  with fail-fast-when-unset (`mcp_auth.py:95-121`), RS256-pinned Cf-Access JWT defeating
  `alg=none`, and a careful first-party OAuth 2.1 server (PKCE-S256-only, single-use codes, exact
  redirect match, allow-list re-checked on **every** request so de-auth beats the 24h TTL —
  `mcp_oauth.py:297-301`).

**Gaps (mostly cheap):**

- **ADR 0011 contradicts the shipped OAuth server (confirmed).** It is still “Accepted,”
  explicitly decides “No OAuth server in thoth,” and delegates OAuth to Cloudflare — while
  `mcp_oauth.py` is a full first-party OAuth 2.1 + PKCE authorization server, with no superseding
  ADR. The canonical decision record actively misleads. Add a “Superseded by” header or a short new
  ADR recording the reversal.
- **No pagination/cap on list tools.** `pkm_todos` returns all open actions (only `include_done`);
  `pkm_read_page` returns full bodies with no length guard. Harmless at current vault size, but add
  a `limit` + truncation affordance.
- **Bare-slug read globs the whole vault root** and `read_page` enforces only path confinement, not
  the folder/type contract — so `pkm_read_page` can read `index.md`, `SCHEMA.md`, `_bases/*`,
  `raw/*` (writes are safe; they re-validate). Scope the `rglob` to the content folders.
- **Prose-only error codes.** `ToolResult.data` carries inconsistent machine tags; a chaining agent
  must regex-match English to distinguish `slug_ambiguous` from `page_not_found` from
  `vault_conflict`. Standardise on a stable `error` enum.

---

## Security / privacy / ops & testing

The posture is well above hobby-grade. The closed tool surface is genuinely closed; redaction is
centralised at a single write-path chokepoint; the SSRF guard is carefully written and
well-tested; the **leak-scan is CI-enforced** (gitleaks via `pre-commit run --all-files` in the
lint job, not a bypassable local hook); recovery is principled; and the testing skill is admirably
candid that green mocked CI does not prove a boundary change works — deploy-to-verify on the live
VPS is the real gate. The main gaps are operational, plus one redaction problem that cuts both
ways.

- **Liveness is passive only (confirmed).** The heartbeat renders *only* inside the daily digest
  (`summary/engine.py:250-281`); there is no active stale-marker alert. Worse, the digest can be
  skipped-when-empty, and if the failure also kills the summary cron, the staleness signal
  **suppresses itself** — the exact silent-failure mode it was meant to catch. Add a small timer
  that pages via the existing `Alerter` when capture/reindex/push is older than ~26h, plus
  optionally an external dead-man's-switch. For an unattended appliance this is high-value: the
  whole point is noticing when it dies.

- **Redaction is net-negative right now — too loose *and* too tight (confirmed both ways).** It
  misses the real secrets (wrong delimiter / bare table cells), *and* the 32+ hex rule ate a real
  git commit SHA inside a GitHub blob URL, breaking the permalink irreversibly. So it leaks
  passwords while corrupting legitimate content. Rebalance toward precision: exempt URL/SHA tokens,
  require secret-ish context for the long-blob rules, add regression fixtures from the real false
  positives, and add a *labelled* secret/PII lint check so the misses become visible. Better still,
  push redaction upstream into curate — the LLM already *notices* these secrets (it writes “rotate
  if shared” warnings), so prompt it to placeholder the value rather than file it verbatim.

- **Low-severity, verified, fine to leave:** the SSRF guard has a TOCTOU/DNS-rebind window and is
  effectively advisory on the Firecrawl web path (Firecrawl fetches server-side, so the resolved-IP
  check never applies to the real fetch) — worth a one-line threat-model note rather than a fix; the
  budget cap is a non-atomic read-then-write that can overshoot by the concurrency count — bounded
  and self-correcting at a 200/day personal cap on a single-writer daemon.

**One unquantified durability item:** recovery says “the index is rebuildable,” but no one measured
how long a full `reindex_from_vault` takes on ~250 pages given the extraction dependency, nor what
the appliance serves for queries *during* a rebuild. For a solo box a multi-hour blind window is
probably fine — it's just unmeasured.

---

## UX & the second-brain value loop

The capture/retrieve loop is well-engineered mechanically. One private Slack channel is a
frictionless capture surface (text, URL, file, multi-image batch treated as one unit of intent,
voice via Whisper); a placeholder-then-edit responder shows a “Filing…/Looking…” signal within ~1s
and streams per-phase progress; the intent gate is a cheap, total, fail-safe-to-query Haiku
classifier with a recoverable “filed as a note, resend to ask” hint. Retrieval answers are grounded
with an unfabricable Sources block of `obsidian://` deep links. This is real, daily-driver-grade
capture and on-demand recall.

**Where it falls short of a habit-forming second brain is the entire output/feedback half of the
loop:**

- **Old knowledge rots (confirmed, high severity).** There is no on-this-day, no spaced/random
  resurfacing, no link-suggestion. The only “review” surface — the digest's `FLAGGED FOR REVIEW`
  section (`summary/engine.py:393-406`) — is driven solely by a `review:true`/`status:review` flag
  that **nothing in the pipeline ever sets** (verified: 0 pages carry it), so it is permanently
  empty. Obsidian's own random-note core plugin is even disabled in the vault. This is the single
  biggest gap versus a habit-forming PKM, and you have the perfect substrate sitting idle (the
  channel, the embeddings, the frontmatter scans the `SummaryEngine` already runs).

- **The digest is a to-do nag, not a briefing.** It surfaces overdue actions, due-soon, and media
  backlog — almost no *knowledge* value, no emerging themes, no resurfaced note — even though the
  persona prompt itself promises “emerging themes” that `engine.py` never computes. Risks being
  muted.

- **Two prominent docs over-promise (both confirmed by hand).** The README sells “web-blended …
  research” answers and names “**Exa** … handle[s] web search” — but `QueryEngine.answer` is
  strictly vault-only and **there is no Exa client or dependency anywhere in the repo** (only the
  README/PKG-INFO marketing copy mentions it). And `slack-setup.md` (echoed in `intent.py:4` and
  ADR 0003:12) documents a “reply *y* to save” confirm flow that **does not exist** — captures file
  directly, there is no `pending`/`affirmative`/`'y'` handler in `slack_app/`, and a stray “y” just
  falls through the intent gate. These erode trust the first time you test them.

- **Single-turn retrieval.** A follow-up (“tell me more,” “and X?”) is re-gated from scratch with no
  memory of the prior answer, so the channel can't sustain an exploratory conversation — the
  highest-value retrieval mode.

---

## Best-practice gap analysis (agentic memory / RAG / PKM methodology)

Reading thoth against the 2025-2026 state of the art, the divergences concentrate into five
themes. The throughline: thoth has converged on the same *substrate* the field recommends (linked
Markdown-as-memory, hybrid RRF, closed tool surface, raw=episodic / curated=semantic tiering) and
should now adopt the field's *ideas* without adopting its *infrastructure*.

**Theme 1 — The loop only runs forward.** Every mature PKM methodology (Matuschak's spaced
resurfacing, Tana Daily Surfacing, Forte's Distill) and every 2025-2026 agent-memory system (Letta
sleep-time compute, A-MEM note-evolution, Generative Agents reflection) treats scheduled resurfacing
and offline consolidation/reflection as table stakes. Pointedly, thoth's own index dependency —
Hindsight — defines three operations, **retain / recall / reflect**, and thoth wires only the first
two. Its own dependency points straight at the missing feature.

**Theme 2 — Write-without-read.** Mem0's ADD/UPDATE/DELETE/NOOP and Graphiti's entity-resolution
loop both insist: retrieve the semantically-similar existing records, show them to the model, *then*
decide. thoth half-built the candidate finder but doesn't ground it — hence the blind-overwrite
update, the ~434 invented wikilinks, and the absence of any entity-resolution step before minting a
near-duplicate node.

**Theme 3 — Retrieval levers left on the table.** Anthropic's Contextual Retrieval shows the biggest
failure-rate reductions come from prepending page-level context before embedding (~35%) and
reranking after fusion (~67% vs ~49% for fusion alone) — and that corpora under ~200K tokens should
often skip RAG entirely. thoth discards the summary before embedding, embeds different text on its
two paths, has no reranking, and can't measure any of it.

**Theme 4 — Docs/schema assert capabilities the code doesn't deliver.** ADR 0011 vs the OAuth
server; the README's phantom Exa/web-blend; the phantom “reply y” flow (asserted in *three* places);
the drifted persona; the fictional note differentiator; the dead conflict fields. For an
AI-navigable project where the docstrings and ADRs *are* the assistant's context, this is uniquely
corrosive — and the cheapest theme to fix.

**Theme 5 — Two silent-failure modes.** Passive-only liveness that can suppress its own signal, plus
a redactor that leaks real secrets while breaking real links. The two failure modes an unattended
solo box can least afford.

**Right-sizing — things that look like gaps but are fine to leave.** No GraphRAG / graph DB /
temporal-KG (the human-curated wikilink graph already gives the cheap 80%); `raw/` excluded from
retrieval (deliberate ADR 0004 deferral); no MemGPT-style self-editing core (a single vault-stored
owner-profile note is the only worthwhile sliver); no procedural-memory tier (correctly absent — a
closed tool surface has no skills to accumulate); HyDE / heavy query rewriting (the keyword
intent-gate is the right dose at this corpus size); an API reranker as a hard dependency (use a
*local* cross-encoder if at all, to honour ADR 0012's no-new-dependency stance); de-vendoring the
8.6 MB Excalidraw plugin (a real clone-bloat wrinkle, but the fixes add seeding fragility for a
cosmetic win). The research is emphatic that the heavyweight versions of Themes 1-3 — graph
databases, per-edge bitemporal modelling, dual-agent runtimes — would duplicate the canonical vault
for one user. **Take the discipline, not the machinery.**

---

## Prioritised roadmap

Effort S/M/L; impact rated for *this* solo, vault-canonical appliance. Within each tier, ordered the
way I'd actually do them.

### Quick wins (do this week)

| # | Item | Effort | Impact | Notes |
|---|---|---|---|---|
| 1 | **Scrub leaked credentials + rewrite vault git history** | S | High (privacy) | Creds removed from the working tree; values remain in history → scrub it (also reclaims space). See closing note |
| 2 | **Unify Hindsight retain text + embed the `summary`** (`title + summary + body` via one shared helper) | S | High | Closes ingest-vs-reindex divergence; Anthropic contextual-embedding recipe in miniature; best retrieval impact-per-effort |
| 3 | **Send an explicit `top_k` to recall** | S | Medium | Stops a server-side cap starving type-scoped queries |
| 4 | **Regenerate/trim the persona prompt** from `vault.contract` constants | S | Medium | Fix entities/media omission, retired `kind`, retired `index.md` nav |
| 5 | **Docs honesty batch**: soften README web/Exa claims; delete the “reply y” flow; reconcile/supersede ADR 0011 | S | Medium | Cheapest theme, outsized trust payoff on a public repo |
| 6 | **Narrow the redaction regex** (exempt URL/SHA tokens; add false-positive fixtures) | S-M | Medium | Stop it corrupting real links; pair with #10 |
| 7 | Add a CI import-linter contract asserting the downward-only graph | S | Low-Med | Mechanically protects the layering an AI assistant is most likely to erode |

### Worth doing (medium bets)

| # | Item | Effort | Impact | Notes |
|---|---|---|---|---|
| 8 | **Resurfacing surface** (on-this-day + spaced/revisit in the digest) | M | High | Single biggest habit-loop gap; reuses existing frontmatter scans + embeddings, near-zero LLM cost |
| 9 | **Slug resolver — stop the agent inventing wikilinks** | M | High | Converts ~434 broken links into valid links or honest absence; unblocks any later typed-edge work |
| 10 | **Secret/PII lint check + push redaction into curate** | M | High | Targeted complement to #6; the LLM already notices secrets |
| 11 | **Active liveness watchdog** (timer → `Alerter` when markers stale; optional dead-man's-switch) | S-M | High | The point of an unattended box is noticing when it dies |
| 12 | **Read-before-write on create-vs-update** (rank with Hindsight; pass candidate body into curate) | M | High | Closes the blind-overwrite data-loss vector; **do as one change** with the recall widening |
| 13 | **Tiny golden-set retrieval eval** (~20-30 `query→path` pairs, recall@5 / MRR behind live-smoke) | M | Med (high as enabler) | Precondition for honestly evaluating #14; ADR 0012 literally asks for this |
| 14 | **Cheap LLM / local rerank** over the fused candidate set | M | Medium | Gate behind #13 proving it helps; prefer local to keep ADR 0012's no-dependency stance |
| 15 | Real action/media glosses (or exempt them); drop/collapse the OCR block; surface the stranded inbox count | S-M | Low-Med | Inbox needs a *binary-fetch* path, not the drain |
| 16 | Reconcile the `kind/*` tag namespace with `SCHEMA.md` | M | Medium | Promote to a property or rewrite the SCHEMA row |
| 17 | Bounded pagination + machine-readable error enum on the MCP tools | S/M | Medium | Anthropic tool-design guidance |

### Big bets (only if you want to go deep)

| # | Item | Effort | Impact | Notes |
|---|---|---|---|---|
| 18 | **Nightly “gardener” / consolidation pass (proposal-only)** | L | High | The compounding-value engine; merges, missing backlinks, candidate links, orphan flags → a review page, never auto-applied. Exercises Hindsight's unused `reflect` |
| 19 | Tiny typed-edge vocabulary (`supersedes:`/`contradicts:`/`part_of:`) + entity-resolution before minting nodes | M-L | Medium | Depends on #9 and #20; cheap 80% of a KG with zero graph DB |
| 20 | Populate **or** retire the conflict/quality fields | S/M | Low-Med | Honesty; precondition for #19 (currently dead in zero pages) |
| 21 | Context-stuffing path for the ~89K-token curated corpus | M-L | Medium (speculative) | Pilot only if Hindsight ops pain keeps recurring |
| 22 | Per-thread conversation memory on the query path | M | Medium | Unlocks exploratory “thinking with your notes” |

### Not worth it (deliberate non-goals — record the rationale)

| Item | Why not |
|---|---|
| Graph DB / Graphiti / full GraphRAG | Duplicates the canonical vault for one user; the wikilink graph already covers single-hop |
| Hosted reranker (Cohere/Voyage) as a hard dependency | Violates ADR 0012's no-network-dependency stance; use a local cross-encoder if at all |
| HyDE / heavy query rewriting | The keyword intent-gate is the right dose at this corpus size |
| Making `raw/` directly searchable (chunking the whole folder) | Deliberate ADR 0004 deferral; curate-then-link mostly covers it |
| Code-execution-with-MCP / progressive disclosure | Pays off at dozens of tools, not 7; would breach the closed surface that bounds injection. **Record as an ADR non-goal.** |
| Multi-tenant / refresh-token / migration scaffolding | Solo box, clean-slate stance |
| Making the budget cap atomic / merging the two daemons | Bounded overshoot, self-corrects daily; a one-line docstring honesty-fix instead |
| De-vendoring the Excalidraw plugin | Real wrinkle, but the fixes add seeding fragility for a cosmetic win |

### Suggested first-two-weeks sequence

1. Day one: **#1 rotate secrets** → **#10 secret lint + curate redaction** (so it can't recur) →
   **#6 narrow the blunt regex**.
2. **#2 unify retain + embed summary** and **#3 top_k** — biggest retrieval impact-per-effort, ship
   together.
3. **#4 persona**, **#5 docs/ADR honesty** — cheap, batch them.
4. **#13 golden set** — before any further retrieval tuning, so #14/#9 are measurable.
5. **#8 resurfacing** + **#11 watchdog** — the two highest-impact behavioural additions.
6. **#9 slug resolver** + **#12 read-before-write** — close the write-without-read theme and unblock
   the KG line.

---

## Strengths to preserve

Right-sizing cuts both ways: several decisions are *better* than typical and should be treated as
invariants no improvement may erode.

1. **The closed, validated tool surface holds.** 7 fixed tools, structured errors instead of raised
   exceptions, path-confined writes, a forced-tool file-plan validated by the vault's own
   validators. Keep additions *additive read tools*; never code-exec or dynamic catalogs.
2. **The schema has exactly one source of truth.** Render docs/persona/prompts *from*
   `vault/contract.py`; never hand-maintain a parallel copy. (Every Theme-4 fix should route *into*
   this single source.)
3. **The vault stays canonical and the moving parts stay few.** Adopt the memory-systems ideas
   (supersession, consolidation, typed edges, resurfacing) as vault-native frontmatter + prompt +
   batch-job changes — never as a graph DB, an API reranker hard-dependency, or an always-on second
   runtime.
4. **Durable, precisely-deferred ingest** with persist-before-LLM and a careful blind/defer/abort
   taxonomy.
5. **Unfabricable citations + graceful degradation** in retrieval.
6. **The recovery story** (vault-canonical, index-rebuildable, `state.db` intentionally unbacked)
   and the **honest SPEC de-rating**.
7. **CI-enforced leak-scan + public-repo/private-vault split.**

**The two most dangerous “improve X, break Y” traps:** (a) widening create-vs-update recall *without*
read-before-write lands a silent-overwrite regression on the canonical store — do both in one edit;
(b) unifying retain text *without* routing both paths through one helper diverges the idempotency
key. Both are net-positive changes that are only safe done coherently.

---

## Discussion points & decisions for the owner

These are genuine judgement calls, framed as questions rather than prescriptions.

1. **Burned credentials — ~~rotate now?~~ RESOLVED (2026-06-14).** Decision: **the vault is not a
   secret store.** No credentials live in it — no `type: credential` tiering, no redactor
   exceptions. Rationale: "private GitHub repo" is not the boundary that matters — every
   curated/indexed page egresses to Anthropic (classify/curate + Hindsight fact-extraction on each
   reindex; embeddings are local), exists in plaintext on ~5 clones (workstation, VPS, k3s PVC,
   GitHub, Obsidian-sync devices), and is echoed into Slack/MCP transcripts on retrieval. Follow-up
   (done / planned): the leaked credentials have been removed from the vault (the AnywhereUSB
   *default* sticker password — public — is kept), and the git history will be scrubbed (closing
   note). The redactor + a secret/PII lint (roadmap **#10**) become the *enforcement* of this
   policy — flag/block a credential-shaped string at curate time — rather than optional hardening;
   narrowing the over-broad regex (**#6**) is now just hygiene so it stops eating real git SHAs /
   URLs.

2. **Is the curated vault small enough to skip RAG for knowledge Q&A?** At ~89K tokens it's under
   Anthropic's 200K context-stuffing line. Keep investing in the operationally painful Hindsight
   index for the part of the vault that fits in a prompt, or pivot to context-stuffing the reference
   set and reserve the index for `raw/`? This reframes most retrieval findings.

3. **Which slice of the closed loop first** — scheduled resurfacing, a propose-only gardener pass, or
   simply making curate auto-flag low-confidence writes so the already-built FLAGGED section has
   input? All three are right-sized; the question is sequencing.

4. **Is `raw/` immutable-after-import or not?** The ~97 “drift” findings are *real edits* from a bulk
   `priority:` migration that touched raw bodies. Either `raw/` is immutable (and migrations must
   never touch it) or it isn't (and check-7 drift on `raw/` is expected noise). Picking one makes ~97
   lint findings either actionable or suppressible.

5. **Is the two-process model (separate `thoth-slack` + `thoth-mcp` sharing one tree + `state.db`) a
   deliberate security boundary?** It's the root of three findings (cross-process budget overshoot,
   cross-process capture-lock gap, shared-db races). One co-located process resolves all three but
   trades away the isolation between the write-capable ingest path and the MCP surface. Worth keeping
   the edges, or worth simplifying?

6. **Untyped edges + no entity-resolution — worth a tiny typed-link vocabulary?** A 2-4 verb
   frontmatter relation set plus a grep+recall entity match before minting a new page is the
   right-sized improvement, but it adds curate/lint burden. Is the RRF blend already “good enough”
   that the graph layer is low-ROI for a solo vault?

7. **Resurfacing cadence and retrieval voice are personal-taste dials.** Daily on-this-day can become
   noise on a large vault — opt-in weekly section, or folded into the daily digest behind a cap? And
   is the terse, tool-like persona right for the *retrieval* half, or would a warmer, more
   exploratory voice (offering related pages, asking a clarifying follow-up) create more
   daily-driver pull? Both are worth A/B-ing on yourself.

---

## Appendix — how this review was produced

- **Coverage:** 8 subsystem deep-dives (ingest, retrieval/index, schema, vault-content audit, MCP,
  architecture, security/ops/testing, UX) + 4 web-research sweeps (agentic memory, RAG/retrieval,
  PKM methodology & tools, knowledge-graphs / MCP tool design). 56 agents total.
- **Verification:** each reviewer emitted its most load-bearing checkable claims; 39 were
  adversarially re-checked against the code/vault and **7 were corrected** (entity-kind/kind
  “redundancy” refuted; conflict fields dead in *zero* not ~2 pages; source-drift = real edits not a
  hash bug; summaries duplicate title on 28/32 not all 32; the inbox holds are binary-only; capture
  dedupe has a content-SHA layer; the link/graph lint has three checks not two). All corrections are
  folded in above.
- **Hand re-checks:** the leaked-secret locations, the missing Exa dependency, the unimplemented
  “reply y” flow, the ingest-vs-reindex embed divergence, and the permanently-empty review queue were
  re-verified directly against source before publishing.
- **What was *not* done (and should be):** no live query was run against the vault to judge *answer
  quality* — the retrieval critique is architecture-by-inspection. Build the golden set (#13) to turn
  inference into measurement.

---

## Closing note — vault test-data & history scrub (2026-06-14)

The credential leaks this review originally enumerated have been **removed from the vault**, and
the specific locations have been stripped from this document. The one credential deliberately kept
is the AnywhereUSB *default* password, which is printed on the device sticker (public).

Two follow-ups are planned and owned separately from the engineering roadmap above:

- **Review and scrub all test data.** The current vault is a working/test corpus; we will sweep it
  for any remaining sensitive or throwaway content before it is treated as a real second brain.
- **Scrub the vault's git history.** A history rewrite (wanted independently to reclaim repo
  **space** — large binary assets and superseded captures) will also purge the credential values
  that still exist in past commits. Until then, treat the vault's history as carrying those values.

The durable engineering takeaway is unchanged and lives in the roadmap: the vault is **not** a
secret store (Discussion point #1), and a secret/PII lint + curate-time redaction (**#10**) is what
enforces that going forward — so accidental credential capture is caught at the door rather than
scrubbed after the fact.
