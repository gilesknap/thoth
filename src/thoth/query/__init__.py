"""Cost-ordered, vault-only retrieval with harness-built (unfabricable) citations.

This is the read side of the appliance (SPEC section 7). A query is answered by blending
two retrieval *sources* and letting both vote on the cited set (issue #143):

1. a STRUCTURAL source -- a lexical scan (grep) over the curated knowledge folders
   (:meth:`QueryEngine.grep`) followed by ``[[wikilink]]`` graph navigation from the
   pages it found (:meth:`QueryEngine.follow_wikilinks`). grep scans the whole file
   including frontmatter, so a reference page's one-line ``summary:`` gloss (issue #72 /
   ADR 0008) is matched here -- transparently absorbing what the old ``index.md``
   catalog pass used to do.
2. a RECALL source -- semantic recall via Hindsight
   (:meth:`QueryEngine.recall_paths`). Recall is the expensive, subprocess-backed pass,
   so it is run **concurrently** with the cheap structural pass (its latency overlaps
   grep rather than serialising after it) and it ALWAYS gets a vote when ``use_recall``
   is true -- no "only when results are thin" gate.

The two ranked source lists are merged by **Reciprocal Rank Fusion** (RRF, see
:data:`RRF_K`): each unique path scores ``Σ 1 / (RRF_K + rank)`` over the sources it
appears in, paths sort by that fused score (structural order breaking ties so a
structural hit leads a recall hit on a tie), and the top ``max_pages`` become the cited
set. So a strong recall-only hit earns a slot even when grep already filled the page
budget, a page found by both sources floats to the top, and empty/stale recall collapses
to pure structural order. Each cited page also carries its retrieval *provenance* -- the
set of methods (:data:`METHOD_GREP` / :data:`METHOD_WIKILINK` / :data:`METHOD_RECALL`)
that surfaced it -- exposed on :class:`QueryResult` and logged at ``DEBUG``.

The composed prose is optional (an injected :class:`~thoth.llm.LLM` may write it,
otherwise a deterministic excerpt of the top page is used), but **the citation block is
always built by the harness, never by the model**: every cited page is run back through
:meth:`~thoth.vault.Vault.resolve` (path confinement) and
:meth:`~thoth.vault.Vault.obsidian_uri`, so a citation cannot point outside the vault
and its ``obsidian://`` link cannot be fabricated (SPEC section 3 and the Appendix
"Retrieval & obsidian links").

Only the standard library plus ``thoth.*`` (which transitively pulls in
``python-frontmatter``/``pyyaml`` via :mod:`thoth.vault`) is imported at module level,
so importing this package is always CI-safe -- no ``anthropic``/``hindsight`` package is
needed at import time (the injected collaborators carry those lazily).
"""

from ._engine import QueryEngine
from ._shared import (
    METHOD_GREP,
    METHOD_RECALL,
    METHOD_WIKILINK,
    RRF_K,
    SEARCHED_DIRS,
    Citation,
    PageProvenance,
    QueryError,
    QueryResult,
)
from ._shared import (
    RECALL_QA_TYPES as RECALL_QA_TYPES,
)

__all__ = [
    "METHOD_GREP",
    "METHOD_RECALL",
    "METHOD_WIKILINK",
    "RRF_K",
    "SEARCHED_DIRS",
    "Citation",
    "PageProvenance",
    "QueryEngine",
    "QueryError",
    "QueryResult",
]
