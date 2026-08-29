"""Per-item helpers for ``thoth capture``, split out of :mod:`thoth.__main__`.

These helpers back the :func:`thoth.__main__.run_capture` loop that the file-walk (#80)
and inbox-drain (#105) branches share, one tallying a capture's disposition and one
committing a batch of imported files. Import safety: the module top level imports only
the standard library, and each helper body imports the ingest and git exception types
lazily.
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
    """Ingest one capture, defer the commit, tally the disposition and print a line.

    The file-walk (#80) and inbox-drain (#105) branches share this helper. A per-item
    failure stays isolated: the helper logs an :class:`~thoth.ingest.IngestError`,
    counts it and skips the item, which stays durable in ``inbox/``.

    A drain hold retires, with the deletion staged into the next batch, once its
    content is durably curated: a genuine file with ``page_paths`` non-empty, or an
    ``unchanged`` skip, which is reported only when the curated page provably already
    exists (#113) and therefore duplicates already-filed content. A deferred or skipped
    hold stays, recoverable and idempotent, so a budget re-trip never silently deletes
    un-filed content.

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
        # Skip on unchanged (#95 task D): already curated, so nothing is re-spent.
        counts.unchanged += 1
        disposition = "unchanged"
    elif report.page_paths:
        counts.filed += 1
        disposition = "filed"
    else:
        counts.skipped += 1
        disposition = "skipped"
    # Ingestor._unchanged_curated proves the curated page exists before it reports
    # `unchanged`, so retiring the hold here cannot lose content (#113). Left alone it
    # would linger in inbox/ forever and re-spend a classify call each run. remove_page
    # is idempotent and path-confined, and the removal stages into the same batch as
    # the new page.
    if disposition in ("filed", "unchanged") and hold_rel is not None:
        vault.remove_page(hold_rel)
    print(
        f"capture [{index}]: {target} -> "
        f"{', '.join(report.page_paths) or report.message or 'no new page'}"
    )
    return disposition


def _commit_capture_batch(git: Any, count: int) -> None:
    """Commit and push one batch of imported files. Surface a conflict loudly and stop.

    :meth:`thoth.git_sync.GitSync.commit` does add -A, commit, rebase and push in one
    call, and returns ``committed=False`` for "nothing to commit", so a flush with no
    pending changes is a safe no-op. A :class:`~thoth.git_sync.VaultConflictError`
    aborts the import rather than ever forcing the push. The content is filed locally,
    and the operator re-runs once the remote is reconciled, because the run is
    idempotent.
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
