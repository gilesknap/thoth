"""Rebuild or incrementally refresh the Hindsight index from the canonical vault.

Hindsight is a *rebuildable derived index* over the canonical Obsidian vault (SPEC
sections 8 and 15), never the store of record. This package is the "vault canonical,
index disposable" mechanism made real: it walks the curated knowledge folders
(:data:`INDEXED_DIRS`), computes a per-page content hash over the page **body**
(everything after the closing frontmatter ``---``), and retains only the pages whose
body changed since the last run.

Two facts from the SPEC shape the design:

* **One Hindsight reference == one vault-relative page path, keyed by a body hash.**
  The hash is :meth:`thoth.vault.Vault.body_sha256` over the page body (frontmatter
  stripped via :func:`_split_body`, the same split :meth:`thoth.vault.Vault.read_page`
  performs), so bumping a page's ``updated:`` frontmatter without touching the body is
  *not* a change and triggers no embedding work. The hash per page is tracked in an
  index-side manifest **outside** the vault
  (:func:`manifest_path` -> ``<thoth_home>/hindsight/reindex-manifest.json``, which is
  ``.gitignore``d), so a reindex never churns curated pages' ``updated:`` dates.

* **The full-rebuild bank wipe is an HTTP DELETE of the bank.**
  Incremental runs reuse the :meth:`~thoth.hindsight.Hindsight.forget` /
  :meth:`~thoth.hindsight.Hindsight.retain` surface (forget-then-retain per changed
  page; forget-and-prune per deleted page). The full-rebuild wipe delegates to
  :meth:`~thoth.hindsight.Hindsight.reset_bank` (a ``DELETE`` of the bank, which removes
  the bank and all its data; the next retain auto-recreates it), so the reindexer never
  touches the Hindsight transport directly and tests substitute a fake
  :class:`~thoth.hindsight.Hindsight`.

The three reindex triggers (SPEC section 8) are: per-ingest incremental (handled by the
ingest pass, not here), nightly catch-up for out-of-band Obsidian edits (``thoth
reindex``), and a manual/on-recovery full rebuild (``thoth reindex --full-rebuild``).

Only the standard library plus :class:`thoth.config.Config`,
:class:`thoth.hindsight.Hindsight`, and :class:`thoth.vault.Vault` are imported at
module top level, so importing this package at pytest collection is always CI-safe even
where the ``hindsight-api`` server is absent.
"""

from ._model import (
    INDEXED_DIRS,
    SKIP_FILES,
    ReindexError,
    ReindexResult,
    manifest_path,
    page_type,
)
from ._model import (
    _split_body as _split_body,
)
from .reindexer import Reindexer

__all__ = [
    "INDEXED_DIRS",
    "SKIP_FILES",
    "ReindexError",
    "ReindexResult",
    "Reindexer",
    "manifest_path",
    "page_type",
]
