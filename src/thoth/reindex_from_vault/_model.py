"""Reindex vocabulary and pure helpers (folders, result types, page parsing)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import yaml  # transitive dep of python-frontmatter, which uses it as its handler

from thoth.config import Config
from thoth.vault import ACTIONABLE_DIRS, CURATED_DIRS

INDEXED_DIRS: tuple[str, ...] = (*CURATED_DIRS, *ACTIONABLE_DIRS)
"""The content folders the reindex walks (SPEC section 8, ADR 0004 and ADR 0005).

Per ADR 0004 the index covers all content pages, so "have I ever noted anything about
X?" reaches the reference folders and the actionable one. Recall precision for knowledge
Q&A is preserved by scoping recall on the ``page_type`` tag at query time, not by
excluding folders here. Both lists stay canonical in :mod:`thoth.vault`.

``inbox/`` is transient deferred-capture holding and ``raw/`` is immutable, often-long
source bytes needing a chunking strategy Hindsight does not do, so both are excluded.
The underscore directories are structure rather than facts and are never walked.
"""

SKIP_FILES: frozenset[str] = frozenset({"SCHEMA.md", "index.md", "log.md"})
"""Spine files never retained, even when they land inside an indexed folder.

``index.md`` is the Home landing page, ``SCHEMA.md`` holds the conventions and
``log.md`` is the append-only action log. All three are structure, not knowledge.
"""


class ReindexError(Exception):
    """Raised when a reindex step fails hard (a checked retain or a bank reset)."""


@dataclass(frozen=True, slots=True)
class ReindexResult:
    """Counts summarising one :meth:`Reindexer.run` pass.

    Attributes:
        changed: Pages retained this run, or every live page on a full rebuild
        skipped: Live pages whose body hash matched the manifest
        pruned: Manifest entries forgotten because the page is gone
        live_pages: Distinct curated pages seen on disk this run
        full_rebuild: Whether this pass wiped the bank and re-retained every page
        aborted: Whether the daily LLM budget (issue #16) was hit mid-walk. Pages
            retained before the cap are recorded, but pruning is skipped because the
            walk is incomplete, and no liveness marker is written
    """

    changed: int
    skipped: int
    pruned: int
    live_pages: int
    full_rebuild: bool
    aborted: bool = False


def manifest_path(config: Config) -> Path:
    """Returns the index-side manifest path for ``config``.

    The manifest lives outside the vault under the Hindsight state dir and is
    gitignored, so the reindex never touches the canonical vault to track its own
    bookkeeping.

    Args:
        config: The frozen runtime configuration, supplying ``thoth_home``

    Returns:
        The absolute path to ``reindex-manifest.json``
    """
    return config.thoth_home / "hindsight" / "reindex-manifest.json"


def page_type(markdown: str) -> str:
    """Returns the leading frontmatter ``type:``, or ``"page"`` when it is absent.

    This only tags a retained fact for recall filtering, never a confinement or contract
    decision, so a missing, empty or unparseable type degrades to the neutral ``"page"``
    rather than raising.

    Args:
        markdown: The full page text, frontmatter and body

    Returns:
        The ``type`` value coerced with :class:`str`, else ``"page"``
    """
    try:
        value = frontmatter.loads(markdown).get("type")
    except yaml.YAMLError:
        return "page"
    if value is None:
        return "page"
    return str(value) or "page"


def _split_body(markdown: str) -> str:
    """Strips a leading YAML frontmatter block, returning the body text.

    Delegates to ``python-frontmatter``, the same parser
    :meth:`thoth.vault.Vault.read_page` uses, so the body hashed here is byte-identical
    to ``read_page(...).body`` for the same file and the idempotency key stays
    consistent across the whole appliance. A document with no frontmatter yields its
    full text.

    Args:
        markdown: The full page text, or a bare body

    Returns:
        The body with any single leading frontmatter block removed
    """
    return frontmatter.loads(markdown).content


def _now_iso() -> str:
    """Returns the current UTC instant as an ISO-8601 manifest timestamp."""
    return datetime.now(UTC).isoformat()
