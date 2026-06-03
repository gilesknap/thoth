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
so importing this module is always CI-safe -- no ``anthropic``/``hindsight`` package is
needed at import time (the injected collaborators carry those lazily).
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from thoth.config import Config
from thoth.hindsight import Hindsight, HindsightError
from thoth.llm import LLM, Message, extract_text
from thoth.vault import REFERENCE_TYPES, Vault, VaultError

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

_WIKILINK_RE: re.Pattern[str] = re.compile(r"\[\[([^\]|#]+)")
"""Capture an Obsidian ``[[wikilink]]`` target (ignoring ``|alias`` / ``#anchor``)."""

_USED_LINE_RE: re.Pattern[str] = re.compile(
    r"^USED:\s*(.*)$", re.IGNORECASE | re.MULTILINE
)
"""Match the model's trailing ``USED: 1, 3`` (or ``USED: none``) selection line."""

_USED_SELECTION_LINE_RE: re.Pattern[str] = re.compile(
    r"^USED:[ \t]*(?:none|[\d,\s]*)$", re.IGNORECASE | re.MULTILINE
)
"""Match a *pure* selection line (``USED:`` then only indices or ``none``).

Used to strip any stray selection-only lines from the displayed prose, while leaving a
legitimate prose sentence that merely *begins* with ``USED:`` (followed by words)
untouched. This guards against a misbehaving model that emits more than one ``USED:``
line: only the last one drives the citation subset, but every selection-only line is
removed so none leaks into the Slack answer.
"""

logger = logging.getLogger(__name__)

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


class QueryEngine:
    """Cost-ordered retrieval over a real vault, with Hindsight (and optionally an LLM).

    All collaborators are injected: the :class:`~thoth.vault.Vault` is real (over the
    canonical vault on disk), :class:`~thoth.hindsight.Hindsight` is the semantic recall
    seam, and the optional :class:`~thoth.llm.LLM` composes prose. The engine performs
    no network I/O itself -- recall and prose are delegated to the collaborators.
    """

    def __init__(
        self,
        config: Config,
        vault: Vault,
        hindsight: Hindsight,
        llm: LLM | None = None,
    ) -> None:
        """Store the injected collaborators.

        Args:
            config: The frozen runtime config (kept for parity with sibling modules;
                link encoding is delegated to ``vault``).
            vault: The real, path-confined vault facade.
            hindsight: The semantic recall seam (subprocess-backed in production).
            llm: An optional LLM for prose composition; when ``None`` the answer falls
                back to a deterministic excerpt of the top page.
        """
        self._config = config
        self._vault = vault
        self._hindsight = hindsight
        self._llm = llm

    # ---- the full cost-ordered pass ---------------------------------------------

    def answer(
        self,
        query: str,
        *,
        max_pages: int = 5,
        use_recall: bool = True,
        search_terms: list[str] | None = None,
    ) -> QueryResult:
        """Blend structural + semantic retrieval (RRF), compose an answer (issue #143).

        Two retrieval *sources* both vote on the cited set:

        * the STRUCTURAL source -- a grep hit list (grep scans frontmatter too, so a
          page's ``summary:`` gloss is matched there) followed by ``[[wikilink]]``
          navigation from those hits, deduped and existence-checked into one ordered
          list;
        * the RECALL source -- semantic Hindsight recall, which **always** gets a vote
          when ``use_recall`` is true (there is no "only when results are thin" gate).

        Because recall is the expensive, subprocess-backed pass, it is submitted to a
        worker thread FIRST and the cheap structural pass runs on the calling thread
        while recall is in flight, so recall's latency overlaps grep instead of
        serialising after it (``subprocess.run`` releases the GIL while it waits). The
        recall worker is pure -- it returns its path list and mutates no shared state;
        all dedup/merge happens single-threaded after the join.

        The two ranked lists are merged by **Reciprocal Rank Fusion** (:data:`RRF_K`):
        each unique path scores ``Σ 1 / (RRF_K + rank)`` over the sources it appears in,
        and paths sort by that fused score descending, structural discovery order
        breaking ties (so a structural hit leads a recall hit on a score tie). The top
        ``max_pages`` paths become the cited set. A page found by both sources floats
        up, a strong recall-only hit earns a slot even when grep already filled the
        budget, and empty recall collapses to pure structural order.

        The prose is written by the injected LLM if present, else taken as a
        deterministic excerpt of the top page; either way the citation block is
        harness-built from confined, real paths. With an LLM the result's citations are
        the **used** subset the model named on its ``USED:`` line (issue #34),
        ``consulted_count`` records how many candidates were offered before that filter,
        and ``provenance`` records the methods that surfaced each cited page.

        ``search_terms`` (issue #102) seed the lexical passes: when the Slack intent
        gate extracts de-pluralised, stop-word-stripped keywords from a natural-language
        message it passes them here, so the grep ranks on those terms instead of the raw
        prose ("list me the docs about dogs" greps ``dog``, not the noise words). The
        recall pass and the composed prose stay keyed off the original ``query`` so the
        answer still reads naturally and the citation/USED logic is unchanged; an empty
        or ``None`` ``search_terms`` falls back to grepping ``query`` verbatim (today's
        behaviour).

        Args:
            query: The natural-language query.
            max_pages: The maximum number of candidate pages to consult and cite.
            use_recall: When false, the semantic Hindsight pass is skipped entirely (no
                worker thread is spawned) -- the cheap, structural-only path.
            search_terms: Optional lexical keywords (from the intent gate) to grep
                instead of the raw ``query``; empty/``None`` greps ``query``.

        Returns:
            A :class:`QueryResult` whose citations all resolve to real vault pages.

        Raises:
            QueryError: if no vault page matches the query at all.
        """
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
                    self.recall_paths,
                    query,
                    limit=max_pages * 2,
                    types=RECALL_QA_TYPES,
                )
                structural, grep_hits = self._structural_paths(
                    grep_term, max_pages=max_pages
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
                        "semantic recall failed; falling back to structural-only "
                        "results"
                    )
                recall_ms = (time.monotonic() - recall_started) * 1000
        else:
            structural, grep_hits = self._structural_paths(
                grep_term, max_pages=max_pages
            )

        # Merge the two ranked sources by Reciprocal Rank Fusion (issue #143). The merge
        # is single-threaded: by here both lists are fully materialised and confined.
        ordered, methods = self._fuse(
            structural, recall_paths, grep_hits=grep_hits, max_pages=max_pages
        )

        if not ordered:
            raise QueryError(f"no vault page found for query: {query!r}")

        consulted = [self.build_citation(path) for path in ordered]
        provenance = [
            PageProvenance(path=path, methods=methods[path], rank=rank)
            for rank, path in enumerate(ordered, start=1)
        ]
        answer, used = self._compose(query, consulted)
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
            lines = [
                f"  #{p.rank} {p.path} via {','.join(p.methods)}" for p in provenance
            ]
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

    # ---- the blend: structural pass + RRF fusion --------------------------------

    def _structural_paths(
        self, grep_term: str, *, max_pages: int
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
                if path not in seen and self._vault.page_exists(path):
                    seen.add(path)
                    ordered.append(path)
                    if from_grep:
                        grep_hits.add(path)

        add(self.grep(grep_term, limit=max_pages * 4), from_grep=True)
        for path in list(ordered):
            if len(ordered) >= max_pages:
                break
            add(self.follow_wikilinks(path, limit=max_pages))
        return ordered, grep_hits

    def _fuse(
        self,
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
        first, then wikilink hops -- :meth:`_structural_paths`), and a recall path is
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
        method_sets: dict[str, set[str]] = {}
        scores: dict[str, float] = {}
        order_index: dict[str, int] = {}

        # Structural discovery order is the stable tie-break key; record it first so a
        # recall-only path (absent from structural) sorts AFTER any structural path with
        # the same fused score (structural leads on a tie). Tag each structural path
        # grep vs wikilink from the caller's grep set.
        for rank, path in enumerate(structural):
            order_index[path] = rank
            scores[path] = scores.get(path, 0.0) + 1.0 / (RRF_K + rank)
            tag = METHOD_GREP if path in grep_hits else METHOD_WIKILINK
            method_sets.setdefault(path, set()).add(tag)
        next_index = len(structural)
        for rank, path in enumerate(recall):
            if path not in order_index:
                order_index[path] = next_index
                next_index += 1
            scores[path] = scores.get(path, 0.0) + 1.0 / (RRF_K + rank)
            method_sets.setdefault(path, set()).add(METHOD_RECALL)

        # Stable sort by descending fused score; Python's sort is stable, so feeding the
        # paths in structural-then-recall discovery order makes that the tie-break.
        candidates = sorted(order_index, key=lambda p: order_index[p])
        candidates.sort(key=lambda p: scores[p], reverse=True)
        ordered = candidates[:max_pages]
        methods = {
            path: tuple(m for m in _METHOD_ORDER if m in method_sets[path])
            for path in ordered
        }
        return ordered, methods

    # ---- pass 1: lexical scan over the curated folders --------------------------

    def grep(self, term: str, *, limit: int = 20) -> list[str]:
        """Lexically scan :data:`SEARCHED_DIRS` ``*.md`` for ``term``, ranked by hits.

        Each candidate page is scored by **how many distinct query tokens it matches**,
        so a natural-language query (``"black curly dog gingham bed"``) surfaces the
        page that hits the most words first -- even when it lives in a folder scanned
        last (issue #96). Tokens are matched on **word boundaries** (a regex
        ``\\b<token>\\b``), case-insensitively, so ``"bed"`` no longer matches
        ``embedded`` and ``"do"`` no longer matches ``window``/``document`` -- substring
        noise that used to flood the results is gone.

        A token hitting the **filename or the page's frontmatter** (its ``title:`` /
        ``summary:`` gloss -- #72 / ADR 0008) weighs more than one hitting only the
        body, so a page whose title matches a word outranks a page that merely mentions
        it in prose for the same token count. The ranking key is a pair -- ``(distinct
        tokens matched, placement-weight sum)`` -- so token *count* dominates (a page
        matching more words always wins) and placement only breaks ties within a count
        tier, each token adding :data:`_HIGH_WEIGHT` for a filename/frontmatter hit or
        :data:`_LOW_WEIGHT` for a body-only hit.

        Candidates are gathered in the existing stable order (folder order from
        :data:`SEARCHED_DIRS`, then filename order) and **stable-sorted by that key
        descending**, so two pages with an identical key keep that original order (the
        pre-#96 tie-break). The ranked list is then deduplicated-by-construction and
        capped at ``limit``.

        Args:
            term: The search text; split into case-insensitive tokens.
            limit: Maximum number of paths to return.

        Returns:
            Matching vault-relative ``.md`` paths, ranked best-first and capped.
        """
        tokens = _tokenize(term)
        if not tokens or limit < 1:
            return []
        patterns = [_token_pattern(token) for token in tokens]
        # Gather every matching page with its ranking key in the existing stable scan
        # order (folder order, then filename order). The sort below is stable, so pages
        # with an identical key keep this order -- preserving the pre-#96 tie-break.
        scored: list[tuple[int, int, str]] = []
        for folder in SEARCHED_DIRS:
            directory = self._vault.root / folder
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.md")):
                rel = f"{folder}/{entry.name}"
                # The filename and the page's frontmatter are the high-weight haystack;
                # the body is the low-weight one. _safe_read returns the raw text with
                # the leading "---" frontmatter block intact (#72), which we split off.
                raw = self._safe_read(entry).lower()
                front, body = _split_frontmatter(raw)
                high_hay = f"{entry.name.lower()}\n{front}"
                matched = 0
                weight = 0
                for pattern in patterns:
                    if pattern.search(high_hay):
                        matched += 1
                        weight += _HIGH_WEIGHT
                    elif pattern.search(body):
                        matched += 1
                        weight += _LOW_WEIGHT
                if matched:
                    scored.append((matched, weight, rel))
        # Rank by distinct-token count first, then placement weight; stable, so equal
        # keys keep their scan order. (matched, weight) descending = best page first.
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [rel for _matched, _weight, rel in scored[:limit]]

    # ---- pass 2: graph navigation -----------------------------------------------

    def follow_wikilinks(self, path: str, *, limit: int = 20) -> list[str]:
        """Resolve the ``[[wikilinks]]`` in a page body to existing vault paths.

        Reads the page at ``path`` (confined by the vault), extracts every
        ``[[target]]`` (alias and ``#anchor`` suffixes stripped), resolves each target
        to a real page via :meth:`_target_to_path`, and returns the unique, existing
        targets in body order. Dangling links (no such page) are silently skipped, so
        the result only ever contains real, confined paths.

        Args:
            path: The vault-relative path of the page whose links to follow.
            limit: Maximum number of resolved links to return.

        Returns:
            Resolved, existing vault-relative paths, ordered and capped. An empty list
            if ``path`` does not exist or has no resolvable links.
        """
        if limit < 1:
            return []
        try:
            page = self._vault.read_page(path)
        except VaultError:
            return []
        resolved: list[str] = []
        seen: set[str] = set()
        for match in _WIKILINK_RE.finditer(page.body):
            target = match.group(1).strip()
            if not target:
                continue
            candidate = self._target_to_path(target)
            if candidate is None or candidate in seen or candidate == path:
                continue
            seen.add(candidate)
            resolved.append(candidate)
            if len(resolved) >= limit:
                break
        return resolved

    # ---- pass 3: semantic recall ------------------------------------------------

    def recall_paths(
        self,
        query: str,
        *,
        limit: int = 10,
        types: frozenset[str] | None = REFERENCE_TYPES,
    ) -> list[str]:
        """Semantic recall via Hindsight, keeping only hits that resolve to real pages.

        Calls :meth:`Hindsight.recall` and returns the ``RecallHit.path`` values, but
        **only those that resolve to a real, confined vault page**. This defends against
        a stale or poisoned index whose ``SOURCE:`` line names a page that no longer
        exists (or a path that would escape the vault): such hits are dropped rather
        than fabricated into a citation. Order is preserved, duplicates removed.

        Recall is **scoped to reference types by default** (ADR 0004 + ADR 0005): the
        index holds every content page, so knowledge Q&A filters to
        :data:`~thoth.vault.REFERENCE_TYPES` (``entity``/``note``/``memory``) to exclude
        the actionable ``action`` type (todos and the to-consume media queue) and keep
        the precision it had when only knowledge was indexed. With the knowledge /
        life-admin families gone, the scope is the reference/actionable axis carried on
        the page ``type`` tag, not a family. A caller wanting actionable recall passes a
        different ``types`` set, or ``None`` to search everything.

        Args:
            query: The natural-language query passed to Hindsight.
            limit: Maximum number of recall hits to request.
            types: The page-type domain scope forwarded to :meth:`Hindsight.recall`;
                defaults to reference types, ``None`` searches all indexed content.

        Returns:
            Vault-relative paths from recall that exist on disk, ordered and deduped.
        """
        if limit < 1:
            return []
        kept: list[str] = []
        seen: set[str] = set()
        for hit in self._hindsight.recall(query, limit=limit, types=types):
            path = hit.path
            if path in seen:
                continue
            if not self._vault.is_inside(path):
                continue
            if not self._vault.page_exists(path):
                continue
            seen.add(path)
            kept.append(path)
        return kept

    # ---- the unfabricable citation ----------------------------------------------

    def build_citation(self, path: str) -> Citation:
        """Confine ``path``, read its title, and build the canonical link + wikilink.

        This is the single place a citation is minted, and it is deliberately strict:
        the path is run through :meth:`~thoth.vault.Vault.obsidian_uri` (which first
        calls :meth:`~thoth.vault.Vault.resolve`), so a path outside the vault raises
        :class:`~thoth.vault.PathConfinementError` and no citation can be fabricated.
        The ``obsidian_uri`` is therefore exactly ``config.obsidian_uri(path)`` for a
        confined path, the ``wikilink`` is derived from the real filename stem, and the
        ``snippet`` is the page's own ``summary:`` frontmatter gloss (issue #72 / ADR
        0008) when it carries one, else ``""``.

        Args:
            path: A vault-relative path to a ``.md`` page.

        Returns:
            A :class:`Citation` for the page.

        Raises:
            thoth.vault.PathConfinementError: if ``path`` escapes the vault root.
            thoth.vault.VaultError: if the page does not exist.
        """
        obsidian_uri = self._vault.obsidian_uri(path)
        slug = PurePosixPath(path).stem
        page = self._vault.read_page(path)
        title_value = page.frontmatter.get("title")
        title = title_value if isinstance(title_value, str) and title_value else slug
        summary_value = page.frontmatter.get("summary")
        snippet = summary_value.strip() if isinstance(summary_value, str) else ""
        return Citation(
            path=PurePosixPath(path).as_posix(),
            title=title,
            obsidian_uri=obsidian_uri,
            wikilink=f"[[{slug}]]",
            snippet=snippet,
        )

    # ---- internals --------------------------------------------------------------

    def _compose(
        self, query: str, consulted: list[Citation]
    ) -> tuple[str, list[Citation]]:
        """Compose the prose answer and the *used* citation subset (issue #34).

        With an injected LLM the consulted page bodies are handed to the model as
        indexed context; the model writes natural prose and ends with a ``USED: 1, 3``
        line naming the candidates that directly supported the answer. That line is
        parsed, mapped back to the consulted citations, and stripped from the displayed
        prose; the matching subset is returned. A missing/garbled ``USED:`` line falls
        back to keeping **all** consulted citations (the pre-#34 behaviour). Without an
        LLM a deterministic excerpt of the top consulted page is returned with that
        single page as its citation.

        Args:
            query: The natural-language query.
            consulted: The harness-built citations for every retrieved candidate page.

        Returns:
            A ``(answer, used_citations)`` pair: the displayed prose (``USED:`` line
            stripped) and the subset of ``consulted`` the answer actually used.
        """
        llm = self._llm
        if llm is not None:
            return self._compose_with_llm(llm, query, consulted)
        return self._excerpt(consulted[0].path), consulted[:1]

    def _compose_with_llm(
        self, llm: LLM, query: str, consulted: list[Citation]
    ) -> tuple[str, list[Citation]]:
        """Hand the indexed candidate pages to the LLM; return prose + the used subset.

        Each candidate is labelled with a 1-based index and its full excerpt is handed
        to the model verbatim (image ``![[embeds]]`` and all, so the model can answer
        questions *about* the attachments). Clean Slack output is the prompt's job, not
        a pre-processor's: the model is told to write natural, concise prose in Slack
        ``mrkdwn`` (``*bold*``/``_italic_``/bullets, never GitHub ``**bold**``),
        referring to pages by title only -- never pasting paths, ``[[wikilinks]]`` or
        ``![[embeds]]``, and never narrating the source list (the harness attaches it,
        so the model must not mention it; issue #63). It ends with a ``USED: <indices>``
        line; that line is parsed back to the consulted citations, stripped from the
        displayed answer, and the used subset returned. A missing/garbled line falls
        back to all citations.
        """
        context_parts: list[str] = []
        for index, citation in enumerate(consulted, start=1):
            body = self._excerpt(citation.path, limit=2000)
            context_parts.append(
                f"[{index}] ## {citation.title} ({citation.path})\n{body}"
            )
        context = "\n\n".join(context_parts)
        prompt = (
            "Answer the question using only the numbered vault pages below.\n\n"
            "Write a natural, concise answer in your own words. Format it as Slack "
            "mrkdwn: *bold* (single asterisks), _italic_ (single underscores) and "
            "lines starting with a bullet for lists -- never GitHub-style **bold** or "
            "Markdown # headings. Refer to pages by their title; do not paste file "
            "paths, [[wikilinks]] or ![[embeds]], and do not mention or list the "
            "sources -- just answer the question.\n\n"
            "On the final line, list the page numbers that directly support your "
            "answer as `USED: 1, 3` (comma-separated), or `USED: none` if no page "
            "applies. Put nothing after that line.\n\n"
            f"Question: {query}\n\nVault pages:\n{context}"
        )
        response = llm.complete([Message(role="user", content=prompt)])
        raw = extract_text(response).strip()
        return _split_used(raw, consulted)

    def _excerpt(self, path: str, *, limit: int = _EXCERPT_CHARS) -> str:
        """Return a stripped, length-capped excerpt of a page body (deterministic)."""
        try:
            page = self._vault.read_page(path)
        except VaultError:
            return ""
        body = page.body.strip()
        if len(body) <= limit:
            return body
        return body[:limit].rstrip() + "…"

    def _target_to_path(self, target: str) -> str | None:
        """Resolve a wikilink/catalog target to an existing vault page path or ``None``.

        Accepts a folder-qualified target (``people/jane-doe``) verbatim, and a bare
        slug (``program-motion-controller``) by probing each searched folder in order.
        A trailing ``.md`` is tolerated. Only confined, existing pages are returned, so
        a target that would escape the vault never resolves.
        """
        cleaned = target.strip().strip("/")
        if not cleaned:
            return None
        if cleaned.endswith(".md"):
            cleaned = cleaned[: -len(".md")]
        if "/" in cleaned:
            candidate = f"{cleaned}.md"
            if self._vault.is_inside(candidate) and self._vault.page_exists(candidate):
                return PurePosixPath(candidate).as_posix()
            return None
        for folder in SEARCHED_DIRS:
            candidate = f"{folder}/{cleaned}.md"
            if self._vault.is_inside(candidate) and self._vault.page_exists(candidate):
                return candidate
        return None

    def _safe_read(self, absolute_path: Path) -> str:
        """Read a small text file for grep, returning ``""`` on any read failure."""
        try:
            if absolute_path.stat().st_size > _MAX_GREP_BYTES:
                return ""
            return absolute_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""


def _tokenize(text: str) -> list[str]:
    """Split a query into lowercase, non-empty whitespace-separated tokens."""
    return [token for token in text.lower().split() if token]


def _token_pattern(token: str) -> re.Pattern[str]:
    """Compile a case-insensitive, word-boundary matcher for one query token (#96).

    Word boundaries (``\\b<token>\\b``) stop the substring noise the old ``token in
    haystack`` scan produced: ``"bed"`` no longer matches ``embedded`` and ``"do"`` no
    longer matches ``window``/``document``. The token is regex-escaped so punctuation in
    a slug-like token (``"drive-control-module"``) matches literally, and a leading or
    trailing word boundary is only asserted when the token *starts*/*ends* with a word
    character (so a token like ``"c++"`` still matches at its non-word edge).
    """
    body = re.escape(token)
    left = r"\b" if token[:1].isalnum() or token[:1] == "_" else ""
    right = r"\b" if token[-1:].isalnum() or token[-1:] == "_" else ""
    return re.compile(f"{left}{body}{right}", re.IGNORECASE)


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split a page's raw text into its YAML frontmatter and its body (#96 weighting).

    A vault page opens with a ``---`` fence, the YAML frontmatter, a closing ``---``
    fence, then the body (the same shape ``python-frontmatter`` writes). This returns
    ``(frontmatter, body)`` so grep can weight a token hitting the title/summary gloss
    above one hitting only prose. When the text has no well-formed frontmatter block the
    whole thing is treated as body (empty frontmatter), so a malformed or fence-less
    page never crashes the scan and simply matches at the lower body weight.
    """
    if not raw.startswith("---"):
        return "", raw
    # Find the closing fence: a line that is exactly "---" after the opening one.
    closing = re.search(r"\n---[ \t]*(?:\n|$)", raw)
    if closing is None:
        return "", raw
    return raw[3 : closing.start()], raw[closing.end() :]


def _split_used(raw: str, consulted: list[Citation]) -> tuple[str, list[Citation]]:
    """Split the model reply into displayed prose + the used citations (issue #34).

    Finds the **last** ``USED: 1, 3`` (or ``USED: none``) line (the prompt promises the
    selection is on the *final* line, with nothing after it), maps its 1-based indices
    back to ``consulted`` citations, and returns the prose with the selection line(s)
    removed plus the matching subset. If the model misbehaves and emits more than one
    selection line, only the last drives the subset, but **every** selection-only line
    is stripped from the prose so none leaks into the displayed answer. A legitimate
    prose sentence that merely *begins* with ``USED:`` (followed by words, not indices)
    is preserved. Robust fallback: a missing/garbled/empty selection keeps **all**
    consulted citations (the pre-#34 behaviour) so a malformed model reply never crashes
    and never silently drops every source. ``USED: none`` yields an empty subset (the
    answer cited nothing), so the renderer shows prose alone.

    Args:
        raw: The model's full text reply (may end with a ``USED:`` line).
        consulted: The candidate citations, in the 1-based order shown to the model.

    Returns:
        A ``(prose, used)`` pair: the answer with the ``USED:`` line stripped, and the
        used citation subset.
    """
    matches = list(_USED_LINE_RE.finditer(raw))
    match = matches[-1] if matches else None
    if match is None:
        return raw.strip(), list(consulted)
    # The last line drives the subset; strip every selection-only line (a stray earlier
    # "USED: 1" must not survive in the prose) while keeping any "USED: <words>" prose.
    prose = _USED_SELECTION_LINE_RE.sub("", raw).strip()
    selection = match.group(1).strip()
    if selection.lower() == "none":
        return prose, []
    indices = [int(tok) for tok in re.findall(r"\d+", selection)]
    if not indices:
        # A garbled selection (no parseable index, not the explicit "none"): keep all.
        return prose, list(consulted)
    used: list[Citation] = []
    seen: set[int] = set()
    for index in indices:
        if 1 <= index <= len(consulted) and index not in seen:
            seen.add(index)
            used.append(consulted[index - 1])
    # Every index out of range -> nothing matched; fall back to all (never drop all).
    if not used:
        return prose, list(consulted)
    return prose, used
