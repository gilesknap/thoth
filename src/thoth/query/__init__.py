"""Cost-ordered, vault-only retrieval with harness-built (unfabricable) citations.

This is the read side of the appliance (SPEC section 7). A query is answered by blending
two retrieval sources and letting both vote on the cited set (issue #143):

1. a STRUCTURAL source, a lexical scan over the curated knowledge folders
   (:meth:`QueryEngine.grep`) followed by link-graph navigation from the pages it found
   (:meth:`QueryEngine.follow_links`). grep scans the whole file including frontmatter,
   so a reference page's one-line ``summary:`` gloss (issue #72, ADR 0008) is matched
   here, absorbing what the old ``index.md`` catalog pass used to do.
2. a RECALL source, semantic recall via Hindsight (:meth:`QueryEngine.recall_paths`). It
   is the expensive, subprocess-backed pass, so it runs concurrently with the cheap
   structural one rather than serialising after it, and it always gets a vote when
   ``use_recall`` is true, with no "only when results are thin" gate.

The two ranked lists are merged by Reciprocal Rank Fusion (:data:`RRF_K`): each unique
path scores the sum of ``1 / (RRF_K + rank)`` over the sources it appears in, and the
top ``max_pages`` become the cited set, with structural order breaking ties. So a strong
recall-only hit earns a slot even when grep already filled the page budget, a page found
by both sources floats to the top, and empty or stale recall collapses to pure
structural order.

Each cited page carries its retrieval provenance, the set of methods
(:data:`METHOD_GREP`, :data:`METHOD_WIKILINK`, :data:`METHOD_RECALL`) that surfaced it,
on :class:`QueryResult` and in the ``DEBUG`` log.

The composed prose is optional, written by an injected :class:`~thoth.llm.LLM` or
falling back to a deterministic excerpt of the top page, but the citation block is
always built by the harness and never by the model: every cited page is run back through
:meth:`~thoth.vault.Vault.resolve` and :meth:`~thoth.vault.Vault.obsidian_uri`, so a
citation cannot point outside the vault and its ``obsidian://`` link cannot be
fabricated (SPEC section 3).

Only the standard library plus ``thoth.*`` is imported at module level, so importing
this package is always CI-safe: no ``anthropic`` or ``hindsight`` package is needed at
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
