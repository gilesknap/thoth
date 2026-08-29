# Architecture review notes

A standing record of an architecture review of thoth carried out during Phase 3 (2026-05-30), ahead of making the repository public.

It captures the reasoning behind the design as it stands, the strengths worth preserving, the trade-offs we made consciously, and the findings that turned into tracked work. The detailed, actionable findings live as GitHub issues, indexed at the end, so this page holds the narrative and the conclusions that did **not** need a separate issue.

This is an *explanation* in the Diátaxis sense. It is here to help you understand *why* thoth is shaped the way it is, not to prescribe API details.

## The shape of the system (recap)

thoth is a deliberately thin appliance rather than a general agent framework. A handful of facts drive almost every decision below:

- **The vault is canonical.** A git-backed Obsidian vault of plain markdown and binary assets is the single source of truth, and everything else is a disposable projection of it.
- **Hindsight is a rebuildable index, never the store.** Losing it loses nothing permanent, because it is re-derived from the vault.
- **`state.db` is transient working memory.** It is small, single-writer, gitignored and TTL-pruned, and never a knowledge store.
- **The general-agent role is external.** Claude Code and claude.ai drive thoth's tools over MCP, and the appliance itself is a closed set of capture and retrieve tools rather than a conversational agent.
- **The tool surface is closed.** The appliance LLM has no shell and no arbitrary filesystem access, only its fixed, validated tools.

## What is working well

These are strengths the review wanted to name explicitly, so that they are preserved rather than eroded by later changes.

### The closed surface bounds the prompt-injection blast radius

thoth ingests arbitrary web pages and files and feeds them to an LLM that then writes to the vault, which is a textbook prompt-injection vector.

The architecture's answer is structural rather than defensive. The appliance LLM has no shell, no arbitrary filesystem and no outbound capability beyond its fixed tools, so a malicious document **cannot** exfiltrate data or execute commands.

The residual risk is bounded to a poisoned vault page or a mis-classification, and even that is checked by schema-validated curator output, path confinement and secret redaction.

This is the single biggest security property of the design, and it is a *consequence of the closed surface* rather than an add-on. It is the same reason the SPEC can drop Hermes' Tirith scanner and command-allowlist entirely.

### Zero data lock-in and graceful degradation

Knowledge lives as plain markdown in git. Lose thoth entirely and you still have an Obsidian vault a human can read and edit by hand.

The index is disposable and `state.db` is transient, so the durable surface area is just markdown files in a git repo.

This reversibility is a genuine architectural virtue, and it is worth protecting against future features that would quietly make some other store load-bearing.

### Lean, well-chosen FOSS reuse

The heavy lifting is delegated to widely used libraries rather than reinvented: `httpx`, `python-dotenv`, `python-frontmatter` with `pyyaml`, `anthropic`, `firecrawl-py`, `mcp` and `FastMCP`, `slack-bolt`, plus the stdlib `argparse`, `zoneinfo`, `hashlib` and `importlib.resources`.

The dependency list is small and the right shape. The review looked specifically for re-implemented wheels and found only minor ones, listed under *FOSS reuse* below.

## Topics reviewed

### Public-release readiness

The repository is safe to make public. Configuration is entirely environment-driven, with `config.py` as the single source of truth reading only the environment and `.env`, no secrets are committed, and none appear anywhere in git history, since `.env` is gitignored and was never tracked.

The only personal data is standard author metadata of name, email and GitHub org, which is expected for an open-source project. Defence-in-depth already exists via `.gitleaks.toml` and a pre-commit hook.

One ergonomic wrinkle surfaced. The vault git wrappers default the push remote to an individual's personal repo, which is the wrong default for a reusable public project, so that became a tracked fix rather than a blocker.

### Relationship to the Hermes predecessor

thoth is a deliberate downsizing of Hermes, a general-purpose conversational agent with 96 skills, around 29 LLM providers, 15 or more chat gateways, shell access, browser automation and personalities.

The review classified the differences into three buckets:

- **Intentional descopes.** The conversational loop, the persistent session knowledge DB, the skill ecosystem and curator, multi-provider routing, non-Slack gateways, browser automation, the shell and Tirith security layer, and personas. All of these are the *point* of the redesign, because they are what keep thoth small, auditable and secure. The general-agent role moved to Claude Code and claude.ai.
- **Planned but not yet built.** Summaries, the MCP server, the full reindex into Hindsight wiring, system cron and systemd, lint, the migration script and config backup. These are skeletoned on the Phase-3 branch, so the real exposure is *unfinished work* rather than lost capability.
- **Genuine silent losses.** Only a few, and none critical for a single user: multi-channel Slack support with allow-lists and per-channel prompts, Hindsight recall-budget tuning, and web auto-context during ingest. These are deferrable, and they are recorded here so that the decision to defer them is conscious rather than accidental. The remaining Hermes losses (LSP, checkpoints, goals, sessions, delegation, usage analytics, TTS) are genuinely not PKM concerns, since git replaces checkpoints and external agents replace delegation.

The conclusion is that there is essentially **no accidental capability loss that matters**. The simplification is well-reasoned.

### Slack routing and the intent-gate enhancement

Free-text routing over Slack at the time of the review was a deterministic `if/elif` ladder rather than an LLM dispatcher. Explicit prefixes (`note:`, `capture:`, `save:`), bare URLs and file uploads route to capture, and everything else in free text is treated as a query.

Sonnet *does* make decisions, but only *within* a branch, such as how to classify content during ingest, and never *across* branches to choose the feature.

The consequence is a sharp edge: plain prose like "remind me to call the dentist" is treated as a query and not filed, unless you prefix it.

The review judged a natural-language intent gate a good enhancement, and a Slack-only one, because MCP already exposes explicit tools so the calling agent dispatches there.

The chosen design keeps the explicit prefixes as deterministic overrides and puts a cheap two-way capture-or-query classifier in front of the bare free-text branch only, with query as the safe fallback. That preserves the deliberate separation between the read-only retrieval path and the write-capable ingest engine.

### Hindsight: embedding, "chunking", and provenance

A key clarification from the review is that **thoth does no embedding and no chunking itself.** Both happen inside the external `hindsight` CLI.

More importantly, Hindsight does **not** chunk text by tokens at all. Its retain pipeline runs an LLM to **extract atomic facts**, then embeds each fact. thoth hands over a whole curated page per `retain()`, and Hindsight decomposes it.

The review's take is that for a find-my-stuff-and-take-me-to-the-note second brain, fact-extraction is a better fit than fixed-size chunking. Recall matches at the level of meaning, there are no chunk-boundary artifacts, and there is nothing to tune.

The lossiness is acceptable *precisely because* the vault is canonical and the index disposable. Every hit points back to the real note via an `obsidian://` link, so imperfect extraction is always recoverable.

This would be the wrong call if Hindsight were the store of record, or if verbatim recall were required, and neither is true here.

That same fact-extraction creates the design's most important integration risk, which is **per-fact provenance**. thoth needs to recover the owning vault path for every recall hit to build citations, but if a page is split into many facts, the in-band `SOURCE:` sentinel may not travel with each one.

Locating the official CLI docs (<https://hindsight.vectorize.io/sdks/cli>) during the review showed the real surface differs substantially from what the code assumed: a different binary name, the bank as a positional argument, `memory retain` and `memory recall` subcommands, JSON output, and first-class `--tags` filtering.

The likely resolution is to use **tags** as the provenance channel rather than the in-band sentinel, to be confirmed against the live binary. These corrections, the provenance verification, the bank rename away from the `hermes` holdover, retry hardening and an optional index backup are all tracked work.

On backup specifically, the review endorsed an *optional* `pg_dump` of the Hindsight index after the nightly reindex, strictly subordinate to `reindex --full-rebuild`, which remains the canonical recovery path.

The strongest argument for it is not cost but **determinism**. A rebuild re-runs LLM fact-extraction and produces a *different* index, whereas a restored dump reproduces the exact prior recall surface.

### FOSS reuse pass

Beyond the well-chosen libraries already in use, the review found only small re-implementations worth addressing. Those are two hand-rolled slugifiers that `python-slugify` would unify, which would also fix Unicode transliteration so that `café` slugifies to `cafe` rather than `caf`, and the absence of retry and backoff around the Hindsight subprocess, where `tenacity` is the FOSS answer for the daemon cold-start case.

Several hand-rolled pieces were deliberately left alone, because reuse would be worse or pointless:

- **`git_sync.py` shelling to bash scripts** is required for the `gh` credential-helper auth dance, and GitPython would fight it.
- **`vault.py` path confinement and slug grammar** is security-critical, so owning and auditing it is correct.
- **`templates.py`** ships seed files via `importlib.resources`, which is not a Jinja2 use case.
- **The `argparse` CLI**, the **strict `_looks_like_url`** and the **base64 and data-URI guards** are trivial, zero-dependency and adequate.

### New findings from unattended-operation analysis

Looking at thoth as an unattended VPS appliance surfaced findings that are not about features at all:

- **Capture durability is gated on the LLM.** The ingest pass order persists the raw source only *after* a successful `classify()` call, so an Anthropic outage loses the capture rather than deferring it. For a throw-it-at-the-box-and-forget system this is the sharpest failure mode, and it became high-priority tracked work to write raw before any LLM call.
- **There is no observability.** If the daemon dies, a push stalls on an unresolved rebase conflict, a quota is exhausted, or the Hindsight daemon does not start, you have no signal beyond a missing daily digest. An errors-to-Slack and heartbeat mechanism is tracked.
- **There is no cost circuit-breaker.** Pay-as-you-go keys plus per-page fact-extraction mean a runaway loop or an accidental full rebuild has unbounded cost. A daily spend guard is tracked.
- **The integration cliff.** CI is mock-only by design, so it is import-safe and offline, which is good, but every real boundary is unverified until VPS-time. A first-light smoke-test checklist is tracked to make a deploy repeatable rather than improvised.
- **Smaller items.** The in-memory Slack dedupe loses state across restarts, so a redelivery can double-ingest, and the type and tag taxonomy is restated in several places that must agree. Both are tracked.

## Conscious trade-offs not requiring action

These are recorded here so that they are revisited deliberately rather than stumbled upon:

- **Single-LLM-vendor coupling.** Dropping multi-provider support is the right call for cost transparency and simplicity, but it means Anthropic availability equals capture-curation availability. The capture-durability work above mitigates the worst of it.
- **Manual secret and token rotation.** The Slack app token and a fine-grained PAT, which the SPEC notes has a 90-day expiry, rotate by hand. On an unattended box an expired token is silent capture death until noticed. This ties to the observability work and to a scheduled rotation reminder.
- **Two-way sync is fail-loud and never-clobber.** `vault-commit` correctly refuses to force-push on a rebase conflict, but the agent cannot resolve it, so ingestion can quietly stall until a human resolves the conflict in Obsidian. That is acceptable given the safety guarantee, *provided* observability makes the stall visible.

## Review record: tracked issues

The full set of actionable findings from this review, as GitHub issues:

**Public release / repo hygiene**
- [#4](https://github.com/gilesknap/thoth/issues/4): Remove individual-centric default push remote.

**Slack surface**
- [#5](https://github.com/gilesknap/thoth/issues/5): Natural-language intent gate for free-text routing (Slack-only).
- [#18](https://github.com/gilesknap/thoth/issues/18): Durable Slack event dedupe to prevent double-ingest on restart.

**Hindsight (semantic index)**
- [#13](https://github.com/gilesknap/thoth/issues/13): Correct the Hindsight CLI invocation to match the official docs.
- [#7](https://github.com/gilesknap/thoth/issues/7): Verify `SOURCE` and tag provenance survives Hindsight fact-extraction.
- [#9](https://github.com/gilesknap/thoth/issues/9): Rename the Hindsight bank from `hermes` to `thoth`.
- [#11](https://github.com/gilesknap/thoth/issues/11): `tenacity`-backed retry around the Hindsight subprocess.
- [#8](https://github.com/gilesknap/thoth/issues/8): Optional `pg_dump` of the index plus manifest for fast restore.

**Reliability / unattended operation**
- [#14](https://github.com/gilesknap/thoth/issues/14): Decouple capture durability from the classify LLM call.
- [#15](https://github.com/gilesknap/thoth/issues/15): Unattended observability, meaning heartbeat plus errors-to-Slack.
- [#16](https://github.com/gilesknap/thoth/issues/16): Cost circuit-breaker and daily spend guard.
- [#17](https://github.com/gilesknap/thoth/issues/17): First-light integration smoke-test checklist.

**Code quality / reuse**
- [#10](https://github.com/gilesknap/thoth/issues/10): Adopt `python-slugify` for deduplication and Unicode-correct slugs.
- [#19](https://github.com/gilesknap/thoth/issues/19): Consolidate the type and tag taxonomy to one source of truth.
