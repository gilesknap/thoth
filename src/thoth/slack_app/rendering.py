"""The ``mrkdwn`` renderers for query answers and capture confirmations."""

from __future__ import annotations

from thoth.ingest import IngestReport
from thoth.query import Citation, QueryResult
from thoth.render import render_vault_ref


def render_citation(citation: Citation) -> str:
    """Render one citation as the concise shared Slack reference (issue #53).

    Delegates to :func:`thoth.render.render_vault_ref`, emitting a title-only clickable
    ``<obsidian-uri|title>`` link over the harness-built ``obsidian://`` link, with no
    trailing path (issue #63). The link target is taken verbatim from the
    :class:`~thoth.query.Citation`; this function never constructs an ``obsidian://``
    URI itself, and the dead ``[[wikilink]]`` is no longer shown (it is un-clickable in
    Slack).

    Args:
        citation: A harness-built citation handle.

    Returns:
        A single ``mrkdwn`` line for the citation.
    """
    return render_vault_ref(
        obsidian_uri=citation.obsidian_uri,
        title=citation.title or citation.path,
        path=citation.path,
    )


def render_query_result(result: QueryResult) -> str:
    """Render a composed answer plus its citation list as a ``mrkdwn`` block.

    The answer prose comes first, followed by a ``Sources:`` list with one
    :func:`render_citation` line per cited page (SPEC Appendix worked example). The
    cited set is the pages the model said it actually used (issue #34's ``USED:`` line,
    parsed in :mod:`thoth.query`), so the list reflects what the answer drew on rather
    than the whole retrieval candidate set. When the answer has no citations the prose
    stands alone -- no trailing note is added (issue #53).

    Args:
        result: The query result to render.

    Returns:
        A ``mrkdwn`` string ready for ``chat.postMessage``.
    """
    lines = [result.answer.strip()]
    if result.citations:
        lines.append("")
        lines.append("*Sources:*")
        lines.extend(f"- {render_citation(c)}" for c in result.citations)
    return "\n".join(lines)


def render_ingest_report(report: IngestReport) -> str:
    """Render a one-to-two-line capture confirmation in ``mrkdwn``.

    Names what was filed and renders one concise shared reference per curated page
    (issue #63): a ``Filed N page(s):`` header followed by a title-only clickable
    ``<obsidian-uri|title>`` line per page (no trailing path). When no curated page was
    written the header names the raw/asset paths directly. A
    :attr:`~thoth.ingest.IngestReport.conflict` is surfaced fail-loud (SPEC section 10)
    with the conflicting path, never swallowed. A
    :attr:`~thoth.ingest.IngestReport.deferred` capture (raw persisted but the LLM was
    unavailable for curation) is surfaced as a partial-success note naming the held raw
    page, so the user knows the item is safe and will be re-curated (SPEC section 6).

    Args:
        report: The structured ingest outcome.

    Returns:
        A concise ``mrkdwn`` confirmation (or conflict / deferred) string.
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
        # One title-only <uri|title> ref per curated page (issue #63: no trailing path).
        # ``titles`` runs parallel to page_paths / obsidian_links (the slug-derived
        # title is filled in upstream when missing).
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
