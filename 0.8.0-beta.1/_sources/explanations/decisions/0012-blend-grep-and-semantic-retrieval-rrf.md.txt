# 12. Blend grep and semantic retrieval with Reciprocal Rank Fusion

Date: 2026-06-04

## Status

Accepted

## Context

`query.py` powers `pkm_search`: given a query, it
selects which vault pages to cite. It has two retrieval sources available:

- **Structural** — a lexical grep over the curated folders plus wikilink traversal.
  Cheap, exact, and good at literal token matches and a page's `summary:` gloss; blind to
  synonyms and paraphrase.
- **Semantic** — Hindsight recall (an embedding/fact-extraction index). Good at "about
  the same thing in different words"; on this vault it is **low-resolution** (it leans
  toward the bank's dominant content cluster).

The earlier design ran them **cost-ordered**: grep first, and recall only as a fallback
*when structural results looked thin*. In practice that gate suppressed recall exactly
when it was most useful — a dense, on-topic query already had "enough" grep hits, so the
grep-complementary pages recall would have added (relevant notes that simply don't share
the query's literal tokens) were never consulted. The two sources are **complementary**,
not a primary-plus-backup pair, so gating one behind the other's thinness left good pages
uncited.

## Decision

**Always run both sources and merge their ranked lists with Reciprocal Rank Fusion
(RRF).** Recall is no longer a fallback — it **always gets a vote** when enabled.

- **Concurrency.** Recall is the slow source, so it runs in a worker thread while the
  cheap structural pass runs on the calling thread; the semantic latency overlaps grep
  rather than serialising after it.
- **Fusion.** Each unique page scores `Σ 1 / (RRF_K + rank)` over the sources that
  surfaced it, with `RRF_K = 60` (the standard Cormack/Clarke/Buettcher damping constant).
  Pages sort by that fused score; structural discovery order breaks ties (a structural hit
  leads a recall hit on a score tie). The top `max_pages` are cited. A recall-only hit at
  rank 0 still scores `1 / 60` — enough to earn a slot even when structural already filled
  `max_pages`, which is the whole point.
- **Provenance.** Each cited page records *which* method(s) surfaced it (`grep` /
  `wikilink` / `recall`) and its fused rank, exposed as `provenance` on the `pkm_search`
  result and logged at DEBUG.
- **Graceful degradation.** A Hindsight failure logs a warning and collapses to
  structural-only order rather than failing the query; `use_recall=False` skips the
  semantic pass (and its worker) entirely.

A related, separate knob — `search_keywords` (issue #139) — lets the calling model seed
the whole-word grep with de-pluralised / synonym terms, since grep matches whole words and
a plural query otherwise misses singular page content.

## Consequences

- **Recall complements grep instead of backstopping it.** On dense, on-topic queries the
  blend cites grep-missed-but-relevant pages it previously skipped (a measured win on the
  owner's dense work domains).
- **Sparse/off-topic queries stay honest.** When recall only returns the dominant cluster
  as noise, those hits land at tail rank and the structural pages still lead — RRF damping
  keeps a weak single-source hit from outranking a strong one.
- **Verifying retrieval needs care.** Because recall is low-resolution, judging the blend
  on a sparse worst-case query reads as "no gain"; probe a dense domain and read
  `provenance` to see each method's contribution. (This nearly got the blend reverted off
  the live appliance — the regression was a client reading vault files directly, not the
  blend. The verification methodology lives in the `thoth-testing` skill.)
- **No new dependencies.** RRF is a few lines of arithmetic; recall already shelled out to
  Hindsight. The cost is one always-on recall call per query (overlapped, and still inside
  the daily budget guard).
