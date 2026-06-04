"""FastMCP stdio server exposing the ``pkm_*`` tools over the closed vault surface.

This is the appliance's Model-Context-Protocol entry point (SPEC sections 2, 3 and 6).
It publishes seven tools -- :func:`pkm_ingest`, :func:`pkm_search`, :func:`pkm_todos`,
:func:`pkm_recent`, :func:`pkm_write_page`, :func:`pkm_read_page` and
:func:`pkm_edit_page` -- each of which is a *pure delegation* to an already-validated
Phase 0-3 collaborator:

* ``pkm_ingest``   -> :meth:`thoth.ingest.Ingestor.ingest`
* ``pkm_search``   -> :meth:`thoth.query.QueryEngine.answer`
* ``pkm_todos``    -> the canonical action scans on :class:`thoth.summary.SummaryEngine`
* ``pkm_recent``   -> :meth:`thoth.summary.SummaryEngine.recent_pages`
* ``pkm_write_page`` -> :meth:`thoth.vault.Vault.write_page`
* ``pkm_read_page`` -> :meth:`thoth.vault.Vault.read_page` (the verbatim read half of
  the read -> modify -> write-back round trip)
* ``pkm_edit_page`` -> a unique-substring body replace that writes back through
  :func:`pkm_write_page` (so the validation + commit apply, the targeted-edit primitive)

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

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from thoth.config import Config
from thoth.git_sync import GitSync, GitSyncError, VaultConflictError
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.query import Citation, QueryEngine, QueryError, QueryResult
from thoth.vault import SchemaError, SlugError, Vault, VaultError

logger = logging.getLogger("thoth")

__all__ = [
    "SERVER_NAME",
    "TOOL_NAMES",
    "ToolContext",
    "ToolResult",
    "McpServerError",
    "pkm_ingest",
    "pkm_search",
    "pkm_todos",
    "pkm_recent",
    "pkm_write_page",
    "pkm_read_page",
    "pkm_edit_page",
    "build_server",
    "run",
]

SERVER_NAME: str = "thoth"
"""The MCP server name advertised to the host (``FastMCP(SERVER_NAME)``)."""

DEFAULT_MCP_HOST: str = "127.0.0.1"
"""Default HTTP bind address: loopback only (issue #103).

The HTTP transport binds loopback by design -- network exposure is delegated to a
cloudflared tunnel + Cloudflare Access in front of it (ADR 0011), never a raw
``0.0.0.0`` socket. Override with ``--host`` only when you understand the consequence.
"""

DEFAULT_MCP_PORT: int = 8765
"""Default HTTP listen port for ``thoth mcp --transport http`` (issue #103)."""

TOOL_NAMES: tuple[str, ...] = (
    "pkm_ingest",
    "pkm_search",
    "pkm_todos",
    "pkm_recent",
    "pkm_write_page",
    "pkm_read_page",
    "pkm_edit_page",
)
"""The exact tools :func:`build_server` registers (one per ``pkm_*`` function)."""

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
        git: The vault git two-way sync used to commit+push the disk writes the
            write tools make (``pkm_write_page``), staging exactly the path each
            wrote (mirrors ``pkm_ingest``'s commit discipline, issue #85).
    """

    config: Config
    vault: Vault
    ingestor: Ingestor
    query_engine: QueryEngine
    git: GitSync


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
    for uri, wikilink in zip(report.obsidian_links, report.wikilinks, strict=False):
        refs.append(f"[open]({uri}) {wikilink}")
    for uri in report.obsidian_links[len(report.wikilinks) :]:
        refs.append(f"[open]({uri})")
    for wikilink in report.wikilinks[len(report.obsidian_links) :]:
        refs.append(wikilink)
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


def pkm_search(
    ctx: ToolContext,
    *,
    query: str,
    max_pages: int = 5,
    search_keywords: list[str] | None = None,
) -> ToolResult:
    """Run a fast, vault-only lookup and return the answer with vault citations.

    Delegates to :meth:`thoth.query.QueryEngine.answer`, rendering the composed answer
    plus its harness-built citations in MCP Markdown style. A
    :class:`~thoth.query.QueryError` (for example no matching page) is surfaced as
    ``ToolResult(ok=False, ...)``. The structured ``data`` also carries ``provenance``
    (issue #143): one ``{path, methods, rank}`` entry per consulted page recording which
    retrieval method(s) -- grep / wikilink / recall -- surfaced it in the RRF blend.

    Args:
        ctx: The injected collaborator bundle.
        query: The natural-language query.
        max_pages: The maximum number of vault pages to cite.
        search_keywords: De-pluralised, synonym-expanded keywords that seed the vault's
            lexical grep (forwarded as ``search_terms``). The grep matches whole words,
            so a plural query misses singular page content unless the calling model
            supplies the singular keyword here.

    Returns:
        A :class:`ToolResult` with the rendered answer or the error message.
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
            # Per-page retrieval provenance from the RRF blend (issue #143): which
            # method(s) surfaced each consulted page and its final rank, so a
            # programmatic caller sees the grep ∪ recall attribution behind the answer.
            "provenance": [
                {"path": p.path, "methods": list(p.methods), "rank": p.rank}
                for p in result.provenance
            ],
        },
    )


def pkm_todos(ctx: ToolContext, *, include_done: bool = False) -> ToolResult:
    """List open (and optionally done) actions from ``actions/*.md`` frontmatter.

    Reuses the canonical action scans on :class:`thoth.summary.SummaryEngine` (so the
    todo/overdue logic lives in exactly one place): open actions come from
    :meth:`~thoth.summary.SummaryEngine.open_actions`, with overdue items flagged via
    :meth:`~thoth.summary.SummaryEngine.overdue_actions`. Each item is rendered with its
    harness-built ``[title](obsidian-uri)`` link plus the plain vault path and the
    ``[[wikilink]]`` (the MCP citation style the other tools use), then its status, due
    date and priority. Done/cancelled actions are left out unless ``include_done`` is
    true.

    Args:
        ctx: The injected collaborator bundle.
        include_done: When true, also list actions whose status is not open (rendered as
            a separate "Done/closed" section).

    Returns:
        A :class:`ToolResult` listing the actions (always ``ok=True``; an empty vault
        yields a "no open actions" note).
    """
    from thoth.summary import ACTION_OPEN_STATUSES, SummaryEngine

    engine = SummaryEngine(ctx.config, ctx.vault)
    open_actions = engine.open_actions()
    overdue_paths = {item.path for item in engine.overdue_actions()}

    lines: list[str] = ["**Open actions:**"]
    if open_actions:
        for item in open_actions:
            lines.append(_render_action(item, overdue=item.path in overdue_paths))
    else:
        lines.append("- _No open actions._")

    closed = (
        _scan_closed_actions(ctx.vault, open_statuses=set(ACTION_OPEN_STATUSES))
        if include_done
        else []
    )
    if closed:
        lines.append("")
        lines.append("**Done/closed:**")
        lines.extend(
            f"- [{title}]({uri}) - `{path}` {wikilink} (status: {status})"
            for title, status, wikilink, path, uri in closed
        )

    return ToolResult(
        ok=True,
        text="\n".join(lines),
        data={
            "open": [item.path for item in open_actions],
            "overdue": sorted(overdue_paths),
            "closed": [wikilink for _, _, wikilink, _, _ in closed],
        },
    )


def _scan_closed_actions(
    vault: Vault, *, open_statuses: set[str]
) -> list[tuple[str, str, str, str, str]]:
    """Read ``actions/*.md``; return ``(title, status, wikilink, path, obsidian_uri)``.

    A closed action is one whose frontmatter ``status`` is not in ``open_statuses`` (the
    open-action logic itself lives in :class:`thoth.summary.SummaryEngine`; this is the
    thin "also show done" extension ``pkm_todos`` adds, read via the confined vault). A
    missing/blank status counts as open and is therefore excluded. Results are sorted
    by path for determinism.
    """
    directory = vault.root / "actions"
    if not directory.is_dir():
        return []
    closed: list[tuple[str, str, str, str, str]] = []
    for entry in sorted(directory.glob("*.md")):
        rel = f"actions/{entry.name}"
        try:
            page = vault.read_page(rel)
        except VaultError:
            continue
        status_value = page.frontmatter.get("status")
        status = status_value if isinstance(status_value, str) else ""
        if not status or status in open_statuses:
            continue
        title_value = page.frontmatter.get("title")
        slug = PurePosixPath(rel).stem
        title = title_value if isinstance(title_value, str) and title_value else slug
        # Match SummaryEngine's folder-qualified wikilink form ([[actions/<slug>]]) so
        # the open and closed sections render identically.
        closed.append(
            (
                title,
                status,
                f"[[{rel.removesuffix('.md')}]]",
                rel,
                vault.obsidian_uri(rel),
            )
        )
    return closed


def _render_action(item: Any, *, overdue: bool) -> str:
    """Render one action item as a Markdown bullet: link, path, wikilink, then status.

    Matches the other tools' MCP citation style -- ``[title](obsidian-uri)`` plus the
    plain vault path and the ``[[wikilink]]`` -- so the action stays usable when a host
    will not make the custom ``obsidian://`` scheme clickable (SPEC Appendix). The link
    target is the harness-built ``obsidian_uri`` carried on the ``ActionItem``; this
    never constructs an ``obsidian://`` URI itself.
    """
    bits: list[str] = [f"status: {item.status}"]
    if item.priority:
        bits.append(f"priority: {item.priority}")
    if item.due_date is not None:
        due = item.due_date.isoformat()
        bits.append(f"due: {due}{' (OVERDUE)' if overdue else ''}")
    return (
        f"- [{item.title}]({item.obsidian_uri}) - `{item.path}` "
        f"{item.wikilink} ({', '.join(bits)})"
    )


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


def _commit_written_page(
    ctx: ToolContext, rel: str, *, action: str, uri: str, wikilink: str
) -> ToolResult:
    """Commit+push exactly the just-written page and render the outcome.

    The page is already validated and on disk (the write tools call this *after* the
    atomic disk write); this stages **only** ``rel`` (``git add -- <rel>``, the
    issue #85 one-path discipline), commits with an ``agent:`` subject, rebases+pushes,
    under the re-entrant capture lock so it never races the Slack ingest committer. A
    :class:`~thoth.git_sync.VaultConflictError` or any other
    :class:`~thoth.git_sync.GitSyncError` is surfaced as ``ToolResult(ok=False, ...)``
    (the page stays on disk locally; only the sync failed) rather than raised into the
    MCP runtime. On success ``committed`` is echoed in ``data`` and a "(not yet
    committed)" note is appended when nothing was staged (mirrors
    :func:`_render_ingest_report`).

    Args:
        ctx: The injected collaborator bundle (its ``git`` does the commit).
        rel: The vault-relative path that was written (the only thing staged).
        action: The past-tense verb for the success line ("Wrote", "Saved").
        uri: The harness-built ``obsidian://`` link for ``rel``.
        wikilink: The ``[[wikilink]]`` for ``rel``.

    Returns:
        A :class:`ToolResult`: ``ok=True`` once the write synced (``committed`` in
        ``data``), else ``ok=False`` with the conflict/sync-failure guidance.
    """
    try:
        with ctx.git.capture_lock:
            result = ctx.git.commit(f"{action.lower()} {rel}", paths=[rel])
    except VaultConflictError as exc:
        return ToolResult(
            ok=False,
            text=(
                f"{action} `{rel}` locally, but a vault conflict blocked the sync: "
                f"{exc}. Resolve the conflict, then re-sync."
            ),
            data={"path": rel, "conflict": True},
        )
    except GitSyncError as exc:
        return ToolResult(
            ok=False,
            text=(f"{action} `{rel}` locally, but the vault git sync failed: {exc}."),
            data={"path": rel, "committed": False},
        )

    head = f"{action} [{rel}]({uri}) - `{rel}` {wikilink}"
    if not result.committed:
        head += " (not yet committed)"
    return ToolResult(
        ok=True,
        text=head,
        data={
            "path": rel,
            "obsidian_uri": uri,
            "wikilink": wikilink,
            "committed": result.committed,
        },
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
    an atomic write. The written path is then staged, committed and pushed via
    :func:`_commit_written_page` (exactly that one path, under the capture lock). On
    success the path is returned with a harness-built ``obsidian://`` link and
    ``[[wikilink]]`` plus the ``committed`` flag. A :class:`~thoth.vault.SchemaError`
    (bad folder/type or missing field) or :class:`~thoth.vault.SlugError` (bad/escaping
    slug) is surfaced as ``ToolResult(ok=False, ...)`` and nothing is written (no commit
    is attempted); a vault git conflict/sync failure after the disk write is likewise
    surfaced ``ok=False`` (the page stays on disk locally).

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
    return _commit_written_page(ctx, rel, action="Wrote", uri=uri, wikilink=wikilink)


def _resolve_page(ctx: ToolContext, path: str) -> str | ToolResult:
    """Resolve ``path`` to a confined vault-relative page path, or a failure result.

    ``path`` may be a full vault-relative path (``notes/foo.md``) or a bare slug
    (``foo``). A full path is confined through the vault exactly like :func:`pkm_ingest`
    (outside the vault -> ``ToolResult(ok=False, ...)``). A bare slug (no ``/`` and not
    an existing in-vault path) is resolved by globbing the vault for a unique
    ``<slug>.md``: zero or several matches yields a ``ToolResult(ok=False, ...)`` with a
    clear message so the caller can disambiguate. Returns the resolved vault-relative
    path on success, otherwise the failure :class:`ToolResult` to return as-is.
    """
    if not ctx.vault.is_inside(path):
        return ToolResult(
            ok=False,
            text=f"Path is outside the vault and was rejected: `{path}`",
            data={"rejected": "path_confinement", "path": path},
        )
    # A full path (or a slug that happens to resolve to an existing file) is used as-is.
    if ctx.vault.page_exists(path):
        return PurePosixPath(path).as_posix()
    # A bare slug (no separator) is resolved by a unique-filename glob over the vault.
    if "/" not in path:
        slug = path.removesuffix(".md")
        matches = sorted(
            p.relative_to(ctx.vault.root).as_posix()
            for p in ctx.vault.root.rglob(f"{slug}.md")
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return ToolResult(
                ok=False,
                text=f"No page found for slug `{slug}`.",
                data={"slug": slug, "matches": []},
            )
        return ToolResult(
            ok=False,
            text=(
                f"Slug `{slug}` is ambiguous ({len(matches)} matches); "
                f"pass the full vault path instead: {matches}"
            ),
            data={"slug": slug, "matches": matches},
        )
    return ToolResult(
        ok=False,
        text=f"Page does not exist: `{path}`",
        data={"path": path},
    )


def pkm_read_page(ctx: ToolContext, *, path: str) -> ToolResult:
    """Read a page's raw frontmatter + body verbatim (the read-then-write-back half).

    Resolves ``path`` (a full vault-relative path or a bare slug) and reads it through
    :meth:`thoth.vault.Vault.read_page`, returning the parsed frontmatter and body
    *verbatim* so an agent can read -> modify -> write the page back safely (the result
    data round-trips into :func:`pkm_write_page` / :func:`pkm_edit_page`). The path is
    confined to the vault exactly like :func:`pkm_ingest` (outside the vault ->
    ``ok=False``); a bare slug is resolved to a unique ``<slug>.md`` (zero/several
    matches -> ``ok=False``). A :class:`~thoth.vault.VaultError` (missing file) is
    surfaced as ``ToolResult(ok=False, ...)`` and never raised into the MCP runtime.

    Args:
        ctx: The injected collaborator bundle.
        path: A vault-relative path (``notes/foo.md``) or a bare slug (``foo``).

    Returns:
        A :class:`ToolResult`: ``ok=True`` with ``{path, frontmatter, body}`` in
        ``data`` plus a rendered raw-markdown block in ``text``, else ``ok=False``.
    """
    resolved = _resolve_page(ctx, path)
    if isinstance(resolved, ToolResult):
        return resolved
    rel = resolved
    try:
        page = ctx.vault.read_page(rel)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Could not read that page: {exc}", data={})

    text = f"`{rel}`\n\n```markdown\n{_render_raw_page(page.frontmatter, page.body)}```"
    return ToolResult(
        ok=True,
        text=text,
        data={
            "path": rel,
            "frontmatter": dict(page.frontmatter),
            "body": page.body,
        },
    )


def _render_raw_page(frontmatter: dict[str, Any], body: str) -> str:
    """Render a page's frontmatter + body back into raw markdown for display.

    A minimal ``key: value`` YAML-ish frontmatter block (enough for a host to show what
    the page contains) followed by the verbatim body. This is for human display only --
    the structured ``data`` (the parsed ``frontmatter`` dict and ``body``) is what
    round-trips into a write, never this rendered string.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body + ("\n" if not body.endswith("\n") else "")


def pkm_edit_page(
    ctx: ToolContext, *, path: str, old_string: str, new_string: str
) -> ToolResult:
    """Make a targeted, exact-string replace on a page body (the file-edit primitive).

    Resolves and reads the page (same path/slug resolution as :func:`pkm_read_page`),
    then replaces a **unique** occurrence of ``old_string`` in the *body* with
    ``new_string`` and writes the result back by delegating to :func:`pkm_write_page`
    (full reuse: the page's existing frontmatter is preserved and the write runs the
    whole validation + #153 commit surface, so the edit is committed/pushed exactly like
    a write). ``old_string`` must appear exactly once: zero occurrences -> ``ok=False``
    ("not found"); more than one -> ``ok=False`` (asking for more surrounding context).
    A no-op edit (``old_string == new_string``) is refused. Nothing raises into the MCP
    runtime.

    Args:
        ctx: The injected collaborator bundle.
        path: A vault-relative path (``notes/foo.md``) or a bare slug (``foo``).
        old_string: The exact body substring to replace (must be unique in the body).
        new_string: The replacement text.

    Returns:
        A :class:`ToolResult`: the :func:`pkm_write_page` outcome (``ok=True`` with the
        committed path) on a successful edit, else ``ok=False`` with the reason.
    """
    if old_string == new_string:
        return ToolResult(
            ok=False,
            text="No edit to make: old_string and new_string are identical.",
            data={},
        )
    resolved = _resolve_page(ctx, path)
    if isinstance(resolved, ToolResult):
        return resolved
    rel = resolved
    try:
        page = ctx.vault.read_page(rel)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Could not read that page: {exc}", data={})

    count = page.body.count(old_string)
    if count == 0:
        return ToolResult(
            ok=False,
            text=f"old_string was not found in `{rel}`.",
            data={"path": rel},
        )
    if count > 1:
        return ToolResult(
            ok=False,
            text=(
                f"old_string is not unique in `{rel}` ({count} occurrences); "
                "include more surrounding context to identify the one to edit."
            ),
            data={"path": rel, "occurrences": count},
        )
    new_body = page.body.replace(old_string, new_string, 1)

    # Write back through the validated write surface so all guardrails + the #153
    # commit apply: folder is the first path segment, slug the filename stem, and the
    # existing frontmatter ('created' preserved, 'updated' restamped) is reused.
    parts = PurePosixPath(rel)
    folder = parts.parts[0]
    slug = parts.stem
    return pkm_write_page(
        ctx,
        folder=folder,
        slug=slug,
        frontmatter=dict(page.frontmatter),
        body=new_body,
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
    def _search(
        query: str,
        max_pages: int = 5,
        search_keywords: list[str] | None = None,
    ) -> ToolResult:
        """Run a fast, vault-only lookup and return the answer with citations.

        Pass `search_keywords` to seed the vault's lexical search: extract them
        from the user's request, de-pluralise to the singular (dogs -> dog),
        drop stop words (list, me, the, about, what, show), and expand obvious
        synonyms (dog -> Labradoodle, pet). The grep matches on whole words, so
        an un-singularised plural will miss singular page content. Omit it only
        when the query is already a single bare keyword.

        When relaying the result, preserve each source's clickable
        `obsidian://open?...` link verbatim -- present citations as those links,
        never flattened to bare `path/to/page.md` text.
        """
        return pkm_search(
            ctx, query=query, max_pages=max_pages, search_keywords=search_keywords
        )

    @server.tool(name="pkm_todos")
    def _todos(include_done: bool = False) -> ToolResult:
        """List open (and optionally done) actions from the vault frontmatter."""
        return pkm_todos(ctx, include_done=include_done)

    @server.tool(name="pkm_recent")
    def _recent(days: int = 7, limit: int = 20) -> ToolResult:
        """List recently created/updated curated pages from their frontmatter dates.

        When relaying the result, preserve each page's clickable
        `obsidian://open?...` link verbatim -- never flatten it to a bare path.
        """
        return pkm_recent(ctx, days=days, limit=limit)

    @server.tool(name="pkm_write_page")
    def _write_page(
        folder: str, slug: str, frontmatter: dict[str, Any], body: str
    ) -> ToolResult:
        """Write a page through the validated vault surface (the escape hatch)."""
        return pkm_write_page(
            ctx, folder=folder, slug=slug, frontmatter=frontmatter, body=body
        )

    @server.tool(name="pkm_read_page")
    def _read_page(path: str) -> ToolResult:
        """Read a page's raw frontmatter + body verbatim (by path or bare slug).

        Use this before editing an existing page so you read -> modify -> write back
        safely: the returned `frontmatter` and `body` round-trip into pkm_write_page
        or pkm_edit_page. `path` may be a full vault path (notes/foo.md) or a bare
        slug (foo) that resolves to a unique page.
        """
        return pkm_read_page(ctx, path=path)

    @server.tool(name="pkm_edit_page")
    def _edit_page(path: str, old_string: str, new_string: str) -> ToolResult:
        """Make a targeted, unique-substring replace on a page body, then commit.

        Prefer this over pkm_write_page to change an existing page (for example to
        add one link): `old_string` must occur exactly once in the body. The edit is
        written back through the validated surface, so it is committed and pushed
        like any write. `path` may be a full vault path or a bare unique slug.
        """
        return pkm_edit_page(
            ctx, path=path, old_string=old_string, new_string=new_string
        )

    return server


def run(
    config: Config,
    ctx: ToolContext | None = None,
    *,
    transport: str = "stdio",
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
) -> None:
    """Wire a real :class:`ToolContext` (if needed) and serve over the chosen transport.

    This is the production entry point (``thoth mcp``). When ``ctx`` is ``None`` it
    wires the full collaborator graph -- a :class:`~thoth.vault.Vault`, an
    :class:`~thoth.llm.LLM`, an :class:`~thoth.extract.Extractor`, a
    :class:`~thoth.hindsight.Hindsight`, a :class:`~thoth.git_sync.GitSync`, an
    :class:`~thoth.ingest.Ingestor` and a :class:`~thoth.query.QueryEngine` (the graph
    ``slack_app.run`` builds) -- then builds the server via :func:`build_server` and
    runs it.

    The ``transport`` selects how the server is exposed (issue #103):

    * ``"stdio"`` (the default) -- the byte-for-byte-unchanged spawn-as-a-child model
      Claude Code uses locally: ``host``/``port`` are ignored and no socket is bound.
    * ``"http"`` -- the streamable-HTTP transport bound to ``host``:``port``
      (loopback by default; network exposure is delegated to cloudflared + Cloudflare
      Access, ADR 0011). Tier-1 bearer auth is mandatory: the server **fails fast** at
      startup if ``THOTH_MCP_API_KEYS`` is unset (never binding an unauthenticated
      socket), and -- when ``THOTH_MCP_CF_ACCESS_*`` are set -- also enforces the
      Cf-Access JWT. See :func:`_run_http`.

    The collaborator construction and the lazy ``mcp`` import happen only here, so
    importing this module stays light. This is never unit-tested live (CI has no stdio
    and no ``mcp`` package).

    Args:
        config: The frozen runtime config.
        ctx: An already-wired context (for tests/embedding); built from ``config`` when
            ``None``.
        transport: ``"stdio"`` (default) or ``"http"``.
        host: HTTP bind address (ignored for stdio).
        port: HTTP listen port (ignored for stdio).

    Raises:
        ValueError: if ``transport`` is not ``"stdio"`` or ``"http"``.
        ConfigError: (HTTP only) if ``THOTH_MCP_API_KEYS`` is unset/empty -- refusing to
            bind an unauthenticated socket.
    """
    if transport not in ("stdio", "http"):
        raise ValueError(
            f"unknown MCP transport {transport!r}; expected 'stdio' or 'http'"
        )
    # Fail fast BEFORE wiring the graph or binding a socket: an HTTP transport with no
    # bearer keys must never start (#103). require_mcp_api_keys raises ConfigError.
    if transport == "http":
        config.require_mcp_api_keys()

    if ctx is None:
        from thoth.budget import make_budget_guard
        from thoth.extract import Extractor
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
        ctx = ToolContext(
            config=config,
            vault=vault,
            ingestor=ingestor,
            query_engine=query_engine,
            git=git,
        )

    server = build_server(ctx)
    if transport == "http":
        _run_http(server, config, host=host, port=port)
    else:
        server.run(transport="stdio")


def _run_http(server: Any, config: Config, *, host: str, port: int) -> None:
    """Serve a built FastMCP over streamable-HTTP with the two-tier auth gate.

    Points the FastMCP settings at ``host``:``port``, wraps the streamable-HTTP ASGI app
    with the bearer (+ optional Cf-Access JWT) middleware
    (:func:`thoth.mcp_auth.build_auth_middleware`) so every request is authenticated
    BEFORE any tool dispatch, and serves it with uvicorn. All web-stack imports
    (``uvicorn``, ``starlette`` via the middleware) happen here, never at module top
    level, so importing this module stays CI-safe. This is exercised live, not in CI
    (the suite has no ``mcp``/``uvicorn``).

    Args:
        server: The built FastMCP instance.
        config: The frozen runtime config (bearer keys + optional Cf-Access settings).
        host: The bind address (loopback by default).
        port: The listen port.
    """
    import uvicorn

    from thoth.mcp_auth import build_auth_middleware

    # FastMCP reads host/port from its settings; set them before building the ASGI app.
    server.settings.host = host
    server.settings.port = port
    # FastMCP's streamable-HTTP transport enables DNS-rebinding protection that, by
    # default, only accepts loopback Host/Origin headers. Behind the cloudflared tunnel
    # the inbound Host is the public hostname, so without this every real connector
    # request 421s. Append any operator-configured public host(s)/origin(s) to the
    # loopback defaults (ADR 0011); the alternative is a cloudflared httpHostHeader
    # rewrite, documented in the deploy how-to.
    extra_hosts = config.mcp_allowed_hosts_list()
    extra_origins = config.mcp_allowed_origins_list()
    if extra_hosts or extra_origins:
        sec = server.settings.transport_security
        if sec is None:  # pragma: no cover - FastMCP always provides defaults
            from mcp.server.transport_security import TransportSecuritySettings

            sec = TransportSecuritySettings()
            server.settings.transport_security = sec
        sec.allowed_hosts = [*sec.allowed_hosts, *extra_hosts]
        sec.allowed_origins = [*sec.allowed_origins, *extra_origins]
    app = server.streamable_http_app()
    # The auth gate runs ahead of the MCP routes: a missing/invalid bearer (or, when
    # Cf-Access is configured, a missing/invalid assertion) yields 401 and the request
    # never reaches a pkm_* tool (issue #103).
    app.add_middleware(build_auth_middleware(config))
    logger.info(
        "thoth MCP serving streamable-HTTP on http://%s:%d (bearer auth%s)",
        host,
        port,
        ", + Cf-Access JWT" if config.mcp_cf_access_enabled() else "",
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
