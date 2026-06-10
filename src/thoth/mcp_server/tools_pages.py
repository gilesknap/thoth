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

    Combines :func:`_resolve_page` with :meth:`thoth.vault.Vault.read_page`: returns
    ``(rel, page)`` on success, otherwise the failure :class:`ToolResult` to return
    as-is (a :class:`~thoth.vault.VaultError` on the read is surfaced ``ok=False``,
    never raised into the MCP runtime).
    """
    resolved = _resolve_page(ctx, path)
    if isinstance(resolved, ToolResult):
        return resolved
    try:
        return resolved, ctx.vault.read_page(resolved)
    except VaultError as exc:
        return ToolResult(ok=False, text=f"Could not read that page: {exc}", data={})


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
