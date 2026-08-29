"""The page tool bodies: ``pkm_write_page``, ``pkm_read_page`` and ``pkm_edit_page``."""

from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath
from typing import Any

from thoth.git_sync import GitSyncError, VaultConflictError
from thoth.vault import Page, VaultError

from .context import ToolContext, ToolResult, _reject_outside
from .render import _ref, _render_raw_page


def _commit_written_page(
    ctx: ToolContext, rel: str, *, action: str, uri: str, wikilink: str
) -> ToolResult:
    """Commits and pushes exactly the just-written page, then renders the outcome.

    The page is already validated and on disk, since the write tools call this after the
    atomic write. Only that one path is staged, keeping the issue #85 discipline, and
    the commit runs under the re-entrant capture lock so it never races the Slack
    committer. A conflict or any other git failure is surfaced as a failed result rather
    than raised into the MCP runtime, because the page stays on disk locally and only
    the sync failed.

    Args:
        ctx: The injected collaborator bundle, whose git does the commit.
        rel: The written vault path, and the only thing staged.
        action: Past-tense verb for the success line.
        uri: The harness-built deep link.
        wikilink: The wikilink for the page.

    Returns:
        A successful result once the write synced, otherwise a failure carrying the
        conflict or sync guidance. A note is appended when nothing was staged.
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

    head = f"{action} {_ref(rel, uri, rel, wikilink)}"
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
    """Writes a page through the validated vault surface, the low-level hatch.

    Delegates straight to the vault, which runs the full folder, slug, source and
    confinement validation plus redaction and an atomic write. The path is then staged,
    committed and pushed under the capture lock.

    A schema or slug rejection is surfaced as a failed result with nothing written and
    no commit attempted. A git conflict after the disk write is likewise a failure, with
    the page staying on disk locally.

    Args:
        ctx: The injected collaborator bundle.
        folder: A top-level vault folder.
        slug: The page slug.
        frontmatter: Page frontmatter, carrying a valid type and source.
        body: The page body markdown.
        today: Date to stamp, kept injectable for tests.

    Returns:
        The written path on success, otherwise the rejection.
    """
    try:
        rel = ctx.vault.write_page(folder, slug, frontmatter, body, today=today)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Vault rejected the page: {exc}", data={})

    uri = ctx.vault.obsidian_uri(rel)
    wikilink = f"[[{PurePosixPath(rel).stem}]]"
    return _commit_written_page(ctx, rel, action="Wrote", uri=uri, wikilink=wikilink)


def _resolve_page(ctx: ToolContext, path: str) -> str | ToolResult:
    """Resolves a path or bare slug to a confined vault path, or fails typed.

    A full path is confined through the vault exactly as ingest does. A bare slug is
    resolved by globbing for a unique file, and zero or several matches yields a clear
    failure so the caller can disambiguate.
    """
    if not ctx.vault.is_inside(path):
        return _reject_outside(path)
    # A full path, or a slug that happens to resolve to a real file, is used as-is
    if ctx.vault.page_exists(path):
        return PurePosixPath(path).as_posix()
    # A bare slug is resolved by a unique-filename glob over the vault
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


def _load_page(ctx: ToolContext, path: str) -> tuple[str, Page] | ToolResult:
    """Resolves a path or slug and reads the page, or fails typed.

    A read failure is surfaced as a failed result and never raised into the MCP runtime.
    """
    resolved = _resolve_page(ctx, path)
    if isinstance(resolved, ToolResult):
        return resolved
    try:
        return resolved, ctx.vault.read_page(resolved)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Could not read that page: {exc}", data={})


def pkm_read_page(ctx: ToolContext, *, path: str) -> ToolResult:
    """Reads a page's raw frontmatter and body verbatim, the read half of a write-back.

    The frontmatter and body come back untouched, so an agent can read, modify and write
    the page back safely, and the result data round-trips into the write and edit tools.
    The path is confined exactly as ingest does, and a bare slug is resolved to a unique
    file. A missing file is surfaced as a failure rather than raised into the MCP
    runtime.

    Args:
        ctx: The injected collaborator bundle.
        path: A vault-relative path or a bare slug.

    Returns:
        The path, frontmatter and body with a rendered markdown block, otherwise the
        failure.
    """
    loaded = _load_page(ctx, path)
    if isinstance(loaded, ToolResult):
        return loaded
    rel, page = loaded

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


def pkm_edit_page(
    ctx: ToolContext, *, path: str, old_string: str, new_string: str
) -> ToolResult:
    """Makes a targeted, exact-string replacement on a page body.

    Resolves and reads the page with the same rules as the read tool, replaces a unique
    occurrence in the body, and writes the result back through the write tool. That
    reuse preserves the existing frontmatter and runs the whole validation and commit
    surface, so an edit is committed exactly like a write.

    The old string must appear exactly once. Zero occurrences fails as not found, and
    more than one fails asking for more surrounding context.

    A no-op edit is refused. Nothing raises into the MCP runtime.

    Args:
        ctx: The injected collaborator bundle.
        path: A vault-relative path or a bare slug.
        old_string: The exact body substring to replace, which must be unique.
        new_string: The replacement text.

    Returns:
        The write outcome on a successful edit, otherwise the failure and its reason.
    """
    if old_string == new_string:
        return ToolResult(
            ok=False,
            text="No edit to make: old_string and new_string are identical.",
            data={},
        )
    loaded = _load_page(ctx, path)
    if isinstance(loaded, ToolResult):
        return loaded
    rel, page = loaded

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

    # Write back through the validated surface so every guardrail and the #153 commit
    # apply. Folder is the first path segment, slug the filename stem, and the existing
    # frontmatter is reused with created preserved and updated restamped
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
