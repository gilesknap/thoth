"""Rebuild or incrementally refresh the Hindsight index from the canonical vault.

Hindsight is a rebuildable derived index over the vault (SPEC sections 8 and 15), never
the store of record. This package walks the curated knowledge folders
(:data:`INDEXED_DIRS`), hashes each page body, and retains only the pages whose body
changed since the last run. Two facts from the SPEC shape the design:

* **One Hindsight reference is one vault-relative page path, keyed by a body hash.** The
  hash covers the body alone, the same split :meth:`thoth.vault.Vault.read_page`
  performs, so bumping a page's ``updated:`` frontmatter without touching the body
  triggers no embedding work. The hashes live in a manifest outside the vault
  (:func:`manifest_path`, which is gitignored), so a reindex never churns curated pages.
* **The full-rebuild bank wipe is an HTTP DELETE of the bank.** Incremental runs
  forget-then-retain per changed page and forget-and-prune per deleted page, while a
  full rebuild delegates to :meth:`~thoth.hindsight.Hindsight.reset_bank`, which the
  next retain auto-recreates. So the reindexer never touches the Hindsight transport
  directly and tests substitute a fake.

The three triggers (SPEC section 8) are per-ingest incremental, handled by the ingest
pass rather than here, nightly catch-up for out-of-band Obsidian edits (``thoth
reindex``), and a manual or on-recovery full rebuild (``thoth reindex --full-rebuild``).
Only the standard library plus :class:`thoth.config.Config`,
:class:`thoth.hindsight.Hindsight` and :class:`thoth.vault.Vault` are imported at module
level, so importing this at pytest collection is CI-safe even where the
``hindsight-api`` server is absent.
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
