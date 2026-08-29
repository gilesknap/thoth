# 7. Retain Hindsight as the semantic recall backend; use LLM comparison for structural analysis

Date: 2026-05-31

## Status

Accepted

## Context

Thoth uses Hindsight, the self-hosted `hindsight.vectorize.io` CLI, as its sole semantic memory backend.

The wrapper (`thoth.hindsight`) shells out to the `hindsight` binary, which stores facts in a local Postgres instance with a vector extension. The only external network call is to the configured LLM provider for fact-extraction, the embedding and extraction step, and no data is sent to a Vectorize.io cloud service.

The Hindsight CLI exposes two checked operations, `memory retain` to write and `memory recall` to query semantically. It does not expose raw embedding vectors, cosine-similarity access, or any clustering primitive.

Two open issues require operations that Hindsight cannot satisfy directly:

- **#38, semantic near-duplicate detection.** Finding page pairs above a cosine-similarity threshold requires either direct vector access, such as iterating all embeddings and computing pairwise similarity, or a dedicated similarity-search API. Hindsight's recall is query-driven, so you ask a question and it returns hits, and there is no way to ask for all page vectors or to score two pages against each other.
- **#37, idea-mining.** Discovering latent themes across a corpus of loose pages would benefit from embedding-based clustering, and Hindsight cannot enumerate all embeddings or return a distance matrix.

We considered four alternatives:

1. **Keep Hindsight and use LLM comparison for #37 and #38.** Feed candidate page summaries or content to a Claude call to judge similarity or surface themes. This adds no new dependency. Token cost is higher than vector math, but both features are offline and async, running under lint and cron, so the latency is acceptable.
2. **Add a local vector store alongside Hindsight**, such as LanceDB or ChromaDB. This gains raw embedding access and clustering, but adds a second index to maintain, duplicating the retain path and doubling reindex complexity.
3. **Replace Hindsight with a local vector store.** This eliminates LLM fact-extraction, which is Hindsight's main quality advantage over naive chunking, and requires thoth to own the chunking and extraction problem. That is a significant regression risk for recall quality.
4. **Replace Hindsight with mem0 or a managed embedding service.** This trades one managed service for another and still exposes no raw vector API.

## Decision

**Keep Hindsight as the sole semantic recall backend.** Implement #37 (idea-mining) and #38 (near-duplicate detection) using LLM-based comparison rather than vector math:

- For **#38**, feed pairs of page summaries and frontmatter to a model call to judge whether they represent the same entity. The lint pass is offline, so the per-pair token cost is acceptable.
- For **#37**, summarise recent `raw/` and `memories` pages into a prompt and ask the model to identify recurring themes. The weekly cron cadence means throughput is not a concern.

If raw embedding access becomes a hard requirement for a future feature, LanceDB is the preferred replacement, because it is a pure Python library with no server, storing local files and supporting cosine similarity and raw vector iteration. That decision is deferred until a concrete use case cannot be satisfied by LLM comparison.

Implementation is deferred as of 2026-05-31. This ADR settles *how* #37 and #38 would be built, meaning LLM comparison on Hindsight, and does not commit to building them now.

There is no strong present need, so both issues are closed as `wontfix` for now rather than scheduled. A future migration from Hindsight to LanceDB is the natural point to revisit them, and until a felt need or that migration arises, neither is built.

## Consequences

- There is no new infrastructure, no second index to maintain and no additional dependency.
- #37 and #38 are implementable on top of the existing page-scanning and LLM infrastructure already present in `summary.py` and `lint.py`.
- Token cost per lint run and per idea-mining digest is higher than pure vector math, which is acceptable given both are async background operations.
- The decision to keep Hindsight relies on its self-hosted, local-Postgres deployment. If a future deployment must use Hindsight Cloud, which is an external service, the privacy and data-residency trade-off should be re-evaluated.
- Hindsight's LLM fact-extraction provider is configurable, covering Anthropic, OpenAI, Gemini and Ollama among others, and thoth does not prescribe which to use.
- The existing `forget` limitation remains, since there is no confirmed per-path CLI verb and it is best-effort only. The authoritative reset is a full `reindex --full-rebuild`, as documented in SPEC section 8.
