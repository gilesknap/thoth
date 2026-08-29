"""The ``pkm_ingest`` tool body: closed-surface capture into the vault."""

from __future__ import annotations

import re
from typing import Any

from thoth.ingest import Capture, IngestError
from thoth.vault import VaultError

from .context import ToolContext, ToolResult, _reject_outside
from .render import _render_ingest_report

# pkm_ingest refuses a base64 or data-URI argument: the closed surface takes text, a
# URL, or a server-resolvable in-vault path, never inline binary (SPEC section 6)
_DATA_URI_RE: re.Pattern[str] = re.compile(r"^\s*data:[^;,\s]*;base64,", re.IGNORECASE)
_BASE64_BLOB_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9+/]{256,}={0,2}$")


def _looks_like_base64_blob(value: str) -> bool:
    """True when ``value`` looks like inline binary, a data-URI or a base64 blob.

    The closed surface (SPEC section 6) never accepts inline binary: a capture carries
    text, a URL, or a server-resolvable in-vault path, and any image, PDF or audio is
    fetched server-side. A leading ``data:...;base64,`` URI is an unambiguous blob, and
    so is a long unbroken run of base64-alphabet characters with no whitespace, since
    ordinary prose has spaces and is far shorter.
    """
    if _DATA_URI_RE.match(value):
        return True
    stripped = value.strip()
    return bool(_BASE64_BLOB_RE.fullmatch(stripped))


def pkm_ingest(
    ctx: ToolContext,
    *,
    text: str | None = None,
    url: str | None = None,
    path: str | None = None,
) -> ToolResult:
    """Captures text, a URL, or a server-resolvable in-vault path into the vault.

    Builds a :class:`~thoth.ingest.Capture` with ``source='mcp'``, delegates to
    :meth:`thoth.ingest.Ingestor.ingest` and renders the report. The closed surface is
    enforced before any work: a base64 or data-URI argument is refused, since binary
    never travels inline (SPEC section 6), and a ``path`` is confined to the vault root
    before the ingestor is called. An ingest error or a conflict is surfaced as
    ``ToolResult(ok=False, ...)`` and never raised into the MCP runtime.

    Args:
        ctx: The injected collaborator bundle
        text: Inline text or markdown to capture, if any
        url: A URL to fetch server-side, if any
        path: A vault-relative path the server can read, if any

    Returns:
        ``ok=True`` with the rendered report, else ``ok=False`` with the rejection
    """
    if not (text or url or path):
        return ToolResult(
            ok=False,
            text="Provide exactly one of text, url, or path to ingest.",
            data={"provided": []},
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
            return _reject_outside(path)
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
