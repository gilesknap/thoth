"""Reindex vocabulary and pure helpers (folders, result types, page parsing)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import yaml  # transitive dep of python-frontmatter (its YAML handler)

from thoth.config import Config
from thoth.vault import ACTIONABLE_DIRS, CURATED_DIRS

INDEXED_DIRS: tuple[str, ...] = (*CURATED_DIRS, *ACTIONABLE_DIRS)
"""The content folders the reindex walks (SPEC section 8; ADR 0004 + ADR 0005).

Per ADR 0004, the index covers **all** content pages, so "have I ever noted anything
about X?" reaches the reference folders (:data:`thoth.vault.CURATED_DIRS`:
``entities``/``notes``/``memories``) **and** the actionable folder
(:data:`thoth.vault.ACTIONABLE_DIRS`: ``actions``, which also holds the media queue).
Recall precision for knowledge Q&A is preserved by **scoping recall on the ``page_type``
tag** at query time (see :meth:`thoth.query.QueryEngine.recall_paths`), not by excluding
folders here. Both lists stay canonical in :mod:`thoth.vault` so the vocabulary lives in
one place.

``inbox/`` (transient deferred-capture holding) and ``raw/`` (immutable, often-long
source bytes needing a chunking strategy Hindsight does not do) remain excluded;
navigational/meta and the underscore directories (``_bases/``/``_meta/``/``_archive/``)
are structure, not facts, and are never walked.
"""

SKIP_FILES: frozenset[str] = frozenset({"SCHEMA.md", "index.md", "log.md"})
"""Spine files never retained even if they land inside an indexed folder.

``index.md`` is the Home landing page (there is no separate ``Home.md``); ``SCHEMA.md``
holds conventions and ``log.md`` is the append-only action log. All three are structure,
not curated knowledge.
"""


class ReindexError(Exception):
    """Raised when a reindex step fails hard (a checked retain or a bank reset)."""


@dataclass(frozen=True, slots=True)
class ReindexResult:
    """Counts summarising one :meth:`Reindexer.run` pass.

    Attributes:
        changed: Pages retained this run (new or body-changed, or every live page on a
            full rebuild).
        skipped: Live pages whose body hash matched the manifest and were not retained.
        pruned: Manifest entries for pages no longer present that were forgotten.
        live_pages: Distinct curated pages seen on disk this run.
        full_rebuild: Whether this pass wiped the bank and re-retained every page.
        aborted: Whether the daily LLM budget (issue #16) was hit mid-walk, so the run
            stopped early; the pages retained before the cap are recorded in the
            manifest, but pruning is skipped (the walk is incomplete) and no liveness
            marker is recorded. ``False`` on a normal complete pass.
    """

    changed: int
    skipped: int
    pruned: int
    live_pages: int
    full_rebuild: bool
    aborted: bool = False


def manifest_path(config: Config) -> Path:
    """Return the index-side manifest path for ``config``.

    The manifest lives outside the vault under the Hindsight state dir
    (``<thoth_home>/hindsight/reindex-manifest.json``) and is ``.gitignore``d, so the
    reindex never touches the canonical vault to track its own bookkeeping.

    Args:
        config: The frozen runtime configuration (supplies ``thoth_home``).

    Returns:
        The absolute path to ``reindex-manifest.json``.
    """
    return config.thoth_home / "hindsight" / "reindex-manifest.json"


def page_type(markdown: str) -> str:
    """Return the leading frontmatter ``type:`` value, or ``"page"`` when absent.

    This is used only to tag a retained fact for recall filtering (alongside the vault
    path), never for any confinement or contract decision, so a missing, empty, or
    unparseable type degrades to the neutral ``"page"`` rather than raising.

    Args:
        markdown: The full page text (frontmatter + body).

    Returns:
        The ``type`` value (for example ``"entity"``; non-string YAML scalars are
        coerced with :class:`str`) or ``"page"`` when the leading frontmatter block
        has no non-empty ``type`` key.
    """
    try:
        value = frontmatter.loads(markdown).get("type")
    except yaml.YAMLError:
        return "page"
    if value is None:
        return "page"
    return str(value) or "page"


def _split_body(markdown: str) -> str:
    """Strip a leading YAML frontmatter block, returning the body text.

    This delegates to ``python-frontmatter`` (the same parser
    :meth:`thoth.vault.Vault.read_page` uses), so the body fed to
    :meth:`thoth.vault.Vault.body_sha256` here is byte-identical to
    ``read_page(...).body`` for the same file -- guaranteeing the body-hash idempotency
    key is consistent across the whole appliance. A document with no leading frontmatter
    block yields its full text as the body.

    Args:
        markdown: The full page text (frontmatter + body), or a bare body.

    Returns:
        The body with any single leading frontmatter block removed.
    """
    return frontmatter.loads(markdown).content


def _now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string (manifest timestamp)."""
    return datetime.now(UTC).isoformat()
