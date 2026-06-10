"""Rebuild or incrementally refresh the Hindsight index from the canonical vault.

Hindsight is a *rebuildable derived index* over the canonical Obsidian vault (SPEC
sections 8 and 15), never the store of record. This module is the "vault canonical,
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
module top level, so importing this module at pytest collection is always CI-safe even
where the ``hindsight-api`` server is absent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import yaml  # transitive dep of python-frontmatter (its YAML handler)

from thoth.budget import BudgetExceededError
from thoth.config import Config
from thoth.hindsight import Hindsight, HindsightError
from thoth.state import MARKER_REINDEX, MarkerStore
from thoth.vault import ACTIONABLE_DIRS, CURATED_DIRS, Vault

__all__ = [
    "INDEXED_DIRS",
    "SKIP_FILES",
    "ReindexError",
    "ReindexResult",
    "Reindexer",
    "manifest_path",
    "page_type",
]

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


class Reindexer:
    """Incremental + full-rebuild reindexer over the canonical vault.

    Construct it from the frozen :class:`~thoth.config.Config`, a real
    :class:`~thoth.vault.Vault` (for the root walk and the body-hash key), and a
    :class:`~thoth.hindsight.Hindsight` (for ``retain``/``forget`` and the full-rebuild
    :meth:`~thoth.hindsight.Hindsight.reset_bank` wipe). Every Hindsight operation goes
    through that injected wrapper, so tests substitute a fake.
    """

    def __init__(
        self,
        config: Config,
        vault: Vault,
        hindsight: Hindsight,
        *,
        markers: MarkerStore | None = None,
    ) -> None:
        """Build a :class:`Reindexer`.

        Args:
            config: The frozen runtime configuration (supplies ``thoth_home`` for the
                manifest path).
            vault: The path-confined vault facade; provides the root to walk and the
                body-hash idempotency key.
            hindsight: The semantic-index wrapper used for ``retain``, ``forget``, and
                the full-rebuild ``reset_bank`` wipe.
            markers: Optional liveness :class:`~thoth.state.MarkerStore`; when wired, a
                successful :meth:`run` records a ``reindex`` marker so the daily
                heartbeat can report "last reindex at T" (issue #15). ``None`` (the
                default) disables recording, so existing callers/tests are unaffected.
        """
        self._config = config
        self._vault = vault
        self._hindsight = hindsight
        self._markers = markers

    @property
    def manifest_file(self) -> Path:
        """The index-side manifest path for this reindexer's config."""
        return manifest_path(self._config)

    def body_hash(self, markdown: str) -> str:
        """Return the body SHA-256 idempotency key for a page's full text.

        The leading frontmatter block is stripped (:func:`_split_body`) and the body is
        hashed with :meth:`thoth.vault.Vault.body_sha256`, so the key is identical to
        ``Vault.body_sha256(read_page(...).body)`` and is invariant under a frontmatter
        ``updated:`` bump.

        Args:
            markdown: The full page text (frontmatter + body).

        Returns:
            The hex SHA-256 of the page body.
        """
        return self._vault.body_sha256(_split_body(markdown))

    def load_manifest(self) -> dict[str, dict[str, str]]:
        """Load the body-hash manifest, treating a missing/corrupt file as empty.

        Returns:
            A mapping of vault-relative path -> ``{"sha256": ..., "retained_at": ...}``.
            A missing file, an empty file, or any JSON/shape error yields ``{}`` (the
            index is disposable, so a damaged manifest just forces a full re-walk rather
            than crashing).
        """
        path = self.manifest_file
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        manifest: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                manifest[key] = {
                    str(k): str(v) for k, v in value.items() if isinstance(k, str)
                }
        return manifest

    def write_manifest(self, manifest: dict[str, dict[str, str]]) -> None:
        """Atomically write the manifest, creating parent directories as needed.

        Args:
            manifest: The path -> ``{"sha256", "retained_at"}`` mapping to persist.
        """
        path = self.manifest_file
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(manifest, indent=2, sort_keys=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def reset_bank(self) -> None:
        """Wipe the Hindsight bank for a full rebuild.

        Delegates to :meth:`~thoth.hindsight.Hindsight.reset_bank`, which issues a
        ``DELETE`` of the bank: it removes the bank and all its data, and the next
        re-retain auto-recreates it. A failed wipe is a hard error because a full
        rebuild must not re-retain on top of stale facts.

        Raises:
            ReindexError: if the wipe fails (wrapping
                :class:`~thoth.hindsight.HindsightError`).
        """
        try:
            self._hindsight.reset_bank()
        except HindsightError as exc:
            raise ReindexError(f"hindsight bank reset failed: {exc}") from exc

    def run(self, *, full_rebuild: bool = False) -> ReindexResult:
        """Reindex the vault, retaining changed pages and pruning deleted ones.

        On ``full_rebuild`` the manifest is ignored, :meth:`reset_bank` is run first,
        and every live page is re-retained. Otherwise pages whose body hash matches the
        manifest are skipped with zero embedding work. For every retained page the prior
        facts are forgotten first (forget-then-retain, so a body edit replaces rather
        than duplicates), then retained with ``tags=[page_type, rel]``. After the walk,
        manifest entries for pages no longer on disk are forgotten and dropped.

        Args:
            full_rebuild: When ``True``, wipe the bank and re-retain every live page
                even if its body hash is unchanged.

        Returns:
            A :class:`ReindexResult` with the changed/skipped/pruned/live counts.

        Raises:
            ReindexError: if :meth:`reset_bank` fails, or a checked
                :meth:`~thoth.hindsight.Hindsight.retain` raises
                :class:`~thoth.hindsight.HindsightError` (the page stays in the vault
                and its manifest entry is not advanced).
        """
        manifest = {} if full_rebuild else self.load_manifest()
        if full_rebuild:
            self.reset_bank()

        seen: set[str] = set()
        changed = 0
        skipped = 0
        aborted = False
        for rel, markdown in self._iter_pages():
            seen.add(rel)
            body = _split_body(markdown)
            digest = self._vault.body_sha256(body)
            if not full_rebuild and manifest.get(rel, {}).get("sha256") == digest:
                skipped += 1
                continue
            try:
                self._retain_page(rel, markdown, body)
            except BudgetExceededError:
                # The daily LLM budget (issue #16) was reached mid-rebuild. Stop
                # cleanly: the pages retained so far are advanced in the manifest below,
                # but the walk is incomplete so we must NOT prune (unseen pages were not
                # visited, not deleted) and must not record the reindex liveness marker.
                # The guard has already emitted the one-per-day notification.
                seen.discard(rel)
                aborted = True
                break
            manifest[rel] = {"sha256": digest, "retained_at": _now_iso()}
            changed += 1

        pruned = 0 if aborted else self._prune_deleted(manifest, seen)
        self.write_manifest(manifest)
        if not aborted:
            self._record_marker()
        return ReindexResult(
            changed=changed,
            skipped=skipped,
            pruned=pruned,
            live_pages=len(seen),
            full_rebuild=full_rebuild,
            aborted=aborted,
        )

    def _record_marker(self) -> None:
        """Record the ``reindex`` liveness marker (best-effort, issue #15).

        A reindex that completed the walk + manifest write is a live signal; a failure
        to write the disposable marker DB must not turn a successful reindex into an
        error, so any error is swallowed.
        """
        if self._markers is None:
            return
        try:
            self._markers.record(MARKER_REINDEX)
        except Exception:  # noqa: BLE001 - marker bookkeeping is best-effort
            pass

    # ---- internals ---------------------------------------------------------------

    def _iter_pages(self) -> list[tuple[str, str]]:
        """Walk the indexed folders, yielding ``(vault_rel_path, text)`` per page.

        Walks each :data:`INDEXED_DIRS` folder recursively for ``*.md`` files, skipping
        :data:`SKIP_FILES` by basename, and returns deterministic, sorted
        ``(rel, text)`` pairs. Folders that do not exist are silently skipped (a fresh
        vault may lack some). The relative path uses POSIX separators so it matches the
        in-band ``SOURCE:`` sentinel and the manifest keys on every platform.

        Returns:
            Sorted ``(vault-relative path, full markdown text)`` pairs.
        """
        root = self._vault.root
        pages: list[tuple[str, str]] = []
        for folder in INDEXED_DIRS:
            base = root / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*.md"):
                if path.name in SKIP_FILES:
                    continue
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                pages.append((rel, path.read_text(encoding="utf-8")))
        pages.sort(key=lambda item: item[0])
        return pages

    def _retain_page(self, rel: str, markdown: str, body: str) -> None:
        """Forget any stale facts for ``rel`` then retain the current body.

        Args:
            rel: The vault-relative page path (the Hindsight reference / manifest key).
            markdown: The full page text (supplies the :func:`page_type` retain tag).
            body: The page body with the leading frontmatter already stripped
                (:func:`_split_body`), so only the body is retained, matching the
                body-hash idempotency key.

        Raises:
            ReindexError: if the checked retain raises
                :class:`~thoth.hindsight.HindsightError`.
        """
        self._hindsight.forget(rel)
        try:
            self._hindsight.retain(rel, body, tags=[page_type(markdown), rel])
        except HindsightError as exc:
            raise ReindexError(f"retain failed for {rel!r}: {exc}") from exc

    def _prune_deleted(
        self, manifest: dict[str, dict[str, str]], seen: set[str]
    ) -> int:
        """Forget and drop manifest entries for pages no longer on disk.

        Args:
            manifest: The manifest being updated in place.
            seen: The set of vault-relative paths observed on this walk.

        Returns:
            The number of pruned (forgotten + removed) manifest entries.
        """
        gone = [rel for rel in manifest if rel not in seen]
        for rel in gone:
            self._hindsight.forget(rel)
            del manifest[rel]
        return len(gone)
