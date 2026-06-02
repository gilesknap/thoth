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
**force it through Anthropic tool-use** rather than asking for JSON as free text and
parsing it. Define a tool whose `input_schema` mirrors the contract, set
`tool_choice={"type": "tool", "name": "<tool>"}`, and read the structured
`tool_use.input` dict (the SDK/transport handles all escaping). Keep the existing
validator as the post-call gate — tool-use guarantees *valid JSON*, not a *valid
plan*. Curate's `submit_file_plan` does this (issue #110); `research.py` is the
in-repo precedent for the loop (`_tool_use_blocks` / `_block_id` /
`tool_result_block` / `assistant_blocks_message`, mostly promoted to `llm.py`).

This was a real fix, not a stylistic one: hand-serialized JSON aborted with
"Unterminated string" on bodies containing newlines, tabs, or non-breaking spaces
(U+00A0) — ~39% of a real import — because the model emitted raw control characters
inside the JSON string. Tool-use eliminates that whole failure class.

**Foot-gun — the repair/retry turn after a tool call.** When you re-ask the model
after a rejected tool call (e.g. the plan failed validation), the follow-up **user
turn must lead with a `tool_result` block keyed to the prior `tool_use` id** — a
plain-text user turn there makes the *live* Messages API reject the request with
HTTP 400 ("tool_use ids were found without tool_result blocks immediately after").
Mirror `research.py`: echo the assistant's `tool_use` turn, then a user turn whose
first block is `tool_result_block(<tool_use_id>, <repair_text>, is_error=True)`.
Only the no-tool-call branch (model declined the tool) takes a plain-text follow-up.
Injected fakes ignore this precondition, so this bug passes CI and only shows up
live — see the `thoth-testing` skill for how to test the repair path against the
real API.

## Clean slate — no backward-compat or migration prose

Per `CLAUDE.md`: this is a personal, single-user project in an early, iterative
phase. There are no other deployments to preserve — config, vault, and the Slack
app are re-created from scratch when needed. So **do not** write migration guides,
compatibility shims, or "if you previously did X, do Y" prose. When something
changes, document only the new way and assume a clean slate. The vault itself is
disposable test data during this phase — never write backfill/migration code for
existing vault content.

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
