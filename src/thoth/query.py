"""Cost-ordered, vault-only retrieval with harness-built (unfabricable) citations.

This is the read side of the appliance (SPEC section 7). A query is answered by
walking progressively more expensive retrieval passes and stopping as soon as the
vault has yielded enough pages:

1. ``index.md`` -- the cheap, authoritative catalog (parsed by
   :meth:`QueryEngine.index_summaries`).
2. a lexical scan (grep) over the curated knowledge folders
   (:meth:`QueryEngine.grep`).
3. ``[[wikilink]]`` graph navigation from the pages already found
   (:meth:`QueryEngine.follow_wikilinks`).
4. semantic recall via Hindsight (:meth:`QueryEngine.recall_paths`), used only when
   the cheaper structural passes did not already answer.

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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from thoth.config import Config
from thoth.hindsight import Hindsight
from thoth.llm import LLM, Message, extract_text
from thoth.vault import REFERENCE_TYPES, Vault, VaultError

__all__ = [
    "SEARCHED_DIRS",
    "Citation",
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

# Catalog line in index.md: "- [[program-motion-controller]] - central coordinator..."
# The separator may be a hyphen or an em dash (the SPEC seed template uses both forms).
_CATALOG_LINE_RE: re.Pattern[str] = re.compile(
    r"^- \[\[([^\]|#]+?)\]\]\s*(?:[-–—]\s*(.*))?$"
)

# Folders searched for lexical/structural retrieval (the reference layer). raw/ and the
# actionable actions/ folder are intentionally excluded: retrieval composes from
# reference pages, and raw sources are reached via their owning page's wikilinks.
SEARCHED_DIRS: tuple[str, ...] = ("entities", "notes", "memories")
"""Top-level vault folders scanned by :meth:`QueryEngine.grep` (reference layer)."""

# Cap on bytes read per page during grep so a pathological file cannot blow up a scan.
_MAX_GREP_BYTES: int = 1_000_000

# Excerpt length used for the deterministic (no-LLM) answer fallback.
_EXCERPT_CHARS: int = 600


class QueryError(Exception):
    """Raised when a query cannot be answered (for example no vault pages match)."""


@dataclass(frozen=True, slots=True)
class Citation:
    """Harness-built, unfabricable handle for one cited vault page.

    Every field is derived from a real, path-confined vault page: ``path`` has passed
    :meth:`~thoth.vault.Vault.resolve`, ``obsidian_uri`` comes from
    :meth:`~thoth.vault.Vault.obsidian_uri`, and ``wikilink`` is derived from the page's
    actual filename. The model never supplies any of these.
    """

    path: str
    """The vault-relative, confined path of the cited page (e.g. ``entities/x.md``)."""
    title: str
    """The page's human-readable title (from frontmatter, else the slug)."""
    obsidian_uri: str
    """The canonical ``obsidian://open`` deep link from :meth:`Vault.obsidian_uri`."""
    wikilink: str
    """The ``[[<slug>]]`` link derived from the real filename stem."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """A composed answer plus its harness-attached citations.

    ``citations`` is the **used** subset: when an LLM composes the prose it ends its
    reply with a ``USED: 1, 3`` line naming the candidate pages that directly supported
    the answer, and only those are kept (issue #34) so the Slack ``Sources:`` list
    reflects what the answer actually drew on, not the whole retrieval candidate set. A
    missing/garbled ``USED:`` line falls back to keeping every consulted page (the
    pre-#34 behaviour), and the deterministic (no-LLM) path keeps its single top page.

    ``consulted_count`` records how many candidate pages were retrieved and offered to
    the model *before* the ``USED:`` filter, so an operator log (issue #52) can compare
    consulted-vs-used recall. ``used_recall`` records whether the (more expensive)
    Hindsight semantic pass was needed: it is ``False`` when the cheap structural passes
    (index/grep/wikilinks) already produced the citations, and ``True`` when recall
    contributed.
    """

    answer: str
    """The composed prose answer (LLM-written when an LLM is injected, else excerpt)."""
    citations: list[Citation] = field(default_factory=list)
    """The citations the answer used, in retrieval order, deduplicated by path."""
    used_recall: bool = False
    """Whether the semantic Hindsight recall pass contributed to the result."""
    consulted_count: int = 0
    """How many candidate pages were offered to the model before the ``USED`` filter."""


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
        self, query: str, *, max_pages: int = 5, use_recall: bool = True
    ) -> QueryResult:
        """Run the full cost-ordered retrieval and compose an answer with citations.

        The passes run cheapest-first and short-circuit: an index/grep hit seeds the
        candidate set, ``[[wikilink]]`` navigation expands it, and Hindsight recall is
        consulted only when ``use_recall`` is true *and* the cheap structural passes
        found nothing (so a query the index/grep/wikilinks already answered never burns
        a recall call). The prose is written by the injected LLM if present, else taken
        as a deterministic excerpt of the top page; either way the citation block is
        harness-built from confined, real paths. With an LLM the result's citations are
        the **used** subset the model named on its ``USED:`` line (issue #34), and
        ``consulted_count`` records how many candidates were offered before that filter.

        Args:
            query: The natural-language query.
            max_pages: The maximum number of candidate pages to consult and cite.
            use_recall: When false, the semantic Hindsight pass is skipped entirely
                (the cheap, structural-only path).

        Returns:
            A :class:`QueryResult` whose citations all resolve to real vault pages.

        Raises:
            QueryError: if no vault page matches the query at all.
        """
        if max_pages < 1:
            raise QueryError("max_pages must be at least 1")

        ordered: list[str] = []
        seen: set[str] = set()
        recall_only: set[str] = set()

        def add(paths: list[str], *, from_recall: bool = False) -> None:
            """Append new, unseen, real vault paths preserving discovery order."""
            for path in paths:
                if path not in seen and self._vault.page_exists(path):
                    seen.add(path)
                    ordered.append(path)
                    if from_recall:
                        recall_only.add(path)

        # 1) index.md catalog -> grep, both lexical and cheap.
        add(self._index_matches(query))
        add(self.grep(query, limit=max_pages * 4))

        # 2) graph navigation from what we already found (bounded).
        for path in list(ordered):
            if len(ordered) >= max_pages:
                break
            add(self.follow_wikilinks(path, limit=max_pages))

        # 3) semantic recall -- the expensive pass. Cost-ordered (SPEC section 7): it
        # runs only when the cheap structural passes found nothing, so a query the
        # index/grep/wikilinks already answered never burns a recall call.
        if use_recall and not ordered:
            add(self.recall_paths(query, limit=max_pages * 2), from_recall=True)

        if not ordered:
            raise QueryError(f"no vault page found for query: {query!r}")

        cited_paths = ordered[:max_pages]
        consulted = [self.build_citation(path) for path in cited_paths]
        answer, used = self._compose(query, consulted)
        # Recall "contributed" only if a recall-only page is in the *used* subset (so a
        # consulted-but-unused recall page no longer counts as recall having helped).
        used_recall = any(c.path in recall_only for c in used)
        logger.info(
            "query consulted %d pages, model used %d", len(consulted), len(used)
        )
        return QueryResult(
            answer=answer,
            citations=used,
            used_recall=used_recall,
            consulted_count=len(consulted),
        )

    # ---- pass 1: the authoritative catalog --------------------------------------

    def index_summaries(self) -> dict[str, str]:
        """Parse ``index.md`` catalog lines into a ``{wikilink_target: summary}`` map.

        Reads the catalog lines of the form ``- [[slug]] - summary`` (the separator may
        be a hyphen or an em dash, per the SPEC seed template) from ``index.md``. The
        map key is the raw wikilink target as written (for example ``program-motion-
        controller`` or ``people/jane-doe``); blank/non-catalog lines and section
        headings are ignored. A missing ``index.md`` yields an empty map rather than
        raising, so a freshly initialised vault still answers structurally.

        Returns:
            An ordered mapping of wikilink target to its one-line summary (``""`` when a
            catalog line carries no summary text).
        """
        summaries: dict[str, str] = {}
        try:
            text = self._read_text("index.md")
        except VaultError:
            return summaries
        if text is None:
            return summaries
        for line in text.splitlines():
            match = _CATALOG_LINE_RE.match(line.strip())
            if match is None:
                continue
            target = match.group(1).strip()
            summary = (match.group(2) or "").strip()
            summaries[target] = summary
        return summaries

    def _index_matches(self, query: str) -> list[str]:
        """Return vault paths whose index catalog entry matches ``query`` (lexical).

        A catalog target or its summary that contains a query token (case-insensitive)
        is resolved to a real vault page via :meth:`_target_to_path`. Order follows the
        catalog; non-resolving (dangling) targets are dropped.
        """
        tokens = _tokenize(query)
        if not tokens:
            return []
        hits: list[str] = []
        for target, summary in self.index_summaries().items():
            haystack = f"{target} {summary}".lower()
            if any(token in haystack for token in tokens):
                path = self._target_to_path(target)
                if path is not None:
                    hits.append(path)
        return hits

    # ---- pass 2: lexical scan over the curated folders --------------------------

    def grep(self, term: str, *, limit: int = 20) -> list[str]:
        """Lexically scan :data:`SEARCHED_DIRS` ``*.md`` for ``term`` (filename + body).

        The match is case-insensitive over both the filename and the page body. Any
        whitespace-separated token of ``term`` matching is enough (so a multi-word query
        still finds a page that mentions one of its words). Results are returned as
        vault-relative paths in a stable order (folder order, then filename order),
        deduplicated, capped at ``limit``.

        Args:
            term: The search text; split into case-insensitive tokens.
            limit: Maximum number of paths to return.

        Returns:
            Matching vault-relative ``.md`` paths, ordered and capped.
        """
        tokens = _tokenize(term)
        if not tokens or limit < 1:
            return []
        hits: list[str] = []
        for folder in SEARCHED_DIRS:
            directory = self._vault.root / folder
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.md")):
                rel = f"{folder}/{entry.name}"
                name_hay = entry.name.lower()
                if any(token in name_hay for token in tokens):
                    hits.append(rel)
                    if len(hits) >= limit:
                        return hits
                    continue
                body = self._safe_read(entry).lower()
                if any(token in body for token in tokens):
                    hits.append(rel)
                    if len(hits) >= limit:
                        return hits
        return hits

    # ---- pass 3: graph navigation -----------------------------------------------

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

    # ---- pass 4: semantic recall ------------------------------------------------

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
        confined path, and the ``wikilink`` is derived from the real filename stem.

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
        return Citation(
            path=PurePosixPath(path).as_posix(),
            title=title,
            obsidian_uri=obsidian_uri,
            wikilink=f"[[{slug}]]",
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

    def _read_text(self, rel: str) -> str | None:
        """Read a confined vault file as text, or ``None`` when it does not exist."""
        absolute = self._vault.resolve(rel)
        if not absolute.is_file():
            return None
        return absolute.read_text(encoding="utf-8", errors="ignore")


def _tokenize(text: str) -> list[str]:
    """Split a query into lowercase, non-empty whitespace-separated tokens."""
    return [token for token in text.lower().split() if token]


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
