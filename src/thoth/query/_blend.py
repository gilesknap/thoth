"""The full cost-ordered pass: structural + recall sources fused by RRF (issue #143).

:func:`_answer` is the orchestration behind :meth:`thoth.query.QueryEngine.answer`
(which documents the user-facing contract and delegates here): it overlaps the
expensive recall pass with the cheap structural one, fuses the two ranked lists by
Reciprocal Rank Fusion, and composes the answer with its harness-built citations.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from thoth.hindsight import Hindsight, HindsightError
from thoth.llm import LLM
from thoth.vault import Vault

from ._compose import _build_citation, _compose
from ._retrieval import _follow_wikilinks, _grep, _recall_paths
from ._shared import (
    _METHOD_ORDER,
    METHOD_GREP,
    METHOD_RECALL,
    METHOD_WIKILINK,
    RECALL_QA_TYPES,
    RRF_K,
    PageProvenance,
    QueryError,
    QueryResult,
    logger,
)


def _answer(
    vault: Vault,
    hindsight: Hindsight,
    llm: LLM | None,
    query: str,
    *,
    max_pages: int = 5,
    use_recall: bool = True,
    search_terms: list[str] | None = None,
) -> QueryResult:
    """Blend structural + semantic retrieval (RRF), compose an answer (issue #143)."""
    if max_pages < 1:
        raise QueryError("max_pages must be at least 1")
    # The keywords from the intent gate (issue #102) seed the lexical grep; the raw
    # query is the fallback so the pre-gate behaviour holds when none were given.
    grep_term = " ".join(search_terms) if search_terms else query

    started = time.monotonic()

    # Submit the expensive recall pass to a worker thread FIRST so its latency
    # overlaps the cheap structural pass below (issue #143 criterion D). The worker
    # is PURE: it only reads the vault (page_exists/is_inside) and returns a list,
    # mutating no shared accumulator -- all dedup/merge happens single-threaded
    # after the join. When use_recall is false no thread is spawned at all.
    recall_ms = 0.0
    recall_ran = use_recall
    recall_failed = False
    recall_paths: list[str] = []
    if use_recall:
        # If future.result() raises, the context manager __exit__ (via
        # shutdown(wait=True)) joins the worker thread before the exception leaves
        # this block, so the pool is never leaked on the error path.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _recall_paths,
                hindsight,
                vault,
                query,
                limit=max_pages * 2,
                types=RECALL_QA_TYPES,
            )
            structural, grep_hits = _structural_paths(
                vault, grep_term, max_pages=max_pages
            )
            # Time spent WAITING on recall (grep already ran concurrently above), so
            # the logged figure is recall's marginal wall-clock contribution (C/D).
            recall_started = time.monotonic()
            try:
                recall_paths = future.result()
            except HindsightError:
                # A recall failure (daemon down, CLI timeout, subprocess error) is a
                # DEGRADATION, not a query failure: fall back to the structural-only
                # results rather than crashing answer(). The structural pass already
                # ran concurrently above, so its hits are intact.
                recall_failed = True
                recall_paths = []
                logger.warning(
                    "semantic recall failed; falling back to structural-only results"
                )
            recall_ms = (time.monotonic() - recall_started) * 1000
    else:
        structural, grep_hits = _structural_paths(vault, grep_term, max_pages=max_pages)

    # Merge the two ranked sources by Reciprocal Rank Fusion (issue #143). The merge
    # is single-threaded: by here both lists are fully materialised and confined.
    ordered, methods = _fuse(
        structural, recall_paths, grep_hits=grep_hits, max_pages=max_pages
    )

    if not ordered:
        raise QueryError(f"no vault page found for query: {query!r}")

    consulted = [_build_citation(vault, path) for path in ordered]
    provenance = [
        PageProvenance(path=path, methods=methods[path], rank=rank)
        for rank, path in enumerate(ordered, start=1)
    ]
    answer, used = _compose(vault, llm, query, consulted)
    # Recall "contributed" only if a recall-surfaced page is in the *used* subset
    # (a consulted-but-unused recall page no longer counts as recall having helped).
    used_recall = any(METHOD_RECALL in methods[c.path] for c in used)
    # Concise operator-readable success line (issue #52): grep-friendly "query
    # answered:" with the consulted/cited counts, whether the recall pass helped,
    # and the wall-clock duration so the happy path is no longer silent. UNCHANGED.
    logger.info(
        "query answered: consulted=%d cited=%d recall=%s in %.0fms",
        len(consulted),
        len(used),
        used_recall,
        (time.monotonic() - started) * 1000,
    )
    # DEBUG-only blend breakdown (issue #143 criterion C/D): per-page method
    # attribution + the semantic pass's marginal wall-clock, guarded so the happy
    # INFO path stays quiet and pays no formatting cost.
    if logger.isEnabledFor(logging.DEBUG):
        lines = [f"  #{p.rank} {p.path} via {','.join(p.methods)}" for p in provenance]
        if not recall_ran:
            recall_state = "skipped"
        elif recall_failed:
            recall_state = "FAILED (fell back to structural)"
        else:
            recall_state = "ran"
        logger.debug(
            "query blend: semantic recall %s (%.0fms)\n%s",
            recall_state,
            recall_ms,
            "\n".join(lines),
        )
    return QueryResult(
        answer=answer,
        citations=used,
        used_recall=used_recall,
        consulted_count=len(consulted),
        provenance=provenance,
    )


def _structural_paths(
    vault: Vault, grep_term: str, *, max_pages: int
) -> tuple[list[str], set[str]]:
    """Build the structural source: grep hits then their wikilink hops, deduped.

    Runs the two cheap, lexical passes on the calling thread and threads them into a
    single ordered list of real, confined paths (issue #143). grep over the curated
    folders comes first (it scans frontmatter, so a page's ``summary:`` gloss
    matches here -- ADR 0008), then ``[[wikilink]]`` navigation expands from those
    hits (bounded, so a giant link farm cannot blow up the pass). Each path is
    existence-checked via :meth:`~thoth.vault.Vault.page_exists` and recorded once,
    in discovery order -- the same structural ordering the pre-blend code produced,
    now isolated so RRF can fuse it with the recall source.

    Args:
        vault: The real, path-confined vault facade.
        grep_term: The (keyword-seeded) text to grep.
        max_pages: The page budget, used to bound the grep/wikilink fan-out.

    Returns:
        A ``(ordered, grep_hits)`` pair: the deduped, existence-checked structural
        paths in discovery order, and the subset that came from grep (the rest are
        wikilink hops) so the caller can attribute each path's provenance method.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    grep_hits: set[str] = set()

    def add(paths: list[str], *, from_grep: bool = False) -> None:
        for path in paths:
            if path not in seen and vault.page_exists(path):
                seen.add(path)
                ordered.append(path)
                if from_grep:
                    grep_hits.add(path)

    add(_grep(vault, grep_term, limit=max_pages * 4), from_grep=True)
    for path in list(ordered):
        if len(ordered) >= max_pages:
            break
        add(_follow_wikilinks(vault, path, limit=max_pages))
    return ordered, grep_hits


def _fuse(
    structural: list[str],
    recall: list[str],
    *,
    grep_hits: set[str],
    max_pages: int,
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Merge the structural + recall sources by Reciprocal Rank Fusion (issue #143).

    Each unique path scores ``Σ 1 / (RRF_K + rank)`` over the sources it appears in
    (``rank`` 0-based), so a page in both sources outscores one topping only a
    single source, and a strong recall-only hit (recall rank 0) still scores
    ``1 / RRF_K`` -- enough to earn a cited slot even when the structural source
    already filled the budget. Paths sort by fused score **descending**, with
    structural discovery order as a stable tie-break (a structural/grep hit leads a
    recall hit on a score tie, and an exact-token grep #1 stays #1). The top
    ``max_pages`` are returned.

    Each returned path also carries the set of methods that surfaced it, in
    :data:`_METHOD_ORDER`: a structural path is tagged :data:`METHOD_GREP` when it
    came from grep, else :data:`METHOD_WIKILINK` (the structural list is grep hits
    first, then wikilink hops -- :func:`_structural_paths`), and a recall path is
    tagged :data:`METHOD_RECALL`; a page in both carries both tags.

    Args:
        structural: The deduped structural paths in discovery order (grep hits, then
            wikilink hops).
        recall: The existence-filtered recall paths in recall-rank order.
        grep_hits: The subset of ``structural`` that came from grep (the rest are
            wikilink hops), used to tag each structural path's provenance method.
        max_pages: The cap on the returned cited set.

    Returns:
        A ``(ordered_paths, methods)`` pair: the fused, capped path list in final
        rank order, and a path -> methods-tuple map covering those paths.
    """
    method_sets: defaultdict[str, set[str]] = defaultdict(set)
    scores: defaultdict[str, float] = defaultdict(float)
    order_index: dict[str, int] = {}

    # Structural discovery order is the stable tie-break key; record it first so a
    # recall-only path (absent from structural) sorts AFTER any structural path with
    # the same fused score (structural leads on a tie). Tag each structural path
    # grep vs wikilink from the caller's grep set.
    for rank, path in enumerate(structural):
        order_index[path] = rank
        scores[path] += 1.0 / (RRF_K + rank)
        tag = METHOD_GREP if path in grep_hits else METHOD_WIKILINK
        method_sets[path].add(tag)
    next_index = len(structural)
    for rank, path in enumerate(recall):
        if path not in order_index:
            order_index[path] = next_index
            next_index += 1
        scores[path] += 1.0 / (RRF_K + rank)
        method_sets[path].add(METHOD_RECALL)

    # Sort by descending fused score, with structural-then-recall discovery order
    # as the tie-break key.
    ordered = sorted(order_index, key=lambda p: (-scores[p], order_index[p]))[
        :max_pages
    ]
    methods = {
        path: tuple(m for m in _METHOD_ORDER if m in method_sets[path])
        for path in ordered
    }
    return ordered, methods
