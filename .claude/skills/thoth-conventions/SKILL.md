---
name: thoth-conventions
description: >-
  Engineering and process conventions for working on thoth that aren't obvious
  from the code — how to shape LLM output, the clean-slate stance, and how
  findings turn into issues and docs. Use when implementing a feature, changing
  LLM-facing prompts, or deciding how to record work.
---

# thoth conventions

## Shape LLM output with the prompt, not string-processing

When the output of an LLM call needs a particular shape (clean Slack prose,
title-only links, a structured file-plan, no leaked markup), get it from the
**prompt plus model-reported structured data** that the harness then formats —
**not** by regex-stripping or rewriting the model's text after the fact.

Post-hoc string processing of model output is brittle: it accretes special cases,
and it backfires (e.g. an embed-stripping pre-processor once blinded the model to
images so "what images do we have?" answered "none"). If the output is wrong,
change the prompt or ask the model for structured fields — don't bolt on a
post-processor. Reserve any string handling for genuinely mechanical, well-defined
transforms, not for cleaning up model prose.

### Force structured output via tool-use, not free-text JSON

The strongest form of the rule above: when a call must return a structured object,
**force it through Anthropic tool-use** rather than parsing free-text JSON. Define a
tool whose `input_schema` mirrors the contract, set `tool_choice={"type": "tool",
"name": "<tool>"}`, and read the `tool_use.input` dict (the transport handles
escaping). Keep the validator as the post-call gate — tool-use guarantees *valid
JSON*, not a *valid plan*. Curate's `submit_file_plan` does this (#110); the loop
helpers (`_tool_use_blocks` / `_block_id` / `tool_result_block` /
`assistant_blocks_message`) live in `thoth.llm`. This was a real fix: hand-serialized
JSON aborted with "Unterminated string" on ~39% of a real import, because the model
emitted raw control chars (newlines, tabs, U+00A0) inside the JSON string.

**Foot-gun — the repair/retry turn after a tool call.** When re-asking the model
after a rejected tool call (e.g. plan failed validation), the follow-up **user turn
must lead with a `tool_result` block keyed to the prior `tool_use` id** — a
plain-text user turn there makes the live Messages API reject with HTTP 400. Mirror
curate's repair turn: echo the assistant's `tool_use` turn, then a user turn whose
first block is `tool_result_block(<id>, <repair_text>, is_error=True)`. Only the
no-tool-call branch (model declined the tool) takes a plain-text follow-up. Injected
fakes ignore this, so it passes CI and only shows up live — see `thoth-testing` for
how to test it.

## Clean slate — no backward-compat or migration prose

Per `CLAUDE.md`: this is a personal, single-user project in an early, iterative
phase. There are no other deployments to preserve — config, vault, and the Slack
app are re-created from scratch when needed. So **do not** write migration guides,
compatibility shims, or "if you previously did X, do Y" prose. When something
changes, document only the new way and assume a clean slate. The vault itself is
disposable test data during this phase — never write backfill/migration code for
existing vault content.

## Bulk vault migrations — sync first; obsidian-git mis-merges YAML silently

A migration that rewrites **every** page's frontmatter is uniquely dangerous under the
vault's two-way obsidian-git sync. Git merges frontmatter as **text, not YAML**: when the
migration commit later merges against a divergent obsidian-git "vault backup" line, git
auto-interleaves the old and new frontmatter on non-overlapping lines **with no conflict
markers** — producing duplicate keys and orphaned `- tag` list items that commit silently.
This actually happened to the ADR 0013 schema migration: the clean migration commit was
corrupted by the very next auto-backup merge, breaking several `actions/` pages.

Before any bulk frontmatter rewrite: **sync/pull every device first**, run the migration on
one checkout, push, then pull everywhere before editing resumes — so the migration never
merges against a stale backup line. Do **not** add a vault pre-commit lint hook as a guard:
it is fiddly on mobile (a failed commit blocks the sync) and `pre-commit` does not fire on
merge commits anyway, which is exactly where this corruption is born. The failure is
**silent** downstream too — `thoth lint` and the nightly reindex both catch the YAML parse
error and skip the page (`engine.py` does `except yaml.YAMLError: continue`), so a
mis-merged page quietly drops out of the Hindsight index rather than raising. The cheap,
reliable safeguard is the pull-first habit, not tooling.

## Live config/code is the source of truth — not a stale issue body or plan

Issue bodies and saved plans capture intent *when written* and go stale as the code
moves. When authoring anything that mirrors a real boundary's configuration (a Helm
chart, deploy manifest, env wiring), read the **current** source — `src/thoth/config/`
for which keys `Config` actually reads (required vs optional), and
`deploy/*.env.example` / the systemd units for live provider/model/key choices — and
let those win over an older issue/plan. Grep for where a value is *consumed*, not just
mentioned: the #158 chart demanded a dead `GEMINI_API_KEY` (read into `Config` but used
nowhere) because it followed the issue body instead of the env example that had already
moved Hindsight to Anthropic.

## The MCP surface must stand alone — never use the vault as a retrieval backdoor

thoth is partly a **prototype for an organizational-memory system** where, in the
target deployment, the backing store (the vault) is **never** available to the
agent — only the MCP tools (`pkm_search` / `pkm_recent` / …). A local
vault checkout is a development convenience, **not** part of the product.

So when demonstrating, testing, or answering with PKM capability, work **through
the MCP tools only**. Do **not** `grep`/read vault files to answer a question or to
"get a definitive list" — that crutch makes the agent look capable while leaving
the actual product unable to do the same job for a real deployment. Treat any case
where you *needed* the filesystem as a **product gap to fix in the MCP** (recall
completeness, enumeration/pagination, match-count + truncation signalling), and
file it per *Findings → issues → docs* below. Editing vault files directly is fine
as an **authoring/admin** action (e.g. a dedupe); the rule is specifically about
not back-dooring *retrieval* the MCP is supposed to provide.

## Findings → issues → docs

When a review or investigation surfaces problems, record them as **GitHub issues**
(one per discrete problem), and, where relevant, link them from an explanations
doc page so the reasoning is captured, not just the ticket. A good issue states
the symptom, the root cause, the fix direction (consistent with these
conventions), and acceptance criteria. File a separate issue for an unrelated
problem discovered in passing rather than folding it into the current change.

## PRs and merges

- A PR body should explain *why* (the design rationale), not just *what*.
- Reference the issue it closes (`Fixes #N`).
- Default merge strategy is a **merge commit**; reserve squash for messy
  back-and-forth branches.
- For boundary/SDK changes, verify live before merging (see the `thoth-testing`
  skill).

### Stacked PRs — GitHub mechanics that bite

- Deleting a branch that is the **base of another open PR closes that PR** (no
  auto-retarget), and a PR whose base branch is gone **cannot be reopened**.
  Retarget dependents FIRST — `gh api -X PATCH repos/<o>/<r>/pulls/<N> -f base=main`
  (`gh pr edit --base` currently aborts on a projectCards GraphQL deprecation) —
  then delete the base branch.
- PR checks build a synthetic merge of head into the **base tip at trigger time**.
  A fix landed on the base needs a **freshly triggered** run to be picked up;
  `gh run rerun` replays the original merge snapshot and proves nothing. For a
  stack, ripple the fix upward with plain merge commits (base → each branch) —
  no rebase needed, and each push triggers fresh runs.
