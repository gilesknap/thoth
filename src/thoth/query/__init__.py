"""Cost-ordered, vault-only retrieval with harness-built (unfabricable) citations.

This is the read side of the appliance (SPEC section 7). A query is answered by blending
two retrieval *sources* and letting both vote on the cited set (issue #143):

1. a STRUCTURAL source: a lexical grep over the curated knowledge folders
   (:meth:`QueryEngine.grep`), followed by link-graph navigation from the pages it found
   (:meth:`QueryEngine.follow_links`). grep scans the whole file including frontmatter,
   so a reference page's one-line ``summary:`` gloss (issue #72, ADR 0008) matches here,
   transparently absorbing what the old ``index.md`` catalog pass used to do.
2. a RECALL source: semantic recall through Hindsight
   (:meth:`QueryEngine.recall_paths`). Recall is the expensive, subprocess-backed pass,
   so it runs **concurrently** with the cheap structural pass, its latency overlapping
   grep rather than serialising after it, and it ALWAYS gets a vote when ``use_recall``
   is true, with no "only when results are thin" gate.

The two ranked source lists merge by **Reciprocal Rank Fusion** (RRF, see
:data:`RRF_K`). Each unique path scores ``Σ 1 / (RRF_K + rank)`` over the sources it
appears in, paths sort by that fused score with structural order breaking ties so a
structural hit leads a recall hit, and the top ``max_pages`` become the cited set. A
strong recall-only hit therefore earns a slot even when grep already filled the page
budget, a page found by both sources floats to the top, and empty or stale recall
collapses to pure structural order. Each cited page also carries its retrieval
*provenance*, the set of methods that surfaced it, :data:`METHOD_GREP`,
:data:`METHOD_WIKILINK` or :data:`METHOD_RECALL`, exposed on :class:`QueryResult` and
logged at ``DEBUG``.

The composed prose is optional, since an injected :class:`~thoth.llm.LLM` may write it
and otherwise a deterministic excerpt of the top page is used. But **the harness always
builds the citation block, never the model**: every cited page is run back through
:meth:`~thoth.vault.Vault.resolve` for path confinement and
:meth:`~thoth.vault.Vault.obsidian_uri`, so a citation cannot point outside the vault
and its ``obsidian://`` link cannot be fabricated (SPEC section 3 and the Appendix
"Retrieval & obsidian links").

Module level imports only the standard library and ``thoth.*``, which transitively pulls
in ``python-frontmatter`` and ``pyyaml`` through :mod:`thoth.vault`. Importing this
package is therefore always CI-safe, needing neither ``anthropic`` nor ``hindsight`` at
import time, since the injected collaborators carry those lazily.
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
