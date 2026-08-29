"""Rebuild or incrementally refresh the Hindsight index from the canonical vault.

Hindsight is a *rebuildable derived index* over the canonical Obsidian vault (SPEC
sections 8 and 15), never the store of record. This package makes the "vault
canonical, index disposable" rule real. It walks the curated knowledge folders in
:data:`INDEXED_DIRS`, computes a per-page content hash over the page **body**,
everything after the closing frontmatter ``---``, and retains only the pages whose
body changed since the last run.

Two facts from the SPEC shape the design:

* **One Hindsight reference is one vault-relative page path, keyed by a body hash.**
  The hash is :meth:`thoth.vault.Vault.body_sha256` over the page body, with the
  frontmatter stripped by :func:`_split_body`, the same split
  :meth:`thoth.vault.Vault.read_page` performs. Bumping a page's ``updated:``
  frontmatter without touching the body is therefore *not* a change and triggers no
  embedding work. An index-side manifest **outside** the vault tracks the hash per
  page, at :func:`manifest_path`, which is
  ``<thoth_home>/hindsight/reindex-manifest.json`` and ``.gitignore``d, so a reindex
  never churns a curated page's ``updated:`` date.

* **The full-rebuild bank wipe is an HTTP DELETE of the bank.**
  An incremental run reuses the :meth:`~thoth.hindsight.Hindsight.forget` and
  :meth:`~thoth.hindsight.Hindsight.retain` surface, forgetting then retaining per
  changed page, and forgetting then pruning per deleted page. The full-rebuild wipe
  delegates to :meth:`~thoth.hindsight.Hindsight.reset_bank`, a ``DELETE`` of the bank
  that removes it and all its data, with the next retain auto-recreating it. The
  reindexer therefore never touches the Hindsight transport directly, and a test
  substitutes a fake :class:`~thoth.hindsight.Hindsight`.

There are three reindex triggers (SPEC section 8). The per-ingest incremental one is
handled by the ingest pass rather than here. ``thoth reindex`` is the nightly catch-up
for an out-of-band Obsidian edit. ``thoth reindex --full-rebuild`` is the manual or
on-recovery full rebuild.

Module top level imports only the standard library, :class:`thoth.config.Config`,
:class:`thoth.hindsight.Hindsight` and :class:`thoth.vault.Vault`, so importing this
package at pytest collection is always CI-safe, even where the ``hindsight-api`` server
is absent.
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
