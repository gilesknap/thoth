"""Sweeps the durable inbox holds back through ingest (issue #105).

A budget-capped bulk import, or an LLM-unavailable capture, leaves the item durable as a
hold but never curated. Grep cannot see it, because the searched folders exclude the
inbox, and the index never retained it, so the content is stranded.

This is the source-independent drain. It walks the holds and yields one capture per
recoverable hold, built from the stored body and threaded source, ready to feed straight
into the existing pipeline. It is symmetric to :mod:`thoth.capture_walk`: a second
capture source, not a new ingest pass.

Because the hold slug is re-derived from the body digest, re-persisting an identical
body lands on the same path, so the caller can remove the original hold once the page is
filed. The body is passed verbatim, with no string-rewriting.

Each hold records its intended curation mode and original filename (issue #95). The
drain reads them back and yields the as-is flag alongside the capture, so the sweep
re-files with the original intent rather than guessing. An unknown mode falls back to
re-curating, the safe default, so an older or hand-written hold never aborts the sweep.

Scope is text holds. A binary hold carries only a provenance stub, its bytes never
having been recoverable, so it is skipped and logged rather than re-fed. Detection is a
content sniff on the stub's marker lines, since the frontmatter carries no binary flag.

Only the standard library, a deferred import of the capture type, and the vault read
surface are used. No LLM and no network.
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
        rel: The hold's vault path, so the caller can remove it once filed.
        capture: Built from the hold's stored body, source and original filename.
        as_is: Whether the hold was captured in low-touch mode, so the sweep re-files
            it as-is rather than re-curating.
    """

    rel: str
    capture: Capture
    as_is: bool


# The source stamped on a hold whose own is missing or unknown, so one odd hold never
# aborts a sweep
_FALLBACK_SOURCE: str = "import"

# Sentinel lines from the binary stub body. A hold whose body is that stub carries no
# recoverable bytes, so it is skipped. This is the single place the sniff is expressed,
# so if the stub wording changes this constant must change with it
_BINARY_STUB_HEAD: str = "# Held capture"
_BINARY_STUB_MARKERS: tuple[str, ...] = (
    "Binary source:",
    "Unsupported binary content",
)


def drain_captures(vault: Vault) -> Iterator[DrainedHold]:
    """Yields one drained hold per recoverable text hold under the inbox.

    Walks the holds in sorted path order for determinism. Each stored body becomes a
    text capture carrying the hold's source, validated and falling back to import when
    absent so one odd hold never aborts the sweep, and its original filename.

    The stamped mode decides the as-is flag, so the sweep re-files with the original
    intent, and an unknown mode falls back to re-curating.

    The body is passed verbatim. A binary hold has no recoverable bytes, so it is
    skipped and logged rather than yielded.

    Args:
        vault: The path-confined vault facade to read holds from.

    Yields:
        Each recoverable text hold, sorted by path.
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
    """Reports whether a body is the binary-capture provenance stub."""
    stripped = body.lstrip()
    if not stripped.startswith(_BINARY_STUB_HEAD):
        return False
    return any(marker in body for marker in _BINARY_STUB_MARKERS)


def _resolve_source(raw: object) -> str:
    """Validates a hold's source, falling back to import when it is unknown."""
    if isinstance(raw, str) and raw in VALID_SOURCES:
        return raw
    return _FALLBACK_SOURCE


def _resolve_filename(raw: object) -> str | None:
    """Returns the hold's original filename when present, otherwise None."""
    return raw if isinstance(raw, str) and raw else None


def _resolve_as_is(raw: object) -> bool:
    """Reports whether a hold's stamped mode is the low-touch as-is mode.

    The mode vocabulary is imported at call time from its single source, mirroring the
    module's deferred-import contract. Anything other than the explicit marker,
    including a missing or hand-written mode, falls back to re-curating, the safe
    default.
    """
    from thoth.ingest import HOLD_MODE_AS_IS

    return raw == HOLD_MODE_AS_IS
