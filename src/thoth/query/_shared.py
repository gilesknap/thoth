"""Shared types, constants, and the package logger of the query passes.

The result/citation dataclasses, the error, the retrieval-method vocabulary, and the
tuning constants live here so the pass submodules of :mod:`thoth.query` stay cycle-free.
Only the standard library plus ``thoth.vault`` is imported, preserving the package's
import-purity contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from thoth.vault import REFERENCE_TYPES

logger = logging.getLogger("thoth.query")

# Lexical retrieval spans the reference folders plus actions/, so a filed action page
# is reachable from knowledge Q&A (issue #106). raw/ stays excluded because raw sources
# are reached through their owning page's wikilinks rather than scanned
SEARCHED_DIRS: tuple[str, ...] = ("entities", "notes", "memories", "actions")
"""Top-level vault folders scanned by :meth:`QueryEngine.grep` (reference + actions)."""

# The reference types plus action, so a filed action page can surface as a recall hit
# on the knowledge-Q&A path (issue #106). recall_paths' own default stays
# REFERENCE_TYPES, so the dashboard path and any explicit-typed caller keep their scope
RECALL_QA_TYPES: frozenset[str] = REFERENCE_TYPES | frozenset({"action"})
"""Page-type scope for the knowledge-Q&A recall pass (reference types + ``action``)."""

# Retrieval-method tags carried in a page's provenance (issue #143). A page can be
# surfaced by more than one method, so provenance holds the set that produced it
METHOD_GREP: str = "grep"
"""Provenance tag: the page was surfaced by the lexical grep pass."""
METHOD_WIKILINK: str = "wikilink"
"""Provenance tag: the page was surfaced by ``[[wikilink]]`` graph navigation."""
METHOD_RECALL: str = "recall"
"""Provenance tag: the page was surfaced by the semantic Hindsight recall pass."""

# Cheapest-discovered first, so a page's methods tuple reads the same however the set
# happens to iterate
_METHOD_ORDER: tuple[str, ...] = (METHOD_GREP, METHOD_WIKILINK, METHOD_RECALL)

# RRF fuses ranked lists by scoring each item as the sum of 1 / (RRF_K + rank) over the
# lists it appears in. 60 is the constant from the original Cormack, Clarke and
# Buettcher paper, and it keeps the gap between adjacent ranks gentle, so a page in both
# sources reliably outscores one that tops a single source. A recall-only hit at rank 0
# still scores 1 / RRF_K, enough for a cited slot when grep already filled max_pages
RRF_K: int = 60
"""Reciprocal Rank Fusion damping constant, the standard 60 (issue #143)."""

# Cap on bytes read per page so a pathological file cannot blow up a grep scan
_MAX_GREP_BYTES: int = 1_000_000

# A token hitting the filename, title or summary gloss outscores one hitting only the
# body, so at the same token count a title match ranks higher (issue #96). The ranking
# key is (distinct tokens matched, weight sum), so these weights only break ties within
# a token-count tier and more matched words always wins
_HIGH_WEIGHT: int = 2
_LOW_WEIGHT: int = 1

# Excerpt length for the deterministic answer fallback, used when no LLM is injected
_EXCERPT_CHARS: int = 600


class QueryError(Exception):
    """Raised when a query cannot be answered (for example no vault pages match)."""


@dataclass(frozen=True, slots=True)
class Citation:
    """Harness-built, unfabricable handle for one cited vault page.

    Every field derives from a real, path-confined vault page: ``path`` has passed
    :meth:`~thoth.vault.Vault.resolve`, ``obsidian_uri`` comes from
    :meth:`~thoth.vault.Vault.obsidian_uri`, ``wikilink`` comes from the page's actual
    filename, and ``snippet`` is the page's own ``summary:`` gloss (ADR 0008) when it
    carries one. The model never supplies any of these.
    """

    path: str
    """The vault-relative, confined path of the cited page (e.g. ``entities/x.md``)."""
    title: str
    """The page's human-readable title (from frontmatter, else the slug)."""
    obsidian_uri: str
    """The canonical ``obsidian://open`` deep link from :meth:`Vault.obsidian_uri`."""
    wikilink: str
    """The ``[[<slug>]]`` link derived from the real filename stem."""
    snippet: str = ""
    """The page's one-line ``summary:`` frontmatter gloss (``""`` when it has none)."""


@dataclass(frozen=True, slots=True)
class PageProvenance:
    """How one cited page was surfaced: its methods and final rank (issue #143).

    A page can be produced by more than one retrieval source, so ``methods`` is the full
    set of tags that surfaced it, reported in the fixed :data:`_METHOD_ORDER`. ``rank``
    is the page's 1-based position in the consulted set after the RRF blend, so a list
    of these reads as the final retrieval order with its attribution attached.
    """

    path: str
    """The vault-relative, confined path of the cited page (e.g. ``entities/x.md``)."""
    methods: tuple[str, ...]
    """The retrieval methods that surfaced this page, in :data:`_METHOD_ORDER`."""
    rank: int
    """The page's 1-based rank in the cited (consulted) set after the blend."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """A composed answer with its harness-attached citations and provenance.

    ``citations`` is the used subset. When an LLM composes the prose it ends its reply
    with a ``USED: 1, 3`` line naming the candidates that directly supported the answer,
    and only those are kept (issue #34), so the Slack ``Sources:`` list reflects what
    the answer drew on rather than the whole candidate set. A missing or garbled
    ``USED:`` line falls back to keeping every consulted page, and the deterministic
    path keeps its single top page.

    ``provenance`` records, for every consulted page in final rank order, which
    retrieval methods surfaced it (issue #143). The structural and semantic sources are
    blended by Reciprocal Rank Fusion, so a page may carry more than one method, and the
    consulted set this covers may be a superset of the used ``citations``.

    ``consulted_count`` is how many candidates were offered to the model before the
    ``USED:`` filter, so an operator log can compare consulted against used (issue #52).
    ``used_recall`` is True when a recall-surfaced page lands in the used subset,
    recording whether the more expensive Hindsight pass contributed.
    """

    answer: str
    """The composed prose answer (LLM-written when an LLM is injected, else excerpt)."""
    citations: list[Citation] = field(default_factory=list)
    """The citations the answer used, in retrieval order, deduplicated by path."""
    used_recall: bool = False
    """Whether the semantic Hindsight recall pass contributed to the result."""
    consulted_count: int = 0
    """How many candidate pages were offered to the model before the ``USED`` filter."""
    provenance: list[PageProvenance] = field(default_factory=list)
    """Per consulted (cited) page, the methods that surfaced it, in final rank order."""
