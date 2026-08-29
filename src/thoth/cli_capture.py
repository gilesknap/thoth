"""Per-item helpers for ``thoth capture``, split out of :mod:`thoth.__main__`.

These back the :func:`thoth.__main__.run_capture` loop shared by the file-walk (#80)
and inbox-drain (#105) branches: tally one capture's disposition, and commit one
batch of imported files. Import safety: only the standard library is imported at
module top level; the ingest/git exception types are imported lazily inside each
helper body.
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
    """Ingest one capture (commit deferred), tally its disposition, print a line.

    Shared by the file-walk (#80) and inbox-drain (#105) branches. Per-item failures are
    isolated: an :class:`~thoth.ingest.IngestError` is logged, counted, and skipped
    (the item stays durable in ``inbox/``). A drain hold is retired (with the deletion
    staged into the next batch) once its content is durably curated -- on a genuine file
    (``page_paths`` non-empty) AND on an ``unchanged`` skip, since ``unchanged`` is only
    reported when the curated page provably already exists (#113); such a hold is a
    duplicate of already-filed content. A deferred/skipped hold stays (recoverable,
    idempotent) so a budget re-trip never silently deletes un-filed content. Returns the
    disposition string.
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
        # Skip-on-unchanged (#95 task D): already curated, nothing re-spent/re-stamped.
        counts.unchanged += 1
        disposition = "unchanged"
    elif report.page_paths:
        counts.filed += 1
        disposition = "filed"
    else:
        counts.skipped += 1
        disposition = "skipped"
    # Retire a drained hold once its content is durably curated -- both on a genuine
    # file AND on an `unchanged` skip (#113). `unchanged` is only reported when the
    # classify-routed curated page provably already exists on disk (see
    # Ingestor._unchanged_curated), so the hold is a duplicate of already-filed content
    # and would otherwise linger in inbox/ forever, re-spending a classify call each
    # run. Never drop a `deferred`/`skipped`/`failed` hold (data-loss guard).
    # remove_page is idempotent + path-confined; the removal stages into the same batch
    # as the new page.
    if disposition in ("filed", "unchanged") and hold_rel is not None:
        vault.remove_page(hold_rel)
    print(
        f"capture [{index}]: {target} -> "
        f"{', '.join(report.page_paths) or report.message or 'no new page'}"
    )
    return disposition


def _commit_capture_batch(git: Any, count: int) -> None:
    """Commit + push one batch of imported files; surface a conflict loudly and stop.

    :meth:`thoth.git_sync.GitSync.commit` does add -A + commit + rebase + push in one
    call and returns ``committed=False`` on "nothing to commit", so a flush with no
    pending changes is a safe no-op. A :class:`~thoth.git_sync.VaultConflictError`
    aborts the import (the content is filed locally; the operator re-runs once the
    remote is reconciled -- the run is idempotent) rather than ever forcing the push.
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
