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
    """Commit and push exactly the just-written page, then render the outcome.

    The page is already validated and on disk, the write tools calling this *after* the
    atomic disk write. This stages **only** ``rel``, as ``git add -- <rel>``, the issue
    #85 one-path discipline, commits with an ``agent:`` subject, then rebases and
    pushes, all under the re-entrant capture lock so it never races the Slack ingest
    committer. A :class:`~thoth.git_sync.VaultConflictError`, or any other
    :class:`~thoth.git_sync.GitSyncError`, surfaces as ``ToolResult(ok=False, ...)``
    rather than raises into the MCP runtime, the page staying on disk locally with only
    the sync failed. On success ``data`` echoes ``committed``, and a "(not yet
    committed)" note is appended when nothing was staged, mirroring
    :func:`_render_ingest_report`.

    Args:
        ctx: The injected collaborator bundle, whose ``git`` does the commit.
        rel: The vault-relative path that was written, and the only thing staged.
        action: The past-tense verb for the success line, "Wrote" or "Saved".
        uri: The harness-built ``obsidian://`` link for ``rel``.
        wikilink: The ``[[wikilink]]`` for ``rel``.

    Returns:
        A :class:`ToolResult`, ``ok=True`` once the write synced, with ``committed`` in
        ``data``, else ``ok=False`` with the conflict or sync-failure guidance.
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
    """Write a page through the validated vault surface, the low-level escape hatch.

    Delegates straight to :meth:`thoth.vault.Vault.write_page`, which performs the full
    folder-by-type, slug, source and confinement validation, plus secret redaction and
    an atomic write. :func:`_commit_written_page` then stages, commits and pushes
    exactly that one path under the capture lock. On success the path returns with a
    harness-built ``obsidian://`` link, a ``[[wikilink]]`` and the ``committed`` flag. A
    :class:`~thoth.vault.SchemaError`, from a bad folder or type or a missing field, or
    a :class:`~thoth.vault.SlugError`, from a bad or escaping slug, surfaces as
    ``ToolResult(ok=False, ...)`` with nothing written and no commit attempted. A vault
    git conflict or sync failure after the disk write likewise surfaces ``ok=False``,
    the page staying on disk locally.

    Args:
        ctx: The injected collaborator bundle.
        folder: A top-level vault folder, a key of ``thoth.vault.FOLDER_TYPE_CONTRACT``.
        slug: The page slug, validated by :meth:`thoth.vault.Vault.validate_slug`.
        frontmatter: The page frontmatter, which must carry a valid ``type`` and
            ``source``.
        body: The page body markdown.
        today: The date to stamp, defaulting to today and kept injectable for tests.

    Returns:
        A :class:`ToolResult` with the written path on success, else the rejection.
    """
    try:
        rel = ctx.vault.write_page(folder, slug, frontmatter, body, today=today)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Vault rejected the page: {exc}", data={})

    uri = ctx.vault.obsidian_uri(rel)
    wikilink = f"[[{PurePosixPath(rel).stem}]]"
    return _commit_written_page(ctx, rel, action="Wrote", uri=uri, wikilink=wikilink)


def _resolve_page(ctx: ToolContext, path: str) -> str | ToolResult:
    """Resolve ``path`` to a confined vault-relative page path, or a failure result.

    ``path`` may be a full vault-relative path such as ``notes/foo.md``, or a bare slug
    such as ``foo``. A full path is confined through the vault exactly as
    :func:`pkm_ingest` confines one, so a path outside the vault yields
    ``ToolResult(ok=False, ...)``. A bare slug, carrying no ``/`` and matching no
    existing in-vault path, resolves by globbing the vault for a unique ``<slug>.md``,
    and zero or several matches yields a ``ToolResult(ok=False, ...)`` with a clear
    message so the caller can disambiguate. Returns the resolved vault-relative path on
    success, otherwise the failure :class:`ToolResult` to return as is.
    """
    if not ctx.vault.is_inside(path):
        return _reject_outside(path)
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


def _load_page(ctx: ToolContext, path: str) -> tuple[str, Page] | ToolResult:
    """Resolve ``path`` (full path or bare slug) and read the page, or fail typed.

    Combines :func:`_resolve_page` with :meth:`thoth.vault.Vault.read_page`, returning
    ``(rel, page)`` on success and otherwise the failure :class:`ToolResult` to return
    as is. A :class:`~thoth.vault.VaultError` on the read surfaces ``ok=False`` and
    never raises into the MCP runtime.
    """
    resolved = _resolve_page(ctx, path)
    if isinstance(resolved, ToolResult):
        return resolved
    try:
        return resolved, ctx.vault.read_page(resolved)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Could not read that page: {exc}", data={})


def pkm_read_page(ctx: ToolContext, *, path: str) -> ToolResult:
    """Read a page's raw frontmatter and body verbatim, the read-then-write-back half.

    Resolves ``path``, a full vault-relative path or a bare slug, and reads it through
    :meth:`thoth.vault.Vault.read_page`, returning the parsed frontmatter and body
    *verbatim* so an agent can read, modify and write the page back safely. The result
    data round-trips into :func:`pkm_write_page` or :func:`pkm_edit_page`. The path is
    confined to the vault exactly as :func:`pkm_ingest` confines one, so a path outside
    yields ``ok=False``, and a bare slug resolves to a unique ``<slug>.md``, with zero
    or several matches yielding ``ok=False``. A :class:`~thoth.vault.VaultError` from a
    missing file surfaces as ``ToolResult(ok=False, ...)`` and never raises into the MCP
    runtime.

    Args:
        ctx: The injected collaborator bundle.
        path: A vault-relative path such as ``notes/foo.md``, or a bare slug such as
            ``foo``.

    Returns:
        A :class:`ToolResult`, ``ok=True`` with ``{path, frontmatter, body}`` in
        ``data`` plus a rendered raw-markdown block in ``text``, else ``ok=False``.
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
    """Make a targeted, exact-string replace on a page body (the file-edit primitive).

    Resolves and reads the page, with the same path and slug resolution as
    :func:`pkm_read_page`, then replaces a **unique** occurrence of ``old_string`` in
    the *body* with ``new_string`` and writes the result back by delegating to
    :func:`pkm_write_page`. That is full reuse: the page's existing frontmatter is
    preserved, and the write runs the whole validation and #153 commit surface, so the
    edit is committed and pushed exactly like a write. ``old_string`` must appear
    exactly once, so zero occurrences yields ``ok=False`` and "not found", while more
    than one yields ``ok=False`` asking for more surrounding context. A no-op edit,
    where ``old_string == new_string``, is refused. Nothing raises into the MCP runtime.

    Args:
        ctx: The injected collaborator bundle.
        path: A vault-relative path such as ``notes/foo.md``, or a bare slug such as
            ``foo``.
        old_string: The exact body substring to replace, which must be unique in the
            body.
        new_string: The replacement text.

    Returns:
        A :class:`ToolResult`, the :func:`pkm_write_page` outcome with ``ok=True`` and
        the committed path on a successful edit, else ``ok=False`` with the reason.
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
