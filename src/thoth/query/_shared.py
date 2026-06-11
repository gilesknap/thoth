"""Shared types, constants, and the package logger of the query passes.

The result/citation dataclasses, the error, the retrieval-method vocabulary, and the
tuning constants live here so the pass submodules of :mod:`thoth.query` stay
cycle-free. Only the standard library plus ``thoth.vault`` is imported, preserving the
package's import-purity contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from thoth.vault import REFERENCE_TYPES

logger = logging.getLogger("thoth.query")

# Folders searched for lexical/structural retrieval. Lexical retrieval spans the
# reference folders PLUS actions/, so a filed action page is reachable from knowledge
# Q&A (issue #106). raw/ stays excluded: raw sources are reached via their owning page's
# wikilinks, not scanned directly.
SEARCHED_DIRS: tuple[str, ...] = ("entities", "notes", "memories", "actions")
"""Top-level vault folders scanned by :meth:`QueryEngine.grep` (reference + actions)."""

# Page-type scope for the knowledge-Q&A semantic-recall pass (issue #106): the reference
# types plus ``action``, so a filed action page can also surface as a recall hit on the
# knowledge-Q&A path. recall_paths' own default stays REFERENCE_TYPES so the actionable
# dashboard path and any explicit-typed caller keep their existing scope.
RECALL_QA_TYPES: frozenset[str] = REFERENCE_TYPES | frozenset({"action"})
"""Page-type scope for the knowledge-Q&A recall pass (reference types + ``action``)."""

# Retrieval-method tags carried in a page's provenance (issue #143). A page can be
# surfaced by more than one method (e.g. found by grep AND recall), so provenance holds
# the *set* of methods that produced it; the order they are reported in is fixed below.
METHOD_GREP: str = "grep"
"""Provenance tag: the page was surfaced by the lexical grep pass."""
METHOD_WIKILINK: str = "wikilink"
"""Provenance tag: the page was surfaced by ``[[wikilink]]`` graph navigation."""
METHOD_RECALL: str = "recall"
"""Provenance tag: the page was surfaced by the semantic Hindsight recall pass."""

# Stable display/report order for the provenance method tags (issue #143): grep first,
# then wikilink, then recall (cheapest-discovered first), so a page's methods tuple
# reads the same regardless of the set's iteration order.
_METHOD_ORDER: tuple[str, ...] = (METHOD_GREP, METHOD_WIKILINK, METHOD_RECALL)

# The standard Reciprocal Rank Fusion damping constant (issue #143). RRF fuses several
# ranked lists by scoring each item ``Σ 1 / (RRF_K + rank)`` over the lists it appears
# in (rank 0-based); the large constant (60 is the value from the original
# Cormack/Clarke/Buettcher RRF paper) keeps the score gap between adjacent ranks gentle,
# so a page that appears in *both* sources reliably outscores one that tops only a
# single source. A recall-only hit at rank 0 still scores ``1 / RRF_K`` -- enough to
# earn a cited slot even when the structural source already filled ``max_pages``.
RRF_K: int = 60
"""Reciprocal Rank Fusion damping constant (the standard 60); see the blend in ``answer``."""  # noqa: E501

# Cap on bytes read per page during grep so a pathological file cannot blow up a scan.
_MAX_GREP_BYTES: int = 1_000_000

# grep token-match placement weights (issue #96): a token hitting the high-weight
# haystack (the filename + the page's frontmatter title/summary gloss) outscores one
# hitting only the body, so for the SAME token count a title match ranks above a
# body-only match. These weights only ever break ties *within* a token-count tier --
# the ranking key is (distinct tokens matched, placement-weight sum), so the count of
# matched words always dominates and more words beats a single better-placed one.
_HIGH_WEIGHT: int = 2
_LOW_WEIGHT: int = 1

# Excerpt length used for the deterministic (no-LLM) answer fallback.
_EXCERPT_CHARS: int = 600


class QueryError(Exception):
    """Raised when a query cannot be answered (for example no vault pages match)."""


@dataclass(frozen=True, slots=True)
class Citation:
    """Harness-built, unfabricable handle for one cited vault page.

    Every field is derived from a real, path-confined vault page: ``path`` has passed
    :meth:`~thoth.vault.Vault.resolve`, ``obsidian_uri`` comes from
    :meth:`~thoth.vault.Vault.obsidian_uri`, ``wikilink`` is derived from the page's
    actual filename, and ``snippet`` is the page's own ``summary:`` frontmatter gloss
    (issue #72 / ADR 0008) when it carries one. The model never supplies any of these.
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
    """How one cited page was surfaced: its retrieval methods + final rank (issue #143).

    A page can be produced by more than one retrieval source (e.g. grep AND recall both
    name it), so ``methods`` is the full set of tags that surfaced it, reported in the
    fixed :data:`_METHOD_ORDER` (grep, wikilink, recall). ``rank`` is the page's 1-based
    position in the cited (consulted) set after the RRF blend -- so a list of
    ``PageProvenance`` reads as the final retrieval order with its attribution attached.
    """

    path: str
    """The vault-relative, confined path of the cited page (e.g. ``entities/x.md``)."""
    methods: tuple[str, ...]
    """The retrieval methods that surfaced this page, in :data:`_METHOD_ORDER`."""
    rank: int
    """The page's 1-based rank in the cited (consulted) set after the blend."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """A composed answer plus its harness-attached citations and per-page provenance.

    ``citations`` is the **used** subset: when an LLM composes the prose it ends its
    reply with a ``USED: 1, 3`` line naming the candidate pages that directly supported
    the answer, and only those are kept (issue #34) so the Slack ``Sources:`` list
    reflects what the answer actually drew on, not the whole retrieval candidate set. A
    missing/garbled ``USED:`` line falls back to keeping every consulted page (the
    pre-#34 behaviour), and the deterministic (no-LLM) path keeps its single top page.

    ``provenance`` (issue #143) records, for **every consulted (cited) page** in final
    rank order, which retrieval methods surfaced it (a :class:`PageProvenance` per
    page). The two retrieval sources -- structural (grep + wikilinks) and semantic
    recall -- are blended by Reciprocal Rank Fusion (:data:`RRF_K`), so a page may carry
    more than one method, and provenance is the record of that blend: it covers the
    consulted set (which may be a superset of the ``USED:`` ``citations``).

    ``consulted_count`` records how many candidate pages were retrieved and offered to
    the model *before* the ``USED:`` filter, so an operator log (issue #52) can compare
    consulted-vs-used recall. ``used_recall`` records whether the (more expensive)
    Hindsight semantic pass contributed to the answer: it is ``True`` when a
    recall-surfaced page lands in the ``USED:`` subset, ``False`` otherwise.
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
