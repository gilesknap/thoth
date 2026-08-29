"""The :class:`QueryEngine` facade: injected collaborators and thin public methods.

The engine holds the collaborators and documents the public retrieval surface. Each
method delegates to the pass functions in the sibling modules, passing those
collaborators explicitly.
"""

from __future__ import annotations

from thoth.config import Config
from thoth.hindsight import Hindsight
from thoth.llm import LLM
from thoth.vault import REFERENCE_TYPES, Vault

from ._blend import _answer
from ._compose import _build_citation
from ._retrieval import _follow_links, _grep, _recall_paths
from ._shared import Citation, QueryResult


class QueryEngine:
    """Cost-ordered retrieval over a real vault, with Hindsight and optionally an LLM.

    Every collaborator is injected. The vault is real and on disk, Hindsight is the
    semantic recall seam, and the optional LLM composes prose. The engine performs no
    network IO itself, because recall and prose are delegated to the collaborators.
    """

    def __init__(
        self,
        config: Config,
        vault: Vault,
        hindsight: Hindsight,
        llm: LLM | None = None,
    ) -> None:
        """Stores the injected collaborators.

        Args:
            config: Frozen runtime config, kept for parity with the sibling modules.
                Link encoding is delegated to the vault.
            vault: The real, path-confined vault facade.
            hindsight: The semantic recall seam.
            llm: Optional LLM for prose. None falls back to a deterministic excerpt
                of the top page.
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
        """Blends structural and semantic retrieval, then composes an answer (#143).

        Two retrieval sources both vote on the cited set:

        * the STRUCTURAL source, a grep hit list, which scans frontmatter so a page's
          ``summary:`` gloss is matched there, followed by link-graph navigation from
          those hits, deduped and existence-checked into one ordered list;
        * the RECALL source, semantic Hindsight recall, which always gets a vote when
          ``use_recall`` is true. There is no "only when results are thin" gate.

        Because recall is the expensive, subprocess-backed pass, it is submitted to a
        worker thread first and the cheap structural pass runs on the calling thread
        while recall is in flight, so its latency overlaps grep instead of serialising
        after it. The worker is pure and mutates no shared state, so all merging happens
        single-threaded after the join.

        The two ranked lists merge by Reciprocal Rank Fusion (:data:`RRF_K`): each
        unique path scores the sum of ``1 / (RRF_K + rank)`` over the sources it appears
        in, and paths sort by that score with structural discovery order breaking ties.
        A page found by both sources floats up, a strong recall-only hit earns a slot
        even when grep already filled the budget, and empty recall collapses to pure
        structural order.

        Prose comes from the injected LLM when there is one, otherwise from a
        deterministic excerpt of the top page. Either way the citation block is
        harness-built from confined, real paths. With an LLM the citations are the used
        subset the model named on its ``USED:`` line (issue #34), ``consulted_count``
        records how many candidates were offered before that filter, and ``provenance``
        records the methods that surfaced each page.

        Args:
            query: The natural-language query, which also keys recall and the prose.
            max_pages: Maximum candidate pages to consult and cite.
            use_recall: False skips the semantic pass entirely, spawning no worker
                thread, which is the cheap structural-only path.
            search_terms: Intent-gate keywords to grep instead of the raw query
                (issue #102), so "list me the docs about dogs" greps ``dog`` rather
                than the noise words. Empty or None greps the query verbatim.

        Returns:
            The answer, whose citations all resolve to real vault pages.

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
        """Lexically scans the searched folders for ``term``, ranked by hits.

        Each page scores on how many distinct query tokens it matches, so a
        natural-language query surfaces the page hitting the most words first even when
        it lives in a folder scanned last (issue #96). Tokens match on word boundaries
        (``<token>``), case-insensitively, so ``bed`` no longer matches ``embedded``
        and ``do`` no longer matches ``window``. That substring noise used to flood the
        results.

        A token hitting the filename or frontmatter, meaning the title or ``summary:``
        gloss (#72, ADR 0008), weighs more than one hitting only the body. The key is a
        pair of distinct tokens matched and placement-weight sum, so token count
        dominates and placement only breaks ties within a tier.

        Candidates are gathered in the stable folder-then-filename order and
        stable-sorted by that key, so an identical key keeps the original order.

        Args:
            term: The search text, split into case-insensitive tokens.
            limit: Maximum paths to return.

        Returns:
            Matching vault paths, ranked best-first and capped.
        """
        return _grep(self._vault, term, limit=limit)

    # ---- pass 2: graph navigation -----------------------------------------------

    def follow_links(self, path: str, *, limit: int = 20) -> list[str]:
        """Resolves a page body's inter-page links to existing vault paths.

        Extracts the OKF standard ``[text](path.md)`` form and any residual
        ``[[target]]`` wikilink, stripping alias and anchor suffixes, then resolves each
        target to a real page by probing each searched folder for a bare slug. Dangling
        links are silently skipped, so the result only ever holds real, confined paths.

        Args:
            path: Vault path of the page whose links to follow.
            limit: Maximum resolved links to return.

        Returns:
            Existing vault paths in body order, capped. Empty when the page is absent
            or carries no resolvable links.
        """
        return _follow_links(self._vault, path, limit=limit)

    # ---- pass 3: semantic recall ------------------------------------------------

    def recall_paths(
        self,
        query: str,
        *,
        limit: int = 10,
        types: frozenset[str] | None = REFERENCE_TYPES,
    ) -> list[str]:
        """Recalls semantically, keeping only hits that resolve to real pages.

        This defends against a stale or poisoned index whose ``SOURCE:`` line names a
        page that no longer exists, or a path that would escape the vault. Such hits are
        dropped rather than fabricated into a citation.

        Recall is scoped to reference types by default (ADR 0004 and ADR 0005). The
        index holds every content page, so knowledge Q&A filters to the reference types
        to exclude the actionable ones and keep the precision it had when only knowledge
        was indexed. The scope is the reference-versus-actionable axis carried on the
        page type, not a family.

        Args:
            query: The natural-language query passed to Hindsight.
            limit: Maximum recall hits to request.
            types: Page-type scope, defaulting to reference types. None searches all
                indexed content.

        Returns:
            Recall paths that exist on disk, ordered and deduped.
        """
        return _recall_paths(
            self._hindsight, self._vault, query, limit=limit, types=types
        )

    # ---- the unfabricable citation ----------------------------------------------

    def build_citation(self, path: str) -> Citation:
        """Confines a path, reads its title, and builds the link and wikilink.

        This is the single place a citation is minted, and it is deliberately strict.
        The path goes through :meth:`~thoth.vault.Vault.obsidian_uri`, which resolves it
        first, so a path outside the vault raises and no citation can be fabricated. The
        wikilink comes from the real filename stem, and the snippet is the page's own
        ``summary:`` gloss (issue #72, ADR 0008) when it carries one.

        Args:
            path: A vault path to a ``.md`` page.

        Returns:
            The citation for the page.

        Raises:
            thoth.vault.PathConfinementError: if the path escapes the vault root.
            thoth.vault.VaultError: if the page does not exist.
        """
        return _build_citation(self._vault, path)
