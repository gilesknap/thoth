# 12. Blend grep and semantic retrieval with Reciprocal Rank Fusion

Date: 2026-06-04

## Status

Accepted

## Context

`query.py` powers `pkm_search`. Given a query, it selects which vault pages to cite, and it has two retrieval sources available:

- **Structural**, a lexical grep over the curated folders plus wikilink traversal. It is cheap and exact, good at literal token matches and at a page's `summary:` gloss, and blind to synonyms and paraphrase.
- **Semantic**, meaning Hindsight recall over an embedding and fact-extraction index. It is good at "about the same thing in different words", and on this vault it is low-resolution, because it leans toward the bank's dominant content cluster.

The earlier design ran them cost-ordered: grep first, and recall only as a fallback when structural results looked thin.

In practice that gate suppressed recall exactly when it was most useful. A dense, on-topic query already had "enough" grep hits, so the grep-complementary pages recall would have added, meaning relevant notes that simply do not share the query's literal tokens, were never consulted.

The two sources are complementary rather than a primary-plus-backup pair, so gating one behind the other's thinness left good pages uncited.

## Decision

**Always run both sources and merge their ranked lists with Reciprocal Rank Fusion (RRF).** Recall is no longer a fallback, and always gets a vote when it is enabled.

- **Concurrency.** Recall is the slow source, so it runs in a worker thread while the cheap structural pass runs on the calling thread. The semantic latency overlaps grep rather than serialising after it.
- **Fusion.** Each unique page scores `Σ 1 / (RRF_K + rank)` over the sources that surfaced it, with `RRF_K = 60`, the standard Cormack, Clarke and Buettcher damping constant. Pages sort by that fused score, and structural discovery order breaks ties, so a structural hit leads a recall hit on a score tie. The top `max_pages` are cited. A recall-only hit at rank 0 still scores `1 / 60`, which is enough to earn a slot even when structural already filled `max_pages`, and that is the whole point.
- **Provenance.** Each cited page records which methods surfaced it, from `grep`, `wikilink` and `recall`, and its fused rank. That is exposed as `provenance` on the `pkm_search` result and logged at DEBUG.
- **Graceful degradation.** A Hindsight failure logs a warning and collapses to structural-only order rather than failing the query, and `use_recall=False` skips the semantic pass and its worker entirely.

A related but separate knob, `search_keywords` (issue #139), lets the calling model seed the whole-word grep with de-pluralised or synonym terms, since grep matches whole words and a plural query otherwise misses singular page content.

## Consequences

- **Recall complements grep instead of backstopping it.** On dense, on-topic queries the blend cites grep-missed-but-relevant pages it previously skipped, which is a measured win on the owner's dense work domains.
- **Sparse and off-topic queries stay honest.** When recall only returns the dominant cluster as noise, those hits land at tail rank and the structural pages still lead, because RRF damping keeps a weak single-source hit from outranking a strong one.
- **Verifying retrieval needs care.** Because recall is low-resolution, judging the blend on a sparse worst-case query reads as "no gain", so probe a dense domain and read `provenance` to see each method's contribution. This nearly got the blend reverted off the live appliance, where the regression turned out to be a client reading vault files directly rather than the blend. The verification methodology lives in the `thoth-testing` skill.
- **No new dependencies.** RRF is a few lines of arithmetic, and recall already shelled out to Hindsight. The cost is one always-on recall call per query, overlapped, and still inside the daily budget guard.
