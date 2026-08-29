"""Per-item helpers for ``thoth capture``, split out of :mod:`thoth.__main__`.

These helpers back the :func:`thoth.__main__.run_capture` loop that the file-walk
(issue #80) and inbox-drain (issue #105) branches share. They tally one capture's
disposition and commit one batch of imported files. Only the standard library is
imported at module top level. Each helper imports the ingest and git exception types
lazily inside its own body.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("thoth")


@dataclass
class _CaptureCounts:
    """Per-run capture dispositions, shared by the file-walk and inbox-drain paths."""

    filed: int = 0
    skipped: int = 0
    unchanged: int = 0
    deferred: int = 0
    failed: int = 0


def _ingest_one(
    graph: Any,
    vault: Any,
    capture: Any,
    *,
    target: str,
    hold_rel: str | None,
    as_is: bool,
    index: int,
    counts: _CaptureCounts,
) -> str:
    """Ingest one capture, tally its disposition, and print a line.

    The helper defers the commit. The file-walk (issue #80) and inbox-drain (issue #105)
    branches share it.

    A per-item failure stays isolated. The helper logs an
    :class:`~thoth.ingest.IngestError`, counts it and skips the item, which stays
    durable in ``inbox/``.

    The helper retires a drain hold once its content is durably curated, and stages the
    deletion into the next batch. It does so on a genuine file, where ``page_paths`` is
    non-empty, and on an ``unchanged`` skip. Ingest reports ``unchanged`` only when the
    curated page provably already exists (issue #113), so such a hold duplicates
    already-filed content. A deferred or skipped hold stays, which is recoverable and
    idempotent, so a budget re-trip never silently deletes un-filed content.

    Returns:
        The disposition string.
    """
    from .ingest import IngestError

    try:
        report = graph.ingestor.ingest(capture, commit=False, as_is=as_is)
    except IngestError as exc:
        counts.failed += 1
        logger.warning("capture [%d]: %s -> FAILED (%s)", index, target, exc)
        return "failed"
    if report.deferred:
        counts.deferred += 1
        disposition = "deferred"
    elif report.unchanged:
        # Skip on unchanged (#95 task D). The page is already curated, so this run
        # re-spends nothing and re-stamps nothing.
        counts.unchanged += 1
        disposition = "unchanged"
    elif report.page_paths:
        counts.filed += 1
        disposition = "filed"
    else:
        counts.skipped += 1
        disposition = "skipped"
    # Retire a drained hold once its content is durably curated: on a genuine file, and
    # on an `unchanged` skip (#113). Ingest reports `unchanged` only when the
    # classify-routed curated page provably already exists on disk, in
    # Ingestor._unchanged_curated, so the hold duplicates already-filed content. Without
    # this step the hold lingers in inbox/ forever and re-spends a classify call on
    # every run. Never drop a `deferred`, `skipped` or `failed` hold, which would lose
    # data. remove_page is idempotent and path-confined. The removal stages into the
    # same batch as the new page.
    if disposition in ("filed", "unchanged") and hold_rel is not None:
        vault.remove_page(hold_rel)
    print(
        f"capture [{index}]: {target} -> "
        f"{', '.join(report.page_paths) or report.message or 'no new page'}"
    )
    return disposition


def _commit_capture_batch(git: Any, count: int) -> None:
    """Commit and push one batch of imported files, then stop loudly on a conflict.

    :meth:`thoth.git_sync.GitSync.commit` does add -A, commit, rebase and push in one
    call. It returns ``committed=False`` on "nothing to commit", so a flush with no
    pending changes is a safe no-op. A :class:`~thoth.git_sync.VaultConflictError`
    aborts the import and never forces the push. The content is filed locally, and the
    operator re-runs once the remote is reconciled, because the run is idempotent.
    """
    from .git_sync import VaultConflictError

    try:
        result = git.commit(f"import: batch ({count} file(s))")
    except VaultConflictError as exc:
        raise SystemExit(
            "capture: VAULT CONFLICT on a batch commit -- content is filed locally "
            f"but the push was refused. Resolve in Obsidian and re-run. ({exc})"
        ) from exc
    if result.committed:
        print(f"capture: committed batch of {count} file(s)")
