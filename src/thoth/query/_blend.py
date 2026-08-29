"""The full cost-ordered pass, fusing the structural and recall sources (#143).

:func:`_answer` is the orchestration behind the engine's public method, which documents
the user-facing contract and delegates here. It overlaps the expensive recall pass with
the cheap structural one, fuses the two ranked lists by Reciprocal Rank Fusion, and
composes the answer with its harness-built citations.
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
from ._retrieval import _follow_links, _grep, _recall_paths
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
    """Blends structural and semantic retrieval, then composes an answer (#143)."""
    if max_pages < 1:
        raise QueryError("max_pages must be at least 1")
    # The intent gate's keywords (issue #102) seed the lexical grep, with the raw query
    # as the fallback so the pre-gate behaviour holds when none were given
    grep_term = " ".join(search_terms) if search_terms else query

    started = time.monotonic()

    # Submit the expensive recall pass first so its latency overlaps the cheap
    # structural pass below (issue #143). The worker is pure: it only reads the vault
    # and returns a list, mutating no shared accumulator, so all merging happens
    # single-threaded after the join. With recall off, no thread is spawned at all
    recall_ms = 0.0
    recall_ran = use_recall
    recall_failed = False
    recall_paths: list[str] = []
    if use_recall:
        # If the result raises, the context manager joins the worker before the
        # exception leaves this block, so the pool is never leaked on the error path
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
            # Time spent waiting on recall, since grep already ran concurrently above,
            # so the logged figure is recall's marginal wall-clock contribution
            recall_started = time.monotonic()
            try:
                recall_paths = future.result()
            except HindsightError:
                # A recall failure is a degradation rather than a query failure, so
                # fall back to structural-only results rather than crashing. The
                # structural pass already ran concurrently, so its hits are intact
                recall_failed = True
                recall_paths = []
                logger.warning(
                    "semantic recall failed; falling back to structural-only results"
                )
            recall_ms = (time.monotonic() - recall_started) * 1000
    else:
        structural, grep_hits = _structural_paths(vault, grep_term, max_pages=max_pages)

    # Merge the two ranked sources by Reciprocal Rank Fusion (issue #143). The merge is
    # single-threaded, since by here both lists are materialised and confined
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
    # Recall counts as having contributed only when a recall-surfaced page reaches the
    # used subset. A consulted-but-unused recall page does not count
    used_recall = any(METHOD_RECALL in methods[c.path] for c in used)
    # Operator-readable success line (issue #52) carrying the consulted and cited
    # counts, whether recall helped, and the duration, so the happy path is not silent
    logger.info(
        "query answered: consulted=%d cited=%d recall=%s in %.0fms",
        len(consulted),
        len(used),
        used_recall,
        (time.monotonic() - started) * 1000,
    )
    # Debug-only blend breakdown (issue #143), giving per-page method attribution and
    # the semantic pass's marginal wall-clock. Guarded so the happy path stays quiet and
    # pays no formatting cost
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
    """Builds the structural source: grep hits then their link hops, deduped.

    Runs both cheap lexical passes on the calling thread into one ordered list of real,
    confined paths. Grep comes first and scans frontmatter, so a page's gloss matches
    here (ADR 0008), then link navigation expands from those hits, bounded so a giant
    link farm cannot blow up the pass. Each path is existence-checked and recorded once
    in discovery order, the same structural ordering as before the blend, now isolated
    so RRF can fuse it with recall.

    Args:
        vault: The path-confined vault facade.
        grep_term: The keyword-seeded text to grep.
        max_pages: The page budget bounding the fan-out.

    Returns:
        The deduped structural paths in discovery order, and the subset that came from
        grep so the caller can attribute each path's method.
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
        add(_follow_links(vault, path, limit=max_pages))
    return ordered, grep_hits


def _fuse(
    structural: list[str],
    recall: list[str],
    *,
    grep_hits: set[str],
    max_pages: int,
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Merges the structural and recall sources by Reciprocal Rank Fusion (#143).

    Each unique path scores ``Σ 1 / (RRF_K + rank)`` over the sources it appears in,
    with ``rank`` 0-based. So a page in both outscores one topping a single source, and
    a strong recall-only hit (recall rank 0) still scores ``1 / RRF_K``, enough to earn
    a cited slot even when the structural source filled the budget.

    Paths sort by fused score descending, with structural discovery order as a stable
    tie-break, so a grep hit leads a recall hit on a tie and an exact-token grep first
    place stays first.

    Each returned path carries the methods that surfaced it. A structural path is tagged
    grep or wikilink depending on which pass found it, a recall path is tagged recall,
    and a page in both carries both.

    Args:
        structural: Deduped structural paths in discovery order.
        recall: Existence-filtered recall paths in recall-rank order.
        grep_hits: The structural subset that came from grep, used to tag each
            structural path's method.
        max_pages: The cap on the returned cited set.

    Returns:
        The fused, capped paths in final rank order, and a map of path to methods.
    """
    method_sets: defaultdict[str, set[str]] = defaultdict(set)
    scores: defaultdict[str, float] = defaultdict(float)
    order_index: dict[str, int] = {}

    # Structural discovery order is the stable tie-break key, so record it first and a
    # recall-only path sorts after any structural path with the same fused score. Tag
    # each structural path grep or wikilink from the caller's set
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

    # Sort by descending fused score, with discovery order as the tie-break
    ordered = sorted(order_index, key=lambda p: (-scores[p], order_index[p]))[
        :max_pages
    ]
    methods = {
        path: tuple(m for m in _METHOD_ORDER if m in method_sets[path])
        for path in ordered
    }
    return ordered, methods
