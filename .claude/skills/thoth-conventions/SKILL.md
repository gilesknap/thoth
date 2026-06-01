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
