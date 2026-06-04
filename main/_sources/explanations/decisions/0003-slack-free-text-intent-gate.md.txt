# 3. Slack free-text intent gate (route prose to the right engine)

Date: 2026-05-31

## Status

Accepted

## Context

The Slack surface (`thoth.slack_app.Handlers.handle_message`) routes a message with a
deterministic `if/elif` ladder: a pending "save?" affirmative, a
`capture:`/`note:`/`save:` prefix, a bare URL, a shared file — and historically
*everything else* (bare free text) defaulted to a vault query.

The consequence was a sharp UX edge: any free text that was not a URL/file/prefixed
note was treated as a *query*. Typing "remind me to call the dentist tomorrow" as
plain prose got *searched*, not *filed*; to capture a free thought you had to prefix it.

Two options were considered:

- **Option A — fold a `capture` tool into the query path.** Rejected: it would break the
  deliberate engine separation. Ingest does git commit/rebase/push, LLM classification,
  and validated vault writes; the query path is **read-only**. Giving the query model
  write access (or blending the two engines) widens the surface and breaks
  least-privilege (SPEC §3, §12).
- **Option B — an intent gate at the routing site.** A cheap classifier that only
  *chooses* an engine, never blends them.

This decision is **Slack-only**. MCP already exposes explicit tools (`pkm_ingest`,
`pkm_search`, `pkm_todos`, …), so the calling agent does its own dispatch and needs no
gate.

## Decision

Add a `thoth.intent.IntentClassifier` collaborator, injected onto `Handlers` alongside
`ingestor` / `query_engine`. It is consulted **only** for bare free text
that hits none of the deterministic short-circuits — those run first and unchanged, and
the prefixes remain the explicit escape hatch when the model guesses wrong.

- One cheap model call (a Haiku, `DEFAULT_INTENT_MODEL`, overridable without a redeploy
  via `THOTH_INTENT_MODEL`) returns `{intent: capture | query, confidence}`.
- The classifier is **total**: any model/network/parse failure returns the safe default
  (route to query) rather than raising.
- **Low confidence falls back to query.** Searching a misfiled note is harmless; silently
  filing a real question as a note is the annoying failure, so the gate defaults to query
  whenever it is unsure (`IntentDecision.route` collapses a `low` verdict to `query`).
- A gate-routed *capture* confirmation carries a one-line recoverable hint
  ("filed as a note — send it again as a question if you meant to ask").

The gate only routes; the ingest and query engines are untouched, and the read-only query
path never gains write access.

## Consequences

- Plain prose like a reminder or a stray fact is now *filed* instead of *answered*,
  without requiring a `note:` prefix.
- One extra small model call is made **per bare free-text message only** — prefixed /
  URL / file messages skip the gate. It reuses the cached persona prefix, so the
  marginal cost is small.
- The classifier is an injectable seam with fakes, so routing is unit-tested with no
  live model or socket; the deterministic short-circuits keep their existing tests.
- A new failure mode (the model misclassifies) is bounded: the prefixes override it, the
  capture hint makes a misfile recoverable in one reply, and low confidence always
  defaults to the harmless query.
