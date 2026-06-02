"""Sweep the durable ``inbox/hold-*.md`` holds back through ingest (issue #105).

A budget-capped bulk import (or an LLM-unavailable capture) leaves the inbound item
durable as ``inbox/hold-<sha12>.md`` (``type: inbox``) but never curated, so grep cannot
see it (its :data:`thoth.query.SEARCHED_DIRS` excludes ``inbox/``) and Hindsight never
retained it -- the content is stranded. This module is the source-independent drain: it
walks the holds and yields one :class:`~thoth.ingest.Capture` per recoverable hold,
built from the hold's STORED body and threaded ``source:``, ready to feed straight
the EXISTING :meth:`thoth.ingest.Ingestor.ingest` pipeline (classify/curate -> file ->
retain). It is symmetric to :mod:`thoth.capture_walk`: a second capture source, not a
new ingest pass.

Because :meth:`Ingestor.persist_inbound` re-derives the hold slug from the body SHA-256,
re-persisting an identical body lands on the SAME ``inbox/hold-*`` path, so the caller
can remove the original hold by its path once the page is filed (the re-persist and the
original are one file). The body is passed VERBATIM -- no string-rewriting (the
project's hard rule).

Each hold records the intended curation ``mode`` (``curate``/``as-is``) and the original
``filename`` in its frontmatter (issue #95, task E). The drain reads them back and
yields the ``as_is`` flag alongside the :class:`~thoth.ingest.Capture` (threading the
original filename onto it), so the sweep re-files each hold with the ORIGINAL intent
rather than guessing -- a capture deferred under ``--as-is`` re-files low-touch, the
default re-curates. A missing/unknown ``mode`` falls back to the curate default (the
safe one), so an older or hand-written hold never aborts the sweep.

Scope v1 is TEXT holds. A binary hold carries only the
:meth:`Ingestor._binary_stub_body` provenance stub (its bytes were never recoverable),
so it is skipped-and-logged rather than re-fed: there is nothing to re-curate. Detection
is a content sniff on the stub's stored marker lines (kept here as a single constant)
since the hold frontmatter carries no binary flag.

Only the standard library plus a deferred import of :data:`thoth.ingest.Capture` (inside
the generator body, mirroring :mod:`thoth.capture_walk`'s import-safety contract) and
the :class:`~thoth.vault.Vault` read surface are used -- no LLM, no network.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

from thoth.vault import VALID_SOURCES, Vault, VaultError

if TYPE_CHECKING:
    from thoth.ingest import Capture

__all__ = ["DrainedHold", "drain_captures"]

logger = logging.getLogger(__name__)


class DrainedHold(NamedTuple):
    """One recoverable inbox hold ready to re-feed, plus its intent (issue #95).

    Attributes:
        rel: The vault-relative ``inbox/hold-*.md`` path (so the caller can remove it
            once the page is filed).
        capture: The :class:`~thoth.ingest.Capture` built from the hold's stored body,
            threaded ``source:`` and original ``filename:``.
        as_is: Whether the hold was captured in low-touch ``--as-is`` mode, so the sweep
            re-files it as-is rather than re-curating (read from the hold's ``mode:``).
    """

    rel: str
    capture: Capture
    as_is: bool


# The source stamped on a hold whose frontmatter ``source:`` is missing or not a known
# value, so one odd hold never aborts a sweep. ``import`` is a member of VALID_SOURCES.
_FALLBACK_SOURCE: str = "import"

# Sentinel lines from :meth:`thoth.ingest.Ingestor._binary_stub_body`: a hold whose
# body is that provenance stub carries no recoverable bytes, so it is skipped (v1 text).
# Kept here as the single place the sniff is expressed -- if the stub wording changes,
# this constant must change with it.
_BINARY_STUB_HEAD: str = "# Held capture"
_BINARY_STUB_MARKERS: tuple[str, ...] = (
    "Binary source:",
    "Unsupported binary content",
)


def drain_captures(vault: Vault) -> Iterator[DrainedHold]:
    """Yield a :class:`DrainedHold` per recoverable text hold under ``inbox/``.

    Walks ``inbox/hold-*.md`` in sorted path order (deterministic). For each, the hold's
    stored body becomes a ``text`` :class:`~thoth.ingest.Capture` carrying the hold's
    threaded ``source:`` (validated against :data:`~thoth.vault.VALID_SOURCES`, falling
    back to ``import`` when absent/unknown so one odd hold never aborts the sweep) and
    its original ``filename:`` (issue #95, task E). The hold's stamped ``mode:`` decides
    the ``as_is`` flag so the sweep re-files with the ORIGINAL intent (a missing/unknown
    mode falls back to re-curate, the safe default). The body is passed verbatim.

    A binary hold -- whose body is the
    :meth:`thoth.ingest.Ingestor._binary_stub_body` provenance stub -- has no
    recoverable bytes, so it is skipped-and-logged and NOT yielded (v1 text scope).

    Args:
        vault: The real, path-confined vault facade to read holds from.

    Yields:
        A :class:`DrainedHold` for each recoverable text hold, sorted by path.
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
        filename = _resolve_filename(page.frontmatter.get("filename"))
        as_is = _resolve_as_is(page.frontmatter.get("mode"))
        yield DrainedHold(
            rel=rel,
            capture=Capture(text=body, source=source, filename=filename),
            as_is=as_is,
        )


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


def _resolve_filename(raw: object) -> str | None:
    """Return the hold's original ``filename:`` when present, else ``None``."""
    return raw if isinstance(raw, str) and raw else None


def _resolve_as_is(raw: object) -> bool:
    """Return whether the hold's stamped ``mode:`` is the low-touch as-is mode.

    Imports the mode vocabulary from :mod:`thoth.ingest` (the single source of the hold
    mode strings) at call time, mirroring the module's deferred-import contract. Any
    value other than the explicit as-is marker -- including a missing or hand-written
    mode -- falls back to ``False`` (re-curate), the safe default.
    """
    from thoth.ingest import HOLD_MODE_AS_IS

    return raw == HOLD_MODE_AS_IS
