"""Blended web + vault Q&A (``pkm_ask``) with unfabricable vault citations.

This is the second retrieval mode of the appliance (SPEC section 7.1). Where
:mod:`thoth.query` answers from the vault alone, :class:`ResearchEngine` answers a
general question by handing **Claude Sonnet** a *closed, read-only* tool surface --
a vault-read tool (over :class:`~thoth.query.QueryEngine`) plus the SSRF-guarded
``web_search`` / ``web_extract`` tools (over :class:`~thoth.extract.Extractor`) --
and letting the model decide whether the web is needed. A ``research:`` prefix or an
explicit ``force_web`` forces the web tools on; a purely personal lookup ("what are my
todos") stays vault-only and cheap because the web tools are simply never offered.

The model composes the prose answer, but **the harness builds every vault citation**:
each vault page the model actually read is run back through
:meth:`~thoth.query.QueryEngine.build_citation`, which confines the path and encodes
the ``obsidian://`` link, so a citation cannot point outside the vault and cannot be
fabricated (SPEC section 3). Web citations are the URLs the model actually extracted.
An SSRF/extract failure is caught and fed back to the model as a ``tool_result`` error
string -- it never escapes :meth:`ResearchEngine.ask`.

The one *writing* action is the explicit offer-to-save step
(:meth:`ResearchEngine.save_answer`): on confirmation it writes a ``queries/<slug>.md``
page through the validated :meth:`~thoth.vault.Vault.write_page`, so web knowledge
becomes a curated second-brain page. It still routes through the same closed surface.

Import safety: only the standard library plus ``thoth.*`` are imported at module top
level. ``anthropic``/``exa_py``/``firecrawl`` are reached only through the injected
:class:`~thoth.llm.LLM` / :class:`~thoth.extract.Extractor` seams (which import them
lazily), so importing this module at pytest collection is always CI-safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from thoth.config import Config
from thoth.extract import ExtractedDoc, ExtractError, Extractor, SsrfError, WebHit
from thoth.llm import (
    LLM,
    Message,
    assistant_blocks_message,
    extract_text,
    tool_result_block,
    user_blocks_message,
)
from thoth.query import Citation, QueryEngine, QueryError
from thoth.vault import SchemaError, SlugError, Vault, VaultError

__all__ = [
    "RESEARCH_PREFIX",
    "VAULT_READ_TOOL",
    "WEB_EXTRACT_TOOL",
    "WEB_SEARCH_TOOL",
    "AskResult",
    "ResearchEngine",
    "ResearchError",
    "WebCitation",
    "build_research_tools",
    "force_web_requested",
    "strip_research_prefix",
]

RESEARCH_PREFIX: str = "research:"
"""Leading marker (case-insensitive) on a question that forces the web tools on."""

# Tool schemas (plain dicts in the Anthropic tool-use format) handed to
# ``LLM.complete(tools=...)``. They are documentation-grade constants; the model's
# ``tool_use`` blocks are dispatched by this harness to the extractor's web methods or
# the QueryEngine read helpers -- the model never reaches a client directly.

VAULT_READ_TOOL: dict[str, object] = {
    "name": "vault_read",
    "description": (
        "Read one of the user's own knowledge pages from their personal vault by its "
        "vault-relative path (for example 'entities/foo.md'). Returns the page body. "
        "Use this for anything personal the user has saved; paths come from the "
        "candidate list provided in the question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path to a .md page to read.",
            }
        },
        "required": ["path"],
    },
}
"""Always-available read-only vault tool (dispatched to the QueryEngine)."""

WEB_SEARCH_TOOL: dict[str, object] = {
    "name": "web_search",
    "description": (
        "Search the public web for pages relevant to a query (semantic discovery). "
        "Returns a ranked list of URLs with titles and snippets. Follow up with "
        "web_extract to read a promising result in full."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The natural-language web search query.",
            }
        },
        "required": ["query"],
    },
}
"""Web discovery tool, offered only when web access is allowed (SSRF inside)."""

WEB_EXTRACT_TOOL: dict[str, object] = {
    "name": "web_extract",
    "description": (
        "Fetch a single public URL and return its clean-markdown text so you can read "
        "it in full. Only http/https public URLs are allowed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http/https URL to read.",
            }
        },
        "required": ["url"],
    },
}
"""Web extraction tool, offered only when web access is allowed (SSRF inside)."""

# Slug helpers for save_answer: lowercase, hyphen-separated, capped length.
_SLUG_STRIP_RE: re.Pattern[str] = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_WORDS: int = 8
_MAX_SLUG_LEN: int = 80


class ResearchError(Exception):
    """Raised on an unparseable/empty model result or a vault rejection on save."""


@dataclass(frozen=True, slots=True)
class WebCitation:
    """One web source the model actually read (an extracted URL)."""

    url: str
    """The URL that was extracted (the citable web source)."""
    title: str
    """The page title from extraction (empty string when none was returned)."""


@dataclass(frozen=True, slots=True)
class AskResult:
    """A blended answer plus its harness-built vault citations and web citations.

    Vault citations are :class:`~thoth.query.Citation` (so the renderers shared with
    :mod:`thoth.query`/``slack_app``/``mcp_server`` work unchanged); web citations are
    :class:`WebCitation`. ``used_web`` is true iff any web tool was invoked, and
    ``saved_path`` is populated only after :meth:`ResearchEngine.save_answer`.
    """

    answer: str
    """The composed prose answer (the model's final assistant text)."""
    vault_citations: list[Citation] = field(default_factory=list)
    """Harness-built citations for vault pages the model actually read."""
    web_citations: list[WebCitation] = field(default_factory=list)
    """The URLs the model actually extracted, in read order."""
    used_web: bool = False
    """Whether any web tool (search or extract) was invoked during the run."""
    saved_path: str | None = None
    """Vault-relative path of a saved ``queries/`` page; set by :meth:`save_answer`."""


def force_web_requested(question: str, *, force_web: bool = False) -> bool:
    """Return ``True`` iff the web tools should be offered for this question.

    The web is offered when ``force_web`` is set or when the question carries a leading
    (case-insensitive) ``research:`` marker. A plain personal lookup such as "what are
    my todos" returns ``False`` so it stays vault-only and cheap.

    Args:
        question: The raw question text.
        force_web: An explicit override that forces the web tools on.

    Returns:
        ``True`` if web access should be allowed, ``False`` otherwise.
    """
    if force_web:
        return True
    return question.lstrip().lower().startswith(RESEARCH_PREFIX)


def strip_research_prefix(question: str) -> str:
    """Return ``question`` with a leading ``research:`` marker removed and trimmed.

    The marker match is case-insensitive and only a single leading occurrence is
    removed; a question with no marker is returned stripped but otherwise intact.

    Args:
        question: The raw question text.

    Returns:
        The question without its leading ``research:`` marker, whitespace-trimmed.
    """
    stripped = question.strip()
    if stripped.lower().startswith(RESEARCH_PREFIX):
        return stripped[len(RESEARCH_PREFIX) :].strip()
    return stripped


def build_research_tools(*, allow_web: bool) -> list[dict[str, object]]:
    """Return the tool schemas offered to the model for one ask.

    The :data:`VAULT_READ_TOOL` is always present; :data:`WEB_SEARCH_TOOL` and
    :data:`WEB_EXTRACT_TOOL` are appended only when ``allow_web`` is true (the
    model-decided web gate, SPEC section 7.1).

    Args:
        allow_web: Whether the web tools should be offered.

    Returns:
        A list of Anthropic tool-schema dicts (vault-read, optionally the two web
        tools).
    """
    tools: list[dict[str, object]] = [VAULT_READ_TOOL]
    if allow_web:
        tools.extend((WEB_SEARCH_TOOL, WEB_EXTRACT_TOOL))
    return tools


class ResearchEngine:
    """Blended web + vault Q&A over injected model and web seams (SPEC section 7.1).

    All external boundaries are injected: a :class:`~thoth.query.QueryEngine` (real,
    over the vault) supplies the read-only vault pass and the unfabricable
    :meth:`~thoth.query.QueryEngine.build_citation`; an
    :class:`~thoth.extract.Extractor` supplies the SSRF-guarded web tools; and an
    :class:`~thoth.llm.LLM` drives the Sonnet tool-use loop. The engine performs no
    network or disk I/O directly -- every effect goes through one of these seams or
    :class:`~thoth.vault.Vault`.
    """

    def __init__(
        self,
        config: Config,
        vault: Vault,
        query_engine: QueryEngine,
        extractor: Extractor,
        llm: LLM,
        *,
        max_web_reads: int = 3,
    ) -> None:
        """Store the injected collaborators.

        Args:
            config: The frozen runtime config (kept for parity with sibling modules).
            vault: The path-confined vault facade (used by :meth:`save_answer`).
            query_engine: The vault retrieval engine (candidate pass + citations).
            extractor: The SSRF-guarded web search/extract seam.
            llm: The injectable Anthropic wrapper driving the tool-use loop.
            max_web_reads: The hard cap on ``web_extract`` dispatches per ask.
        """
        self._config = config
        self._vault = vault
        self._query = query_engine
        self._extractor = extractor
        self._llm = llm
        self._max_web_reads = max_web_reads

    # ---- the blended ask ---------------------------------------------------------

    def ask(
        self, question: str, *, force_web: bool = False, max_pages: int = 5
    ) -> AskResult:
        """Answer ``question`` from the vault and (model-decided) the web, citing both.

        Runs three steps: (1) a read-only vault pass via the
        :class:`~thoth.query.QueryEngine` gathers candidate page paths; (2) the web gate
        is decided (``force_web`` or a ``research:`` prefix), the tool set is built, and
        the Sonnet tool-use loop runs -- bounded to ``max_web_reads`` ``web_extract``
        dispatches -- dispatching ``tool_use`` blocks to ``web_search``/``web_extract``/
        ``vault_read``; an SSRF/extract error is fed back to the model as a
        ``tool_result`` error string and never raised; (3) the final assistant text is
        the answer, vault citations are harness-built via
        :meth:`~thoth.query.QueryEngine.build_citation` for the pages the model actually
        read, and web citations are the URLs it extracted.

        Args:
            question: The natural-language question (a leading ``research:`` marker
                forces the web tools on and is stripped from the prompt).
            force_web: Force the web tools on regardless of the prefix.
            max_pages: How many candidate vault pages to surface to the model.

        Returns:
            An :class:`AskResult` whose vault citations all resolve to real, confined
            vault pages and whose web citations are the URLs actually read.

        Raises:
            ResearchError: only when the model produces an empty/blank final answer.
        """
        allow_web = force_web_requested(question, force_web=force_web)
        clean_question = strip_research_prefix(question)
        candidates = self._vault_candidates(clean_question, max_pages=max_pages)
        tools = build_research_tools(allow_web=allow_web)

        loop = _ToolLoop(self, candidates)
        answer = loop.run(clean_question, tools)
        if not answer.strip():
            raise ResearchError("model produced an empty answer")

        vault_citations = self._citations_for(loop.read_vault_paths)
        web_citations = [
            WebCitation(url=url, title=title) for url, title in loop.read_web_urls
        ]
        return AskResult(
            answer=answer.strip(),
            vault_citations=vault_citations,
            web_citations=web_citations,
            used_web=loop.used_web,
        )

    # ---- offer-to-save (the one writing action) ----------------------------------

    def save_answer(
        self,
        question: str,
        result: AskResult,
        *,
        slug: str | None = None,
        today: date | None = None,
    ) -> str:
        """Write the answer as a ``queries/<slug>.md`` page via the validated vault.

        Builds frontmatter (``type='query'``, ``source='mcp'``, a tag set, and a
        ``sources`` list of the web URLs) and a body of the answer followed by a
        ``## Sources`` section listing the web URLs and ``[[wikilinks]]`` for the vault
        citations, then writes it through :meth:`~thoth.vault.Vault.write_page` (which
        validates the folder/type contract, the slug, and confines the path). The slug
        defaults to a slugified question.

        Args:
            question: The original question (used for the title and the default slug).
            result: The :class:`AskResult` to persist.
            slug: An explicit slug; defaults to a slugified ``question``.
            today: The date to stamp; defaults to :meth:`date.today`.

        Returns:
            The vault-relative path written (always under ``queries/``).

        Raises:
            ResearchError: if the slug is invalid or the vault otherwise rejects the
                page (nothing is written outside ``queries/``).
        """
        clean_question = strip_research_prefix(question)
        page_slug = slug if slug is not None else _slugify(clean_question)
        try:
            Vault.validate_slug(page_slug)
        except SlugError as exc:
            raise ResearchError(f"invalid slug for saved answer: {exc}") from exc

        web_urls = [c.url for c in result.web_citations]
        frontmatter: dict[str, object] = {
            "title": clean_question or page_slug.replace("-", " ").title(),
            "type": "query",
            "source": "mcp",
            "tags": ["query"],
        }
        if web_urls:
            frontmatter["sources"] = web_urls
        body = self._render_save_body(result, web_urls)
        try:
            return self._vault.write_page(
                "queries", page_slug, frontmatter, body, today=today
            )
        except (SchemaError, SlugError, VaultError) as exc:
            raise ResearchError(f"vault rejected saved answer: {exc}") from exc

    # ---- thin web pass-throughs (SSRF inside the extractor) ----------------------

    def web_search(self, query: str, *, num_results: int = 5) -> list[WebHit]:
        """Discover candidate web pages via the extractor (SSRF guard inside).

        Args:
            query: The web search query.
            num_results: How many results to request.

        Returns:
            A list of :class:`~thoth.extract.WebHit` (possibly empty).
        """
        return self._extractor.web_search(query, num_results=num_results)

    def web_extract(self, url: str) -> ExtractedDoc:
        """Read a single URL to clean markdown via the extractor (SSRF guard inside).

        Args:
            url: The http/https URL to extract.

        Returns:
            The :class:`~thoth.extract.ExtractedDoc` for the URL.

        Raises:
            thoth.extract.SsrfError: if the URL is blocked by the SSRF guard.
            thoth.extract.ExtractError: if extraction fails.
        """
        return self._extractor.web_extract(url)

    # ---- internals ---------------------------------------------------------------

    def _vault_candidates(self, question: str, *, max_pages: int) -> list[Citation]:
        """Run the read-only vault pass and return harness-built candidate citations.

        Reuses :meth:`~thoth.query.QueryEngine.answer` for the structural + recall pass.
        A :class:`~thoth.query.QueryError` (no vault page matched) is non-fatal here: a
        blended ask may still be answered from the web alone, so an empty candidate list
        is returned rather than raising.
        """
        if max_pages < 1:
            return []
        try:
            vault_result = self._query.answer(question, max_pages=max_pages)
        except QueryError:
            return []
        return list(vault_result.citations)

    def _citations_for(self, paths: list[str]) -> list[Citation]:
        """Build harness citations for read paths, dropping any that don't resolve.

        Each path the model claims to have read is run through
        :meth:`~thoth.query.QueryEngine.build_citation`; a path that escapes the vault
        or names a non-existent page raises and is dropped (so a model that "reads" a
        bogus page yields no fabricated citation for it). Order and de-duplication are
        preserved.
        """
        citations: list[Citation] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            try:
                citations.append(self._query.build_citation(path))
            except VaultError:
                continue
        return citations

    @staticmethod
    def _render_save_body(result: AskResult, web_urls: list[str]) -> str:
        """Render the saved page body: the answer plus a ``## Sources`` section.

        The sources section lists each web URL as a bullet and each vault citation as a
        ``[[wikilink]]`` bullet, so the saved page links back into the vault graph and
        records its web provenance.
        """
        lines = [result.answer.strip(), "", "## Sources"]
        if not web_urls and not result.vault_citations:
            lines.append("- (no external sources)")
        for url in web_urls:
            lines.append(f"- {url}")
        for citation in result.vault_citations:
            lines.append(f"- {citation.wikilink}")
        return "\n".join(lines)


class _ToolLoop:
    """Drives one bounded Sonnet tool-use loop, recording what the model read.

    The loop is internal to :class:`ResearchEngine`. It builds a transcript the real
    Messages API accepts: after a ``stop_reason='tool_use'`` response it appends the
    assistant turn carrying the model's **native** ``tool_use`` block(s)
    (:func:`~thoth.llm.assistant_blocks_message`) and then a single user turn of
    ``tool_result`` blocks (:func:`~thoth.llm.tool_result_block`), each keyed by the
    originating block's ``tool_use_id``. This is the contract the API enforces (a
    tool-use turn must be answered by matching ``tool_result`` blocks); flattening it to
    text -- as an earlier draft did -- passes a call-counting fake but 400s in
    production. The loop terminates when the model returns no ``tool_use`` block, or
    when a hard iteration cap is reached.
    """

    # A hard ceiling on total model turns so a misbehaving fake/model cannot spin
    # forever; web_extract dispatches are separately capped by ``max_web_reads``.
    _MAX_TURNS: int = 12

    def __init__(self, engine: ResearchEngine, candidates: list[Citation]) -> None:
        """Initialise the loop state for one ask.

        Args:
            engine: The owning :class:`ResearchEngine` (for the seams + caps).
            candidates: The harness-built candidate vault citations to offer the model.
        """
        self._engine = engine
        self._candidates = candidates
        self.used_web: bool = False
        self.read_vault_paths: list[str] = []
        self.read_web_urls: list[tuple[str, str]] = []
        self._web_reads: int = 0

    def run(self, question: str, tools: list[dict[str, object]]) -> str:
        """Run the loop and return the model's final assistant text.

        Args:
            question: The cleaned question (prefix already stripped).
            tools: The tool schemas to offer (vault-read, optionally web).

        Returns:
            The concatenated text of the terminating assistant turn.
        """
        messages = [Message(role="user", content=self._initial_prompt(question))]
        last_text = ""
        for _ in range(self._MAX_TURNS):
            response = self._engine._llm.complete(messages, tools=tools)  # noqa: SLF001
            last_text = extract_text(response)
            tool_uses = _tool_use_blocks(response)
            if not tool_uses:
                return last_text
            # Echo the assistant turn with its NATIVE tool_use block(s), then answer
            # with one tool_result block per block, keyed by the originating
            # tool_use_id -- the shape the Messages API requires after a tool_use stop.
            messages.append(assistant_blocks_message(response))
            results = [
                tool_result_block(_block_id(block), text, is_error=is_error)
                for block in tool_uses
                for text, is_error in (self._dispatch(block),)
            ]
            messages.append(user_blocks_message(results))
        return last_text

    def _dispatch(self, block: Any) -> tuple[str, bool]:
        """Dispatch one ``tool_use`` block to its seam; return ``(text, is_error)``.

        ``web_search`` and ``web_extract`` go to the extractor (their SSRF/extract
        errors are caught and returned as an error result, never raised); ``vault_read``
        reads a candidate page through the vault. An unknown tool name yields an error
        result so the model can recover. The boolean is the API ``is_error`` flag set
        onto the resulting ``tool_result`` block.
        """
        name = _block_name(block)
        tool_input = _block_input(block)
        if name == "web_search":
            return self._do_web_search(tool_input)
        if name == "web_extract":
            return self._do_web_extract(tool_input)
        if name == "vault_read":
            return self._do_vault_read(tool_input)
        return f"error: unknown tool {name!r}", True

    def _do_web_search(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Run a web search via the engine seam, recording that the web was used."""
        self.used_web = True
        query = tool_input.get("query")
        if not isinstance(query, str) or not query.strip():
            return "error: web_search requires a non-empty 'query' string", True
        try:
            hits = self._engine.web_search(query)
        except (SsrfError, ExtractError) as exc:
            return f"error: web_search failed: {exc}", True
        if not hits:
            return "web_search returned no results.", False
        lines = "\n".join(f"- {hit.title} <{hit.url}>: {hit.snippet}" for hit in hits)
        return lines, False

    def _do_web_extract(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Read a URL via the engine seam, capping the number of extractions.

        Beyond :attr:`ResearchEngine._max_web_reads` extractions the request is refused
        with an error result (still counted as web use) rather than dispatched, so the
        bounded-loop guarantee holds even if the model keeps asking.
        """
        self.used_web = True
        url = tool_input.get("url")
        if not isinstance(url, str) or not url.strip():
            return "error: web_extract requires a non-empty 'url' string", True
        if self._web_reads >= self._engine._max_web_reads:  # noqa: SLF001
            msg = "error: web_extract budget exhausted; answer with what you have."
            return msg, True
        self._web_reads += 1
        try:
            doc = self._engine.web_extract(url)
        except (SsrfError, ExtractError) as exc:
            return f"error: web_extract failed for {url}: {exc}", True
        self.read_web_urls.append((doc.source_url, doc.title))
        return f"# {doc.title}\n\n{doc.markdown}", False

    def _do_vault_read(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Read a vault page through the read-only vault seam.

        The page is read via the engine's vault facade; a path that escapes the vault or
        does not exist yields an error result (and is **not** recorded as read, so no
        citation is later fabricated for it). A successfully read path is recorded so
        the harness can mint its unfabricable citation afterwards.
        """
        path = tool_input.get("path")
        if not isinstance(path, str) or not path.strip():
            return "error: vault_read requires a non-empty 'path' string", True
        try:
            page = self._engine._vault.read_page(path)  # noqa: SLF001
        except VaultError as exc:
            return f"error: vault_read failed for {path}: {exc}", True
        self.read_vault_paths.append(page.path)
        return f"# {path}\n\n{page.body}", False

    def _initial_prompt(self, question: str) -> str:
        """Build the opening user turn: the question plus the candidate page list."""
        if self._candidates:
            listing = "\n".join(f"- {c.path} ({c.title})" for c in self._candidates)
            candidate_block = (
                "Candidate vault pages you may read with the vault_read tool:\n"
                f"{listing}\n\n"
            )
        else:
            candidate_block = (
                "No candidate vault pages were found for this question.\n\n"
            )
        return (
            "Answer the user's question. You may read their personal vault pages with "
            "the vault_read tool, and (when offered) search and read the public web "
            "with web_search/web_extract. Cite what you used. When you have enough, "
            "reply with the final answer and no further tool calls.\n\n"
            f"{candidate_block}"
            f"Question: {question}"
        )


# ---- response-shape helpers (tolerant of SDK objects and dict-shaped fakes) -------


def _tool_use_blocks(response: Any) -> list[Any]:
    """Return the ``tool_use`` content blocks of a response (objects or dicts).

    :func:`thoth.llm.extract_text` deliberately ignores ``tool_use`` blocks, so the
    research loop inspects ``response.content`` itself. Tolerant of the real SDK shape
    (blocks with a ``.type`` attribute) and a dict-shaped fake
    (``{'content': [{'type': 'tool_use', ...}]}``).

    Args:
        response: An Anthropic response object or a dict-shaped stand-in.

    Returns:
        The list of ``tool_use`` blocks, in order (possibly empty).
    """
    content = (
        response.get("content")
        if isinstance(response, dict)
        else getattr(response, "content", None)
    )
    if content is None:
        return []
    blocks: list[Any] = []
    for block in content:
        block_type = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if block_type == "tool_use":
            blocks.append(block)
    return blocks


def _block_name(block: Any) -> str:
    """Return a ``tool_use`` block's tool ``name`` as a string (``''`` when absent)."""
    name = (
        block.get("name") if isinstance(block, dict) else getattr(block, "name", None)
    )
    return name if isinstance(name, str) else ""


def _block_id(block: Any) -> str:
    """Return a ``tool_use`` block's ``id`` as a string (``''`` when absent).

    The id keys the matching ``tool_result`` block in the next user turn, so it must be
    carried through verbatim (the Messages API rejects a ``tool_result`` whose
    ``tool_use_id`` matches no prior ``tool_use`` block).
    """
    value = block.get("id") if isinstance(block, dict) else getattr(block, "id", None)
    return value if isinstance(value, str) else ""


def _block_input(block: Any) -> dict[str, Any]:
    """Return a ``tool_use`` block's ``input`` map (``{}`` when absent/ill-typed)."""
    value = (
        block.get("input") if isinstance(block, dict) else getattr(block, "input", None)
    )
    return value if isinstance(value, dict) else {}


def _slugify(text: str) -> str:
    """Build a vault slug from free text (lowercase, hyphenated, length-capped).

    Lowercases, replaces every run of non-alphanumerics with a single hyphen, drops
    leading/trailing hyphens, caps the word count and total length, and falls back to
    ``"query"`` when nothing usable remains -- so the result always satisfies
    :data:`thoth.vault.SLUG_RE` for a non-empty input of real words.

    Args:
        text: The free text to slugify (typically the question).

    Returns:
        A slug string suitable for :meth:`~thoth.vault.Vault.validate_slug`.
    """
    collapsed = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    if not collapsed:
        return "query"
    words = collapsed.split("-")[:_MAX_SLUG_WORDS]
    slug = "-".join(words)[:_MAX_SLUG_LEN].strip("-")
    return slug or "query"
