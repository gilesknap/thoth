"""Citation / report rendering in MCP Markdown (mirrors slack_app's mrkdwn)."""

from __future__ import annotations

from itertools import zip_longest
from typing import Any

from thoth.ingest import IngestReport
from thoth.query import Citation, QueryResult


def _ref(label: str, uri: str, path: str, wikilink: str) -> str:
    """Renders the MCP reference triple: link, plain path and ``[[wikilink]]``.

    Emits ``[label](obsidian-uri)`` followed by the plain vault-relative path and the
    wikilink on the same line, so the reference stays usable when a host will not make
    the custom ``obsidian://`` scheme clickable (SPEC Appendix). The link target is
    always harness-built by a collaborator; this never constructs a URI itself.
    """
    return f"[{label}]({uri}) - `{path}` {wikilink}"


def _render_citation(citation: Citation) -> str:
    """Renders one vault citation as Markdown, taking the target verbatim.

    The link target comes from the harness-built :class:`~thoth.query.Citation`, and the
    shape is :func:`_ref`'s.
    """
    label = citation.title or citation.path
    return _ref(label, citation.obsidian_uri, citation.path, citation.wikilink)


def _render_query_result(result: QueryResult) -> str:
    """Renders a composed answer plus its vault citations as a Markdown block."""
    lines = [result.answer.strip()]
    if result.citations:
        lines.append("")
        lines.append("**Sources:**")
        lines.extend(f"- {_render_citation(c)}" for c in result.citations)
    else:
        lines.append("")
        lines.append("_No vault sources cited._")
    return "\n".join(lines)


def _render_ingest_report(report: IngestReport) -> str:
    """Renders a one or two line capture confirmation in Markdown.

    Names what was filed, the curated page paths or the raw and asset paths when no
    curated page was written, and lists every harness-built ``obsidian://`` link and
    ``[[wikilink]]`` the report carries (SPEC step 8). A conflict is surfaced fail-loud
    with the conflicting path and never swallowed (SPEC section 10). A deferred capture,
    where the raw persisted but the LLM was unavailable, becomes a partial-success note
    naming the held raw page (SPEC section 6).
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
    for uri, wikilink in zip_longest(report.obsidian_links, report.wikilinks):
        if uri is not None and wikilink is not None:
            refs.append(f"[open]({uri}) {wikilink}")
        elif uri is not None:
            refs.append(f"[open]({uri})")
        elif wikilink is not None:
            refs.append(wikilink)
    if refs:
        parts.append(" - ".join(refs))
    if report.message:
        parts.append(report.message)
    return "\n".join(parts)


def _render_action(item: Any, *, overdue: bool) -> str:
    """Renders one action item as a Markdown bullet, then its status fields.

    Matches the other tools' citation style, so the action stays usable when a host will
    not make the ``obsidian://`` scheme clickable (SPEC Appendix). The link target is
    the harness-built ``obsidian_uri`` carried on the item; this never constructs one
    itself.
    """
    bits: list[str] = [f"status: {item.status}"]
    if item.priority:
        bits.append(f"priority: {item.priority}")
    if item.due_date is not None:
        due = item.due_date.isoformat()
        bits.append(f"due: {due}{' (OVERDUE)' if overdue else ''}")
    ref = _ref(item.title, item.obsidian_uri, item.path, item.wikilink)
    return f"- {ref} ({', '.join(bits)})"


def _render_raw_page(frontmatter: dict[str, Any], body: str) -> str:
    """Renders a page's frontmatter and body back into raw markdown for display.

    A minimal ``key: value`` block, enough for a host to show what the page contains,
    followed by the verbatim body. This is for human display only: the structured
    ``data`` is what round-trips into a write, never this rendered string.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body + ("\n" if not body.endswith("\n") else "")
