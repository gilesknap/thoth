"""FastMCP stdio server exposing the ``pkm_*`` tools over the closed vault surface.

This is the appliance's Model-Context-Protocol entry point (SPEC sections 2, 3 and 6).
It publishes seven tools -- :func:`pkm_ingest`, :func:`pkm_search`, :func:`pkm_ask`,
:func:`pkm_save_answer`, :func:`pkm_todos`, :func:`pkm_recent` and
:func:`pkm_write_page` -- each of which is a *pure delegation* to an already-validated
Phase 0-3 collaborator:

* ``pkm_ingest``   -> :meth:`thoth.ingest.Ingestor.ingest`
* ``pkm_search``   -> :meth:`thoth.query.QueryEngine.answer`
* ``pkm_ask``      -> :meth:`thoth.research.ResearchEngine.ask` (its reply surfaces the
  offer-to-save affordance)
* ``pkm_save_answer`` -> :meth:`thoth.research.ResearchEngine.save_answer` (the
  user-confirmed "save this answer" write, closing the section 7.1 loop)
* ``pkm_todos``    -> the canonical action scans on :class:`thoth.summary.SummaryEngine`
* ``pkm_recent``   -> :meth:`thoth.summary.SummaryEngine.recent_pages`
* ``pkm_write_page`` -> :meth:`thoth.vault.Vault.write_page`

The closed-surface promise (SPEC section 3) is preserved here exactly as it is in
``slack_app.py``: the LLM driving this server gets no shell and no arbitrary file
access. Every path is confined by the :class:`~thoth.vault.Vault`, binary bytes never
travel as base64 (``pkm_ingest`` rejects a base64/data-URI argument and accepts only
text, a URL, or a server-resolvable in-vault path), and the ``obsidian://`` links in
every reply are harness-built (unfabricable) by the underlying collaborators, never
invented by the model.

The tool *bodies* take an explicit :class:`ToolContext` -- the single injection bundle
of collaborators -- so they are ordinary, fully testable functions: a test exercises
each with fakes (recording the delegated call) and a real :class:`~thoth.vault.Vault`
over a temporary vault, with no ``mcp`` package and no live stdio. ``mcp`` / ``FastMCP``
is imported **lazily** inside :func:`build_server` and :func:`run` only, so importing
this module is always CI-safe -- only the standard library and ``thoth.*`` are imported
at module level. Each ``pkm_*`` function catches the relevant typed errors and returns a
``ToolResult(ok=False, ...)`` rather than raising into the MCP runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from thoth.config import Config
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.query import Citation, QueryEngine, QueryError, QueryResult
from thoth.research import AskResult, ResearchEngine, ResearchError, WebCitation
from thoth.vault import SchemaError, SlugError, Vault, VaultError

__all__ = [
    "SERVER_NAME",
    "TOOL_NAMES",
    "ToolContext",
    "ToolResult",
    "McpServerError",
    "pkm_ingest",
    "pkm_search",
    "pkm_ask",
    "pkm_save_answer",
    "pkm_todos",
    "pkm_recent",
    "pkm_write_page",
    "build_server",
    "run",
]

SERVER_NAME: str = "thoth"
"""The MCP server name advertised to the host (``FastMCP(SERVER_NAME)``)."""

TOOL_NAMES: tuple[str, ...] = (
    "pkm_ingest",
    "pkm_search",
    "pkm_ask",
    "pkm_save_answer",
    "pkm_todos",
    "pkm_recent",
    "pkm_write_page",
)
"""The exact tools :func:`build_server` registers (one per ``pkm_*`` function)."""

# The offer-to-save affordance appended to a blended answer (SPEC section 7.1 step 4):
# the model/host can call pkm_save_answer to file the answer as a queries/ page.
_SAVE_OFFER_TEXT: str = (
    "_To keep this, call pkm_save_answer with the question and this answer "
    "(plus any web source URLs)._"
)

# A base64/data-URI argument is refused by pkm_ingest: the closed surface accepts text,
# a URL, or a server-resolvable in-vault path only -- never inline binary (SPEC section
# 6). A leading data: URI is an unambiguous blob; a long, unbroken, base64-alphabet run
# with no spaces is a blob too (ordinary prose has spaces and is far shorter).
_DATA_URI_RE: re.Pattern[str] = re.compile(r"^\s*data:[^;,\s]*;base64,", re.IGNORECASE)
_BASE64_BLOB_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9+/]{256,}={0,2}$")


class McpServerError(Exception):
    """Raised for an MCP wiring failure (for example a missing collaborator)."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured outcome of a ``pkm_*`` tool, rendered by the MCP host.

    Attributes:
        ok: ``True`` on success, ``False`` when a typed collaborator error was caught
            and surfaced (the tool never raises into the MCP runtime).
        text: A Markdown reply (MCP style: ``[label](obsidian-uri)`` plus the plain
            vault path and the ``[[wikilink]]``) suitable for a chat host to display.
        data: A structured echo (paths, ``used_web`` and the like) for programmatic
            callers that want the fields rather than the rendered prose.
    """

    ok: bool
    text: str
    data: dict[str, Any]


@dataclass
class ToolContext:
    """The single injection bundle the ``pkm_*`` tools delegate through.

    Holds the frozen config and the already-constructed Phase 0-3 collaborators. The
    tool functions take this explicitly (the FastMCP wrappers in :func:`build_server`
    close over one instance and forward the same arguments), so each tool is a pure,
    testable delegation with no global state.

    Attributes:
        config: The frozen runtime configuration.
        vault: The path-confined read/write vault facade (the only disk surface).
        ingestor: The constructed ingest pipeline (``pkm_ingest``).
        query_engine: The vault-only retrieval engine (``pkm_search``).
        research: The blended web+vault Q&A engine (``pkm_ask``).
    """

    config: Config
    vault: Vault
    ingestor: Ingestor
    query_engine: QueryEngine
    research: ResearchEngine


# ---- citation / report rendering (MCP Markdown, mirrors slack_app's mrkdwn) --------


def _render_citation(citation: Citation) -> str:
    """Render one vault citation as Markdown: link, plain path, and ``[[wikilink]]``.

    Emits ``[title](obsidian-uri)`` (the Markdown link form), then the plain
    vault-relative path and the ``[[wikilink]]`` on the same line, so the reference is
    still usable when a host will not make the custom ``obsidian://`` scheme clickable
    (SPEC Appendix). The link target is taken verbatim from the harness-built
    :class:`~thoth.query.Citation`; this never constructs an ``obsidian://`` URI itself.
    """
    label = citation.title or citation.path
    return f"[{label}]({citation.obsidian_uri}) - `{citation.path}` {citation.wikilink}"


def _render_query_result(result: QueryResult) -> str:
    """Render a composed answer plus its vault citations as a Markdown block."""
    lines = [result.answer.strip()]
    if result.citations:
        lines.append("")
        lines.append("**Sources:**")
        lines.extend(f"- {_render_citation(c)}" for c in result.citations)
    else:
        lines.append("")
        lines.append("_No vault sources cited._")
    return "\n".join(lines)


def _render_ask_result(result: AskResult, *, offer_save: bool = True) -> str:
    """Render a blended Q&A answer with both vault and web citations in Markdown.

    When ``offer_save`` is set (and the answer is non-empty) the offer-to-save line is
    appended, surfacing the :func:`pkm_save_answer` affordance (SPEC section 7.1 step 4)
    so the host can file the answer as a ``queries/`` page on confirmation.
    """
    lines = [result.answer.strip()]
    if result.vault_citations or result.web_citations:
        lines.append("")
        lines.append("**Sources:**")
        lines.extend(f"- {_render_citation(c)}" for c in result.vault_citations)
        for web in result.web_citations:
            label = web.title or web.url
            lines.append(f"- [{label}]({web.url}) - {web.url}")
    else:
        lines.append("")
        lines.append("_No sources cited._")
    if offer_save and result.answer.strip():
        lines.append("")
        lines.append(_SAVE_OFFER_TEXT)
    return "\n".join(lines)


def _render_ingest_report(report: IngestReport) -> str:
    """Render a one-to-two-line capture confirmation in Markdown.

    Names what was filed (the curated page paths, or the raw/asset paths when no curated
    page was written) and lists every harness-built ``obsidian://`` link and
    ``[[wikilink]]`` the report carries (SPEC step 8). A conflict is surfaced fail-loud
    with the conflicting path, never swallowed (SPEC section 10). A ``deferred`` capture
    (raw persisted but the LLM was unavailable for curation) is surfaced as a
    partial-success note naming the held raw page (SPEC section 6).
    """
    if report.conflict:
        detail = report.message or "a vault conflict blocked the sync"
        return f"**Vault conflict** - {detail}. Content was filed locally."

    if report.deferred:
        held = report.raw_paths or report.asset_paths
        where = ", ".join(f"`{path}`" for path in held) or "the inbox"
        note = report.message or "curation deferred -- LLM unavailable"
        return f"Saved raw to {where}. {note}"

    filed = report.page_paths or report.raw_paths or report.asset_paths
    if filed:
        head = "Filed " + ", ".join(f"`{path}`" for path in filed)
    else:
        head = "Nothing new to file"
    if not report.committed:
        head += " (not yet committed)"

    parts = [head]
    refs: list[str] = []
    for uri, wikilink in zip(report.obsidian_links, report.wikilinks, strict=True):
        refs.append(f"[open]({uri}) {wikilink}")
    if refs:
        parts.append(" - ".join(refs))
    if report.message and not report.conflict:
        parts.append(report.message)
    return "\n".join(parts)


def _looks_like_base64_blob(value: str) -> bool:
    """Return ``True`` if ``value`` looks like inline binary (data-URI or base64 blob).

    The closed surface (SPEC section 6) never accepts inline binary: a capture carries
    text, a URL, or a server-resolvable in-vault path, and any image/PDF/audio is
    fetched server-side. A leading ``data:...;base64,`` URI is an unambiguous blob; so
    is a long, unbroken run of base64-alphabet characters with no whitespace (ordinary
    prose has spaces and is far shorter).
    """
    if _DATA_URI_RE.match(value):
        return True
    stripped = value.strip()
    return bool(_BASE64_BLOB_RE.fullmatch(stripped))


# ---- the pkm_* tool bodies (pure delegations on a ToolContext) ---------------------


def pkm_ingest(
    ctx: ToolContext,
    *,
    text: str | None = None,
    url: str | None = None,
    path: str | None = None,
) -> ToolResult:
    """Capture text, a URL, or a server-resolvable in-vault path into the vault.

    Builds a :class:`~thoth.ingest.Capture` with ``source='mcp'`` and delegates to
    :meth:`thoth.ingest.Ingestor.ingest`, rendering the resulting report. The closed
    surface is enforced before any work: a base64/data-URI argument is refused (binary
    never travels inline -- SPEC section 6), and a ``path`` is confined to the vault
    root via the :class:`~thoth.vault.Vault` (a server-resolvable in-vault path only)
    before the ingestor is called. A :class:`~thoth.ingest.IngestError` or a conflict is
    surfaced as ``ToolResult(ok=False, ...)`` and never raised into the MCP runtime.

    Args:
        ctx: The injected collaborator bundle.
        text: Inline text/markdown to capture, if any.
        url: A URL to fetch server-side, if any.
        path: A vault-relative path the server can read (image/PDF/audio), if any.

    Returns:
        A :class:`ToolResult`: ``ok=True`` with the rendered report on success, else
        ``ok=False`` with the rejection or error message.
    """
    provided = [
        name for name, value in (("text", text), ("url", url), ("path", path)) if value
    ]
    if not provided:
        return ToolResult(
            ok=False,
            text="Provide exactly one of text, url, or path to ingest.",
            data={"provided": provided},
        )
    for value in (text, url, path):
        if value is not None and _looks_like_base64_blob(value):
            return ToolResult(
                ok=False,
                text=(
                    "Inline binary (base64/data-URI) is not accepted. Send text, a "
                    "URL, or a server-resolvable in-vault path instead."
                ),
                data={"rejected": "base64"},
            )

    resolved_path: Any = None
    if path is not None:
        if not ctx.vault.is_inside(path):
            return ToolResult(
                ok=False,
                text=f"Path is outside the vault and was rejected: `{path}`",
                data={"rejected": "path_confinement", "path": path},
            )
        resolved_path = ctx.vault.resolve(path)

    capture = Capture(
        text=text,
        url=url,
        path=resolved_path,
        source="mcp",
    )
    try:
        report = ctx.ingestor.ingest(capture)
    except IngestError as exc:
        return ToolResult(ok=False, text=f"Could not file that: {exc}", data={})
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Vault rejected the capture: {exc}", data={})

    return ToolResult(
        ok=not report.conflict,
        text=_render_ingest_report(report),
        data={
            "page_paths": list(report.page_paths),
            "raw_paths": list(report.raw_paths),
            "asset_paths": list(report.asset_paths),
            "obsidian_links": list(report.obsidian_links),
            "wikilinks": list(report.wikilinks),
            "committed": report.committed,
            "conflict": report.conflict,
            "deferred": report.deferred,
        },
    )


def pkm_search(ctx: ToolContext, *, query: str, max_pages: int = 5) -> ToolResult:
    """Run a fast, vault-only lookup and return the answer with vault citations.

    Delegates to :meth:`thoth.query.QueryEngine.answer`, rendering the composed answer
    plus its harness-built citations in MCP Markdown style. A
    :class:`~thoth.query.QueryError` (for example no matching page) is surfaced as
    ``ToolResult(ok=False, ...)``.

    Args:
        ctx: The injected collaborator bundle.
        query: The natural-language query.
        max_pages: The maximum number of vault pages to cite.

    Returns:
        A :class:`ToolResult` with the rendered answer or the error message.
    """
    try:
        result = ctx.query_engine.answer(query, max_pages=max_pages)
    except QueryError as exc:
        return ToolResult(ok=False, text=f"Could not answer that: {exc}", data={})
    return ToolResult(
        ok=True,
        text=_render_query_result(result),
        data={
            "answer": result.answer,
            "citations": [c.path for c in result.citations],
            "used_recall": result.used_recall,
        },
    )


def pkm_ask(ctx: ToolContext, *, question: str, force_web: bool = False) -> ToolResult:
    """Answer a question by blending the vault with the web (when the model chooses to).

    Delegates to :meth:`thoth.research.ResearchEngine.ask`, forwarding ``force_web``,
    and renders the harness-built vault citations plus the web URLs the model actually
    read. ``used_web`` and the web sources surface in the result data. A
    :class:`~thoth.research.ResearchError` is surfaced as ``ToolResult(ok=False, ...)``.

    Args:
        ctx: The injected collaborator bundle.
        question: The natural-language question.
        force_web: When true, the web is consulted even for a vault-answerable question
            (a leading ``research:`` marker in ``question`` has the same effect).

    Returns:
        A :class:`ToolResult` with the rendered blended answer or the error message.
    """
    try:
        result = ctx.research.ask(question, force_web=force_web)
    except ResearchError as exc:
        return ToolResult(ok=False, text=f"Could not answer that: {exc}", data={})
    return ToolResult(
        ok=True,
        text=_render_ask_result(result),
        data={
            "answer": result.answer,
            "vault_citations": [c.path for c in result.vault_citations],
            "web_citations": [w.url for w in result.web_citations],
            "used_web": result.used_web,
        },
    )


def pkm_save_answer(
    ctx: ToolContext,
    *,
    question: str,
    answer: str,
    web_sources: list[str] | None = None,
    vault_paths: list[str] | None = None,
    slug: str | None = None,
) -> ToolResult:
    """File a previously-given blended answer as a ``queries/<slug>.md`` page.

    This closes the offer-to-save loop (SPEC section 7.1 step 4): the host calls it
    after a :func:`pkm_ask` answer the user wants to keep. It reconstructs an
    :class:`~thoth.research.AskResult` from the supplied ``answer`` plus any
    ``web_sources`` URLs and ``vault_paths`` (each rebuilt into an unfabricable
    :class:`~thoth.query.Citation` via the query engine -- a path that does not resolve
    is silently dropped, never fabricated), then writes the page through the validated
    :meth:`~thoth.research.ResearchEngine.save_answer` (which confines the path to
    ``queries/`` and validates the slug). A :class:`~thoth.research.ResearchError` (bad
    slug or vault rejection) is surfaced as ``ToolResult(ok=False, ...)``; nothing is
    written on rejection.

    Args:
        ctx: The injected collaborator bundle.
        question: The original question (used for the title and default slug).
        answer: The answer prose to persist.
        web_sources: The web source URLs to record (``sources`` frontmatter + bullets).
        vault_paths: Vault-relative page paths to cite as ``[[wikilinks]]``; each is
            re-validated through the query engine and dropped if it does not resolve.
        slug: An explicit slug; defaults to a slugified ``question``.

    Returns:
        A :class:`ToolResult` with the written path on success, else the rejection.
    """
    if not answer.strip():
        return ToolResult(ok=False, text="Refusing to save an empty answer.", data={})
    web_citations = [
        WebCitation(url=url, title="") for url in (web_sources or []) if url
    ]
    vault_citations: list[Citation] = []
    for path in vault_paths or []:
        try:
            vault_citations.append(ctx.query_engine.build_citation(path))
        except VaultError:
            continue
    result = AskResult(
        answer=answer,
        vault_citations=vault_citations,
        web_citations=web_citations,
        used_web=bool(web_citations),
    )
    try:
        rel = ctx.research.save_answer(question, result, slug=slug)
    except ResearchError as exc:
        return ToolResult(ok=False, text=f"Could not save that: {exc}", data={})

    uri = ctx.vault.obsidian_uri(rel)
    wikilink = f"[[{PurePosixPath(rel).stem}]]"
    return ToolResult(
        ok=True,
        text=f"Saved [{rel}]({uri}) - `{rel}` {wikilink}",
        data={"path": rel, "obsidian_uri": uri, "wikilink": wikilink},
    )


def pkm_todos(ctx: ToolContext, *, include_done: bool = False) -> ToolResult:
    """List open (and optionally done) actions from ``actions/*.md`` frontmatter.

    Reuses the canonical action scans on :class:`thoth.summary.SummaryEngine` (so the
    todo/overdue logic lives in exactly one place): open actions come from
    :meth:`~thoth.summary.SummaryEngine.open_actions`, with overdue items flagged via
    :meth:`~thoth.summary.SummaryEngine.overdue_actions`. Each item is rendered with its
    status, due date, priority, and ``[[wikilink]]``. Done/cancelled actions are left
    out unless ``include_done`` is true.

    Args:
        ctx: The injected collaborator bundle.
        include_done: When true, also list actions whose status is not open (rendered as
            a separate "Done/closed" section).

    Returns:
        A :class:`ToolResult` listing the actions (always ``ok=True``; an empty vault
        yields a "no open actions" note).
    """
    from thoth.summary import SummaryEngine

    engine = SummaryEngine(ctx.config, ctx.vault)
    open_actions = engine.open_actions()
    today = engine.today
    overdue_paths = {
        item.path
        for item in open_actions
        if item.due_date is not None and item.due_date < today
    }

    lines: list[str] = ["**Open actions:**"]
    if open_actions:
        for item in open_actions:
            lines.append(_render_action(item, overdue=item.path in overdue_paths))
    else:
        lines.append("- _No open actions._")

    closed = engine.closed_actions() if include_done else []
    if closed:
        lines.append("")
        lines.append("**Done/closed:**")
        lines.extend(
            f"- {item.wikilink} {item.title} (status: {item.status})"
            for item in closed
        )

    return ToolResult(
        ok=True,
        text="\n".join(lines),
        data={
            "open": [item.path for item in open_actions],
            "overdue": sorted(overdue_paths),
            "closed": [item.wikilink for item in closed],
        },
    )


def _render_action(item: Any, *, overdue: bool) -> str:
    """Render one action item as a Markdown bullet with status/due/priority/wikilink."""
    bits: list[str] = [f"status: {item.status}"]
    if item.priority:
        bits.append(f"priority: {item.priority}")
    if item.due_date is not None:
        due = item.due_date.isoformat()
        bits.append(f"due: {due}{' (OVERDUE)' if overdue else ''}")
    return f"- {item.wikilink} {item.title} ({', '.join(bits)})"


def pkm_recent(ctx: ToolContext, *, days: int = 7, limit: int = 20) -> ToolResult:
    """List recently created/updated curated pages from their frontmatter dates.

    Reuses :meth:`thoth.summary.SummaryEngine.recent_pages` (the canonical recent scan)
    so the recency logic lives in one place; each page is rendered with a harness-built
    ``obsidian://`` link (via :meth:`thoth.vault.Vault.obsidian_uri`), plain path, and
    ``[[wikilink]]``. The result is capped at ``limit`` pages.

    Args:
        ctx: The injected collaborator bundle.
        days: The recency window in days (a page counts if its frontmatter date falls
            within this many days of today).
        limit: The maximum number of pages to list.

    Returns:
        A :class:`ToolResult` listing the recent pages (always ``ok=True``).
    """
    from thoth.summary import SummaryEngine

    engine = SummaryEngine(ctx.config, ctx.vault)
    pages = engine.recent_pages(days=days)[:limit]

    lines: list[str] = [f"**Recent pages (last {days} day(s)):**"]
    rendered: list[dict[str, str]] = []
    if pages:
        for page in pages:
            uri = ctx.vault.obsidian_uri(page.path)
            updated = page.updated.isoformat() if page.updated is not None else "?"
            lines.append(
                f"- [{page.title or page.path}]({uri}) - `{page.path}` "
                f"{page.wikilink} ({page.page_type}, {updated})"
            )
            rendered.append({"path": page.path, "obsidian_uri": uri})
    else:
        lines.append("- _No recent pages._")

    return ToolResult(
        ok=True,
        text="\n".join(lines),
        data={"pages": rendered, "days": days, "limit": limit},
    )


def pkm_write_page(
    ctx: ToolContext,
    *,
    folder: str,
    slug: str,
    frontmatter: dict[str, Any],
    body: str,
    today: date | None = None,
) -> ToolResult:
    """Write a page through the validated vault surface (the low-level escape hatch).

    Delegates straight to :meth:`thoth.vault.Vault.write_page`, which performs the full
    folder-by-type, slug, source, and confinement validation plus secret redaction and
    an atomic write. On success the written vault-relative path is returned with a
    harness-built ``obsidian://`` link and ``[[wikilink]]``. A
    :class:`~thoth.vault.SchemaError` (bad folder/type or missing field) or
    :class:`~thoth.vault.SlugError` (bad/escaping slug) is surfaced as
    ``ToolResult(ok=False, ...)`` and nothing is written.

    Args:
        ctx: The injected collaborator bundle.
        folder: A top-level vault folder (key of ``thoth.vault.FOLDER_TYPE_CONTRACT``).
        slug: The page slug (validated by :meth:`thoth.vault.Vault.validate_slug`).
        frontmatter: The page frontmatter (must carry a valid ``type`` and ``source``).
        body: The page body markdown.
        today: The date to stamp; defaults to today (kept injectable for tests).

    Returns:
        A :class:`ToolResult` with the written path on success, else the rejection.
    """
    try:
        rel = ctx.vault.write_page(folder, slug, frontmatter, body, today=today)
    except (SchemaError, SlugError) as exc:
        return ToolResult(ok=False, text=f"Vault rejected the page: {exc}", data={})
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Vault rejected the page: {exc}", data={})

    uri = ctx.vault.obsidian_uri(rel)
    wikilink = f"[[{PurePosixPath(rel).stem}]]"
    return ToolResult(
        ok=True,
        text=f"Wrote [{rel}]({uri}) - `{rel}` {wikilink}",
        data={"path": rel, "obsidian_uri": uri, "wikilink": wikilink},
    )


# ---- the FastMCP server (mcp imported lazily here only) ----------------------------


def build_server(ctx: ToolContext) -> Any:
    """Lazily import ``FastMCP``, build the server, and register the ``pkm_*`` tools.

    ``mcp`` is imported **inside** this function so module import stays CI-safe. A
    :class:`FastMCP` named :data:`SERVER_NAME` is created and one tool per
    :data:`TOOL_NAMES` is registered, each forwarding to the matching ``pkm_*`` function
    bound to ``ctx`` (so the registered callables carry the same keyword arguments the
    pure functions do). The server is returned but **not** started -- :func:`run` does
    that.

    Args:
        ctx: The injected collaborator bundle every registered tool delegates through.

    Returns:
        The configured ``FastMCP`` instance (typed ``Any`` to avoid a top-level import
        of the optional ``mcp`` dependency).
    """
    from mcp.server.fastmcp import FastMCP

    server: Any = FastMCP(SERVER_NAME)

    @server.tool(name="pkm_ingest")
    def _ingest(
        text: str | None = None, url: str | None = None, path: str | None = None
    ) -> ToolResult:
        """Capture text, a URL, or a server-resolvable in-vault path into the vault."""
        return pkm_ingest(ctx, text=text, url=url, path=path)

    @server.tool(name="pkm_search")
    def _search(query: str, max_pages: int = 5) -> ToolResult:
        """Run a fast, vault-only lookup and return the answer with citations."""
        return pkm_search(ctx, query=query, max_pages=max_pages)

    @server.tool(name="pkm_ask")
    def _ask(question: str, force_web: bool = False) -> ToolResult:
        """Answer a question by blending the vault with the web when chosen."""
        return pkm_ask(ctx, question=question, force_web=force_web)

    @server.tool(name="pkm_save_answer")
    def _save_answer(
        question: str,
        answer: str,
        web_sources: list[str] | None = None,
        vault_paths: list[str] | None = None,
        slug: str | None = None,
    ) -> ToolResult:
        """File a previously-given blended answer as a queries/ page."""
        return pkm_save_answer(
            ctx,
            question=question,
            answer=answer,
            web_sources=web_sources,
            vault_paths=vault_paths,
            slug=slug,
        )

    @server.tool(name="pkm_todos")
    def _todos(include_done: bool = False) -> ToolResult:
        """List open (and optionally done) actions from the vault frontmatter."""
        return pkm_todos(ctx, include_done=include_done)

    @server.tool(name="pkm_recent")
    def _recent(days: int = 7, limit: int = 20) -> ToolResult:
        """List recently created/updated curated pages from their frontmatter dates."""
        return pkm_recent(ctx, days=days, limit=limit)

    @server.tool(name="pkm_write_page")
    def _write_page(
        folder: str, slug: str, frontmatter: dict[str, Any], body: str
    ) -> ToolResult:
        """Write a page through the validated vault surface (the escape hatch)."""
        return pkm_write_page(
            ctx, folder=folder, slug=slug, frontmatter=frontmatter, body=body
        )

    return server


def run(config: Config, ctx: ToolContext | None = None) -> None:
    """Wire a real :class:`ToolContext` (if needed) and serve over MCP stdio.

    This is the production entry point (``thoth mcp``). When ``ctx`` is ``None`` it
    wires the full collaborator graph -- a :class:`~thoth.vault.Vault`, an
    :class:`~thoth.llm.LLM`, an :class:`~thoth.extract.Extractor`, a
    :class:`~thoth.hindsight.Hindsight`, a :class:`~thoth.git_sync.GitSync`, an
    :class:`~thoth.ingest.Ingestor`, a :class:`~thoth.query.QueryEngine`, and a
    :class:`~thoth.research.ResearchEngine` (the graph ``slack_app.run`` builds, plus
    research) -- then builds the server via :func:`build_server` and calls its stdio run
    loop. The collaborator construction and the lazy ``mcp`` import happen only here, so
    importing this module stays light. This is never unit-tested live (CI has no stdio).

    Args:
        config: The frozen runtime config.
        ctx: An already-wired context (for tests/embedding); built from ``config`` when
            ``None``.
    """
    if ctx is None:
        from thoth.budget import make_budget_guard
        from thoth.extract import Extractor
        from thoth.git_sync import GitSync
        from thoth.hindsight import Hindsight
        from thoth.llm import LLM

        vault = Vault(config)
        # The daily cost guard (issue #16): one shared cap over the Anthropic +
        # Hindsight calls, persisted in state.db. MCP has no Slack target, so it blocks
        # silently (no alerter); the cap still defers spend once reached.
        guard = make_budget_guard(config)
        llm = LLM(config, guard=guard)
        extractor = Extractor(config)
        hindsight = Hindsight(config, guard=guard)
        git = GitSync(config)
        # SCHEMA.md as the curate-call system_extra so curated pages match the live
        # per-type schema (mirrors thoth.__main__._build_graph).
        ingestor = Ingestor(
            config, vault, llm, extractor, hindsight, git, schema_md=vault.schema_md()
        )
        query_engine = QueryEngine(config, vault, hindsight, llm)
        research = ResearchEngine(config, vault, query_engine, extractor, llm)
        ctx = ToolContext(
            config=config,
            vault=vault,
            ingestor=ingestor,
            query_engine=query_engine,
            research=research,
        )

    server = build_server(ctx)
    server.run(transport="stdio")
