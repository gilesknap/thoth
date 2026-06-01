"""Sweep the durable ``inbox/hold-*.md`` holds back through ingest (issue #105).

A budget-capped bulk import (or an LLM-unavailable capture) leaves the inbound item
durable as ``inbox/hold-<sha12>.md`` (``type: inbox``) but never curated, so grep cannot
see it (its :data:`thoth.query.SEARCHED_DIRS` excludes ``inbox/``) and Hindsight never
retained it -- the content is stranded. This module is the source-independent drain: it
walks the holds and yields one :class:`~thoth.ingest.Capture` per recoverable hold, built
from the hold's STORED body and its threaded ``source:``, ready to feed straight through
the EXISTING :meth:`thoth.ingest.Ingestor.ingest` pipeline (classify/curate -> file ->
retain). It is symmetric to :mod:`thoth.capture_walk`: a second capture source, not a new
ingest pass.

Because :meth:`Ingestor.persist_inbound` re-derives the hold slug from the body SHA-256,
re-persisting an identical body lands on the SAME ``inbox/hold-*`` path, so the caller can
remove the original hold by its path once the page is filed (the re-persist and the
original are one file). The body is passed VERBATIM -- no string-rewriting (the project's
hard rule).

Scope v1 is TEXT holds. A binary hold carries only the
:meth:`Ingestor._binary_stub_body` provenance stub (its bytes were never recoverable), so
it is skipped-and-logged rather than re-fed: there is nothing to re-curate. Detection is a
content sniff on the stub's stored marker lines (kept here as a single constant) since the
hold frontmatter carries no binary flag.

Only the standard library plus a deferred import of :data:`thoth.ingest.Capture` (inside
the generator body, mirroring :mod:`thoth.capture_walk`'s import-safety contract) and the
:class:`~thoth.vault.Vault` read surface are used -- no LLM, no network.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from thoth.vault import VALID_SOURCES, Vault, VaultError

if TYPE_CHECKING:
    from thoth.ingest import Capture

__all__ = ["drain_captures"]

logger = logging.getLogger(__name__)

# The source stamped on a hold whose frontmatter ``source:`` is missing or not a known
# value, so one odd hold never aborts a sweep. ``import`` is a member of VALID_SOURCES.
_FALLBACK_SOURCE: str = "import"

# Sentinel lines from :meth:`thoth.ingest.Ingestor._binary_stub_body`: a hold whose body
# is that provenance stub carries no recoverable bytes, so it is skipped (v1 text scope).
# Kept here as the single place the sniff is expressed -- if the stub wording changes,
# this constant must change with it.
_BINARY_STUB_HEAD: str = "# Held capture"
_BINARY_STUB_MARKERS: tuple[str, ...] = (
    "Binary source:",
    "Unsupported binary content",
)


def drain_captures(vault: Vault) -> Iterator[tuple[str, Capture]]:
    """Yield ``(hold_rel_path, Capture)`` per recoverable text hold under ``inbox/``.

    Walks ``inbox/hold-*.md`` in sorted path order (deterministic). For each, the hold's
    stored body becomes a ``text`` :class:`~thoth.ingest.Capture` carrying the hold's
    threaded ``source:`` (validated against :data:`~thoth.vault.VALID_SOURCES`, falling
    back to ``import`` when absent/unknown so one odd hold never aborts the sweep). The
    body is passed verbatim.

    A binary hold -- whose body is the
    :meth:`thoth.ingest.Ingestor._binary_stub_body` provenance stub -- has no recoverable
    bytes, so it is skipped-and-logged and NOT yielded (v1 text scope).

    Args:
        vault: The real, path-confined vault facade to read holds from.

    Yields:
        ``(hold_rel_path, Capture)`` for each recoverable text hold, sorted by path.
    """
    from thoth.ingest import Capture

    inbox = vault.root / "inbox"
    if not inbox.is_dir():
        return
    for entry in sorted(inbox.glob("hold-*.md"), key=lambda item: item.name):
        rel = f"inbox/{entry.name}"
        try:
            page = vault.read_page(rel)
        except VaultError as exc:
            logger.warning("inbox drain: skip %s (unreadable: %s)", rel, exc)
            continue
        body = page.body
        if _is_binary_stub(body):
            logger.info("inbox drain: skip %s (binary stub, no recoverable bytes)", rel)
            continue
        source = _resolve_source(page.frontmatter.get("source"))
        yield rel, Capture(text=body, source=source)


def _is_binary_stub(body: str) -> bool:
    """Return whether ``body`` is the binary-capture provenance stub (skip, v1 text)."""
    stripped = body.lstrip()
    if not stripped.startswith(_BINARY_STUB_HEAD):
        return False
    return any(marker in body for marker in _BINARY_STUB_MARKERS)


def _resolve_source(raw: object) -> str:
    """Validate a hold's ``source:`` against VALID_SOURCES, else fall back to import."""
    if isinstance(raw, str) and raw in VALID_SOURCES:
        return raw
    return _FALLBACK_SOURCE
