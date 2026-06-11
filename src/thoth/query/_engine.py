"""The :class:`QueryEngine` facade: injected collaborators + thin public methods.

The engine holds the injected collaborators and documents the public retrieval
surface; each method delegates to the module-level pass functions in
:mod:`thoth.query._retrieval` / :mod:`thoth.query._blend` /
:mod:`thoth.query._compose` with those collaborators as explicit parameters.
"""

from __future__ import annotations

from thoth.config import Config
from thoth.hindsight import Hindsight
from thoth.llm import LLM
from thoth.vault import REFERENCE_TYPES, Vault

from ._blend import _answer
from ._compose import _build_citation
from ._retrieval import _follow_wikilinks, _grep, _recall_paths
from ._shared import Citation, QueryResult


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
        return _answer(
            self._vault,
            self._hindsight,
            self._llm,
            query,
            max_pages=max_pages,
            use_recall=use_recall,
            search_terms=search_terms,
        )

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
        return _grep(self._vault, term, limit=limit)

    # ---- pass 2: graph navigation -----------------------------------------------

    def follow_wikilinks(self, path: str, *, limit: int = 20) -> list[str]:
        """Resolve the ``[[wikilinks]]`` in a page body to existing vault paths.

        Reads the page at ``path`` (confined by the vault), extracts every
        ``[[target]]`` (alias and ``#anchor`` suffixes stripped), resolves each target
        to a real page (probing each searched folder for a bare slug), and returns the
        unique, existing targets in body order. Dangling links (no such page) are
        silently skipped, so the result only ever contains real, confined paths.

        Args:
            path: The vault-relative path of the page whose links to follow.
            limit: Maximum number of resolved links to return.

        Returns:
            Resolved, existing vault-relative paths, ordered and capped. An empty list
            if ``path`` does not exist or has no resolvable links.
        """
        return _follow_wikilinks(self._vault, path, limit=limit)

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
        return _recall_paths(
            self._hindsight, self._vault, query, limit=limit, types=types
        )

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
        return _build_citation(self._vault, path)
