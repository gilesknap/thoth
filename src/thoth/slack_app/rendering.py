"""The ``mrkdwn`` renderers for query answers and capture confirmations."""

from __future__ import annotations

from thoth.ingest import IngestReport
from thoth.query import Citation, QueryResult
from thoth.render import render_vault_ref


def render_citation(citation: Citation) -> str:
    """Renders one citation as the concise shared Slack reference (issue #53).

    Delegates to :func:`thoth.render.render_vault_ref`, emitting a title-only clickable
    ``<obsidian-uri|title>`` with no trailing path (issue #63). The link target is taken
    verbatim from the citation, so this never constructs an ``obsidian://`` URI itself,
    and the dead ``[[wikilink]]`` is no longer shown because it is un-clickable in
    Slack.

    Args:
        citation: A harness-built citation handle

    Returns:
        A single ``mrkdwn`` line for the citation
    """
    return render_vault_ref(
        obsidian_uri=citation.obsidian_uri,
        title=citation.title or citation.path,
        path=citation.path,
    )


def render_query_result(result: QueryResult) -> str:
    """Renders a composed answer plus its citation list as a ``mrkdwn`` block.

    The prose comes first, then a ``Sources:`` list with one :func:`render_citation`
    line per cited page. The cited set is the pages the model said it actually used
    (issue #34's ``USED:`` line, parsed in :mod:`thoth.query`), so the list reflects
    what the answer drew on rather than the whole candidate set. With no citations the
    prose stands alone and no trailing note is added (issue #53).

    Args:
        result: The query result to render

    Returns:
        A ``mrkdwn`` string ready for ``chat.postMessage``
    """
    lines = [result.answer.strip()]
    if result.citations:
        lines.append("")
        lines.append("*Sources:*")
        lines.extend(f"- {render_citation(c)}" for c in result.citations)
    return "\n".join(lines)


def render_ingest_report(report: IngestReport) -> str:
    """Renders a one or two line capture confirmation in ``mrkdwn``.

    Names what was filed, with one concise reference per curated page under a ``Filed N
    page(s):`` header and no trailing path (issue #63). When no curated page was written
    the header names the raw or asset paths directly.

    A conflict is surfaced fail-loud with the conflicting path and never swallowed (SPEC
    section 10). A deferred capture, where the raw persisted but the LLM was
    unavailable, becomes a partial-success note naming the held raw page, so the user
    knows the item is safe and will be re-curated (SPEC section 6).

    Args:
        report: The structured ingest outcome

    Returns:
        A concise ``mrkdwn`` confirmation, conflict or deferred string
    """
    if report.conflict:
        detail = report.message or "a vault conflict blocked the sync"
        return f":warning: *Vault conflict* - {detail}. Content was filed locally."

    if report.deferred:
        held = report.raw_paths or report.asset_paths
        where = ", ".join(f"`{path}`" for path in held) or "the inbox"
        note = report.message or "curation deferred -- LLM unavailable"
        return f":hourglass_flowing_sand: Saved raw to {where}. {note}"

    parts: list[str] = []
    if report.page_paths:
        count = len(report.page_paths)
        head = f"Filed {count} page(s):"
        if not report.committed:
            head += " (not yet committed)"
        parts.append(head)
        # One title-only <uri|title> ref per curated page (issue #63). Titles runs
        # parallel to page_paths and obsidian_links, filled in upstream when missing
        for path, uri, title in zip(
            report.page_paths,
            report.obsidian_links,
            report.titles,
            strict=False,
        ):
            parts.append(render_vault_ref(obsidian_uri=uri, title=title, path=path))
    else:
        filed = report.raw_paths or report.asset_paths
        if filed:
            head = "Filed " + ", ".join(f"`{path}`" for path in filed)
        else:
            head = "Nothing new to file"
        if not report.committed:
            head += " (not yet committed)"
        parts.append(head)

    if report.message:
        parts.append(report.message)
    return "\n".join(parts)
