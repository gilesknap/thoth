# 4. Index all content in the embedding store, scope recall by tag

Date: 2026-05-31

## Status

Accepted

## Context

The vault partitions into two domains (`thoth.vault`, SPEC §9):

- **Knowledge** — `entities/ concepts/ comparisons/ queries/` (`KNOWLEDGE_DIRS`):
  fact-bearing prose, retrieved by *semantic similarity*.
- **Life-admin** — `actions/ media/ memories/ people/` (`LIFE_ADMIN_DIRS`):
  structured pages keyed by frontmatter `type`, retrieved by *field* (due date,
  priority, status) via Obsidian Bases and the structural `summary`/`todos` scans.

The reindex (`thoth.reindex_from_vault`) embeds **only** `KNOWLEDGE_DIRS`
(`INDEXED_DIRS = KNOWLEDGE_DIRS`). Life-admin folders and `raw/` are excluded from the
semantic index entirely. This was a reasonable v1: it kept the embedding store small,
low-churn, and free of low-information templated stubs.

Two problems surfaced:

1. **It is a surprising special-case.** The exclusion of `memories/` from semantic
   recall contradicts the natural mental model that *all* content is searchable both
   semantically and structurally. It surprised the author.
2. **It forecloses valuable retrieval.** "Have I ever noted anything about X?" should
   hit `memories/` and `actions/`. Semantic search over loose personal captures is one
   of the highest-value PKM moves — and clustering loose captures (idea-mining, issue
   #37) is *exactly* a semantic-similarity task over the folders the partition excludes.

The original partition was buying two real things:

- **Recall precision.** Life-admin pages are short, templated, frontmatter-heavy stubs.
  Adding hundreds of near-identical low-information documents degrades semantic recall
  quality for knowledge queries.
- **Low churn.** Life-admin pages change state constantly (task done, due moved); each
  change would re-embed.

But Hindsight already retains every page with `tags=[page_type, rel]`. That means the
noise can be partitioned at **query time by tag** rather than at **index time by
folder** — which recovers completeness without sacrificing precision.

## Decision

**Embed all curated and life-admin content in the semantic index; scope recall by tag
according to intent.**

- The reindex walks knowledge *and* life-admin folders, retaining each page tagged with
  its `page_type` (already the tag contract).
- Recall is filtered by tag per caller intent:
  - Knowledge Q&A (`pkm_search` / `pkm_ask`) → filter to knowledge tags (preserves
    today's precision).
  - "Search my memories" / idea-mining (#37) → filter to `memory` / life-admin tags.
  - "Search everything" → no filter.
- **Churn is bounded by the existing body-hash idempotency**: a page whose body is
  unchanged is not re-embedded, so pure frontmatter/status transitions (task done, due
  moved) do not churn the index.

`raw/` remains **out of scope of this decision**. It is excluded not for noise but
because it is immutable source bytes, often long — embedding it well needs a *chunking*
strategy, and Hindsight does fact-extraction rather than chunking. "Embed `raw/`" is a
separate, larger design question tracked independently.

## Consequences

- The natural model holds: all curated and life-admin pages are searchable both
  semantically and structurally. The `memories/` special-case disappears.
- Knowledge recall precision is preserved because knowledge queries filter to knowledge
  tags; the life-admin stubs are only returned when a caller asks for them.
- Idea-mining (#37) and any future "find related captures" feature can lean on the
  shared index instead of doing an ad-hoc scan or embedding pages themselves.
- The index grows by the life-admin page count and incurs more retain calls, but the
  body-hash guard keeps churn proportional to *body* changes, not state changes.
- `INDEXED_DIRS` is no longer an alias of `KNOWLEDGE_DIRS`; recall sites must pass a tag
  filter expressing intent, so the retrieval API gains a domain-scope parameter.
- `raw/` stays excluded; revisiting it is deferred to a chunking-strategy decision.
