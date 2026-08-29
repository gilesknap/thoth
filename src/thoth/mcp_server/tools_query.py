"""The read-only tool bodies: ``pkm_search``, ``pkm_todos`` and ``pkm_recent``."""

from __future__ import annotations

from thoth.query import QueryError

from .context import ToolContext, ToolResult
from .render import _ref, _render_action, _render_query_result


def pkm_search(
    ctx: ToolContext,
    *,
    query: str,
    max_pages: int = 5,
    search_keywords: list[str] | None = None,
) -> ToolResult:
    """Runs a fast, vault-only lookup and returns the answer with its citations.

    Delegates to :meth:`thoth.query.QueryEngine.answer`, rendering the composed answer
    plus its harness-built citations in MCP Markdown style. A
    :class:`~thoth.query.QueryError` is surfaced as ``ToolResult(ok=False, ...)``. The
    structured ``data`` also carries one ``{path, methods, rank}`` provenance entry per
    consulted page, recording which retrieval methods surfaced it in the RRF blend
    (issue #143).

    Args:
        ctx: The injected collaborator bundle
        query: The natural-language query
        max_pages: The maximum number of vault pages to cite
        search_keywords: De-pluralised, synonym-expanded keywords seeding the lexical
            grep. The grep matches whole words, so a plural query misses singular page
            content unless the calling model supplies the singular here

    Returns:
        The rendered answer, or the error message
    """
    try:
        result = ctx.query_engine.answer(
            query, max_pages=max_pages, search_terms=search_keywords
        )
    except QueryError as exc:
        return ToolResult(ok=False, text=f"Could not answer that: {exc}", data={})
    return ToolResult(
        ok=True,
        text=_render_query_result(result),
        data={
            "answer": result.answer,
            "citations": [c.path for c in result.citations],
            "used_recall": result.used_recall,
            # Per-page provenance from the RRF blend, so a programmatic caller sees
            # the grep and recall attribution behind the answer (issue #143)
            "provenance": [
                {"path": p.path, "methods": list(p.methods), "rank": p.rank}
                for p in result.provenance
            ],
        },
    )


def pkm_todos(ctx: ToolContext, *, include_done: bool = False) -> ToolResult:
    """Lists open, and optionally done, actions from ``actions/*.md`` frontmatter.

    Reuses the canonical action scans on :class:`thoth.summary.SummaryEngine`, so the
    todo and overdue logic lives in exactly one place. Each item renders in the MCP
    citation style the other tools use, followed by its status, due date and priority.
    Done and cancelled actions are left out unless ``include_done`` is true.

    Args:
        ctx: The injected collaborator bundle
        include_done: When true, add a separate section for non-open actions

    Returns:
        The listed actions, always ``ok=True``, with a note when the vault is empty
    """
    from thoth.summary import SummaryEngine

    engine = SummaryEngine(ctx.config, ctx.vault)
    open_actions = engine.open_actions()
    overdue_paths = {item.path for item in engine.overdue_actions()}

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
            f"- {_ref(item.title, item.obsidian_uri, item.path, item.wikilink)} "
            f"(status: {item.status})"
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


def pkm_recent(ctx: ToolContext, *, days: int = 7, limit: int = 20) -> ToolResult:
    """Lists recently created or updated pages from their frontmatter dates.

    Reuses :meth:`thoth.summary.SummaryEngine.recent_pages` so the recency logic lives
    in one place, and renders each page with a harness-built ``obsidian://`` link, its
    plain path and its wikilink.

    Args:
        ctx: The injected collaborator bundle
        days: The recency window, matched against the page's frontmatter date
        limit: The maximum number of pages to list

    Returns:
        The recent pages, always ``ok=True``
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
            ref = _ref(page.title or page.path, uri, page.path, page.wikilink)
            lines.append(f"- {ref} ({page.page_type}, {updated})")
            rendered.append({"path": page.path, "obsidian_uri": uri})
    else:
        lines.append("- _No recent pages._")

    return ToolResult(
        ok=True,
        text="\n".join(lines),
        data={"pages": rendered, "days": days, "limit": limit},
    )
