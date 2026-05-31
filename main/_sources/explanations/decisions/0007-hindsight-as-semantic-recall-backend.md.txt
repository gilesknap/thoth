# 7. Retain Hindsight as the semantic recall backend; use LLM comparison for structural analysis

Date: 2026-05-31

## Status

Accepted

## Context

Thoth uses Hindsight (`hindsight.vectorize.io` CLI, self-hosted) as its sole semantic
memory backend. The wrapper (`thoth.hindsight`) shells out to the `hindsight` binary,
which stores facts in a local Postgres instance with a vector extension. The only
external network call is to the configured LLM provider for fact-extraction (the
embedding/extraction step); no data is sent to a Vectorize.io cloud service.

The Hindsight CLI exposes two checked operations: `memory retain` (write) and
`memory recall` (semantic query). It does **not** expose raw embedding vectors,
cosine-similarity access, or any clustering primitive.

Two open issues require operations that Hindsight cannot satisfy directly:

- **#38 (semantic near-duplicate detection):** finding page pairs above a
  cosine-similarity threshold requires either direct vector access (e.g. iterating
  all embeddings and computing pairwise similarity) or a dedicated similarity-search
  API. Hindsight's recall is query-driven — you ask a question, it returns hits — so
  there is no way to ask "give me all page vectors" or "score these two pages against
  each other".
- **#37 (idea-mining):** discovering latent themes across a corpus of loose pages would
  benefit from embedding-based clustering. Hindsight cannot enumerate all embeddings or
  return a distance matrix.

Alternatives considered:

1. **Keep Hindsight; use LLM comparison for #37/#38.** Feed candidate page
   summaries/content to a Claude call to judge similarity or surface themes. No new
   dependency. Token cost is higher than vector math but both features are offline/async
   (lint and cron), so latency is acceptable.
2. **Add a local vector store alongside Hindsight** (LanceDB, ChromaDB). Gains raw
   embedding access and clustering; adds a second index to maintain, duplicating the
   retain path and doubling reindex complexity.
3. **Replace Hindsight with a local vector store.** Eliminates LLM fact-extraction
   (Hindsight's main quality advantage over naive chunking) and requires thoth to own
   the chunking/extraction problem. Significant regression risk for recall quality.
4. **Replace Hindsight with mem0 or a managed embedding service.** Trades one managed
   service for another; still exposes no raw vector API.

## Decision

**Keep Hindsight as the sole semantic recall backend.** Implement #37 (idea-mining) and
#38 (near-duplicate detection) using LLM-based comparison rather than vector math:

- **#38:** feed pairs of page summaries/frontmatter to a model call to judge whether
  they represent the same entity. The lint pass is offline so per-pair token cost is
  acceptable.
- **#37:** summarise recent `raw/`/`memories` pages into a prompt and ask the model to
  identify recurring themes. The weekly cron cadence means throughput is not a concern.

If raw embedding access becomes a hard requirement for a future feature, **LanceDB** is
the preferred replacement: pure Python library, no server, local files, supports cosine
similarity and raw vector iteration. That decision is deferred until a concrete use case
cannot be satisfied by LLM comparison.

**Implementation deferred (2026-05-31).** This ADR settles *how* #37 and #38 would be
built (LLM comparison on Hindsight); it does not commit to building them now. There is no
strong present need, so both issues are closed as won't-fix **for now** (`wontfix`) rather
than scheduled. A future Hindsight → LanceDB conversion is the natural point to revisit
them; until a felt need or that migration arises, neither is built.

## Consequences

- No new infrastructure, no second index to maintain, no additional dependency.
- #37 and #38 are implementable on top of existing page-scanning and LLM infrastructure
  already present in `summary.py` and `lint.py`.
- Token cost per lint run and per idea-mining digest is higher than pure vector math;
  this is acceptable given both are async/background operations.
- The decision to keep Hindsight relies on its self-hosted, local-Postgres deployment.
  If a future deployment must use Hindsight Cloud (external service), the privacy and
  data-residency trade-off should be re-evaluated.
- Hindsight's LLM fact-extraction provider is configurable (Anthropic, OpenAI, Gemini,
  Ollama, etc.); thoth does not prescribe which to use.
- The existing `forget` limitation (no confirmed per-path CLI verb; best-effort only)
  remains; the authoritative reset is a full `reindex --full-rebuild` as documented in
  SPEC §8.
