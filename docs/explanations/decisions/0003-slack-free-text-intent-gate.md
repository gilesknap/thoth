# 3. Slack free-text intent gate (route prose to the right engine)

Date: 2026-05-31

## Status

Accepted

## Context

The Slack surface (`thoth.slack_app.Handlers.handle_message`) routes a message with a deterministic `if/elif` ladder: a pending "save?" affirmative, a `capture:`, `note:` or `save:` prefix, a bare URL, a shared file. Historically *everything else*, meaning bare free text, defaulted to a vault query.

The consequence was a sharp UX edge. Any free text that was not a URL, a file or a prefixed note was treated as a *query*, so typing "remind me to call the dentist tomorrow" as plain prose got searched rather than filed. To capture a free thought you had to prefix it.

We considered two options:

- **Option A, fold a `capture` tool into the query path.** Rejected, because it would break the deliberate engine separation. Ingest does git commit, rebase and push, LLM classification and validated vault writes, while the query path is read-only. Giving the query model write access, or blending the two engines, widens the surface and breaks least-privilege (SPEC sections 3 and 12).
- **Option B, an intent gate at the routing site.** A cheap classifier that only *chooses* an engine and never blends them.

This decision is Slack-only. MCP already exposes explicit tools such as `pkm_ingest`, `pkm_search` and `pkm_todos`, so the calling agent does its own dispatch and needs no gate.

## Decision

Add a `thoth.intent.IntentClassifier` collaborator, injected onto `Handlers` alongside `ingestor` and `query_engine`.

It is consulted only for bare free text that hits none of the deterministic short-circuits. Those run first and unchanged, and the prefixes remain the explicit escape hatch when the model guesses wrong.

- One cheap model call, a Haiku named by `DEFAULT_INTENT_MODEL` and overridable without a redeploy via `THOTH_INTENT_MODEL`, returns `{intent: capture | query, confidence}`.
- The classifier is total. Any model, network or parse failure returns the safe default of routing to query rather than raising.
- Low confidence falls back to query. Searching a misfiled note is harmless, while silently filing a real question as a note is the annoying failure, so the gate defaults to query whenever it is unsure and `IntentDecision.route` collapses a `low` verdict to `query`.
- A gate-routed *capture* confirmation carries a one-line recoverable hint, telling you it was filed as a note and to send it again as a question if you meant to ask.

The gate only routes. The ingest and query engines are untouched, and the read-only query path never gains write access.

## Consequences

- Plain prose like a reminder or a stray fact is now filed instead of answered, without requiring a `note:` prefix.
- One extra small model call is made per bare free-text message only, because prefixed, URL and file messages skip the gate. It reuses the cached persona prefix, so the marginal cost is small.
- The classifier is an injectable seam with fakes, so routing is unit-tested with no live model and no socket, and the deterministic short-circuits keep their existing tests.
- The new failure mode, where the model misclassifies, is bounded. The prefixes override it, the capture hint makes a misfile recoverable in one reply, and low confidence always defaults to the harmless query.
