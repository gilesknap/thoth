"""The :class:`LintEngine`: the vault walk plus thin ``check_*`` delegations.

The engine owns the only disk surface of the scan, parsing pages out of the vault
folders and reading spine files, and hands the parsed pages to the pure check
functions in the ``checks_*`` modules. Check 13, :meth:`LintEngine.record`, appends
the single ``log.md`` entry.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import frontmatter
import yaml

from thoth._time import LONDON
from thoth.config import Config
from thoth.vault import ACTIONABLE_DIRS, ASSET_SLUG_RE, CURATED_DIRS, Vault

from .checks_freshness import (
    _check_log_rotation,
    _check_page_size,
    _check_source_drift,
    _check_stale,
)
from .checks_links import (
    _ASSETS_DIR,
    _check_broken_links,
    _check_image_hygiene,
    _check_link_style,
    _check_orphans,
)
from .checks_metadata import (
    _check_contradictions,
    _check_frontmatter,
    _check_quality_signals,
    _check_summaries,
    _check_tag_audit,
)
from .model import Finding, LintError, LintReport, _Page

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

__all__ = [
    "SPINE_FILES",
    "EXCLUDED_DIRS",
    "LintEngine",
]

SPINE_FILES: frozenset[str] = frozenset({"index.md", "SCHEMA.md", "log.md"})
"""Structural backbone files (matches ``reindex.SKIP_FILES``); not curated knowledge."""

EXCLUDED_DIRS: frozenset[str] = frozenset({"_bases", "_meta", "_archive", ".obsidian"})
"""Structural directories excluded from the orphan / index / size scans (SPEC 5)."""

# Immutable raw source subdirs whose sha256 frontmatter is drift-checked (check 7).
_RAW_DIRS: tuple[str, ...] = ("articles", "papers", "transcripts")


class LintEngine:
    """Pure, deterministic vault linter built from a frozen Config and Vault.

    All retrieval is a pure read over the vault folders, using no LLM and no network.
    The single non-deterministic input, the current calendar date, is injected as
    ``today``, so the stale, overdue and media-cold windows are reproducible under a
    frozen clock in tests.
    """

    def __init__(
        self, config: Config, vault: Vault, *, today: date | None = None
    ) -> None:
        """Store collaborators and resolve the injected clock to a London date.

        Args:
            config: The frozen runtime config, carried for symmetry with
                :class:`~thoth.summary.SummaryEngine`, since lint reads no new field.
            vault: The path-confined vault facade, the only disk surface used.
            today: The calendar date used for every stale and overdue window. With
                ``None``, the current Europe/London date is used.
        """
        self._config = config
        self._vault = vault
        self._today = today if today is not None else datetime.now(LONDON).date()

    @property
    def today(self) -> date:
        """The calendar date used for the stale, overdue and media-cold windows."""
        return self._today

    # ---- aggregate -------------------------------------------------------------------

    def run(self) -> LintReport:
        """Run checks 1-12 and 14, aggregating a sorted :class:`~thoth.lint.LintReport`.

        Findings are concatenated across the checks and sorted by
        ``(severity, check, path)``, so the report is deterministic. Check 13,
        :meth:`record`, is *not* run here, because the caller decides whether to log.

        Returns:
            The aggregated :class:`~thoth.lint.LintReport`.

        Raises:
            thoth.lint.LintError: when a check cannot run, as with a missing vault root
                or a missing ``SCHEMA.md`` for the tag audit.
        """
        checks = (
            self.check_orphans,
            self.check_broken_links,
            self.check_summaries,
            self.check_frontmatter,
            self.check_stale,
            self.check_contradictions,
            self.check_source_drift,
            self.check_quality_signals,
            self.check_page_size,
            self.check_tag_audit,
            self.check_image_hygiene,
            self.check_log_rotation,
            self.check_link_style,
        )
        findings: list[Finding] = []
        for check in checks:
            findings.extend(check())
        findings.sort(key=lambda f: (int(f.severity), f.check, f.path))
        return LintReport(findings=tuple(findings))

    def record(self, report: LintReport) -> None:
        """Append exactly one ``log.md`` entry for ``report`` (SPEC check 13).

        Delegates to :meth:`thoth.vault.Vault.append_log` with the ``lint`` action and a
        ``"<N> issues found"`` subject, so a single ``## [YYYY-MM-DD] lint | N issues
        found`` block is appended. ``files`` is empty, because the grouped findings live
        in the rendered report rather than the log. A clean report still logs ``0 issues
        found``.

        Args:
            report: The report whose ``total`` is logged.

        Raises:
            thoth.vault.VaultError: when ``log.md`` is missing.
        """
        self._vault.append_log("lint", f"{report.total} issues found", [])

    # ---- checks 1-12 (thin delegations to the pure check functions) ------------------

    def check_orphans(self) -> list[Finding]:
        """Flag curated reference pages with zero inbound wikilinks (check 1).

        Findings are ``Severity.ORPHAN``. An ``actions/`` page is exempt, and a
        self-link does not count as inbound.

        Returns:
            One ``Severity.ORPHAN`` :class:`~thoth.lint.Finding` per orphaned reference
            page.
        """
        return _check_orphans(self._curated_pages())

    def check_broken_links(self) -> list[Finding]:
        """Flag ``[text](path.md)`` links resolving to no page, honouring aliases (2).

        Findings are ``Severity.BROKEN``, the highest severity. A target resolves
        against a page's slug, its vault-relative path with or without ``.md``, or its
        ``aliases``.

        Returns:
            One :class:`~thoth.lint.Finding` per unresolved link occurrence.
        """
        return _check_broken_links(self._all_scanned_pages())

    def check_summaries(self) -> list[Finding]:
        """Flag content pages missing a one-line ``summary:`` gloss (check 3).

        Findings are ``Severity.STYLE``. Every :data:`~thoth.vault.SUMMARY_TYPES` page
        needs a non-empty one-line ``summary:`` (issue #72, ADR 0008).

        Returns:
            The summary-gloss findings.
        """
        return _check_summaries([*self._curated_pages(), *self._actionable_pages()])

    def check_frontmatter(self) -> list[Finding]:
        """Validate frontmatter on every curated and life-admin page (check 4).

        Findings are ``Severity.STYLE``, covering the required fields, the
        :data:`~thoth.lint.TYPE_REQUIRED_FIELDS` per type, a valid ``type`` and
        ``source``, a boolean ``personal``, and in-vocabulary ``status``, ``kind``,
        ``priority`` and ``media_type``.

        Returns:
            The frontmatter findings.
        """
        return _check_frontmatter(self._all_scanned_pages())

    def check_stale(self) -> list[Finding]:
        """Flag stale reference pages and overdue / cold actionable pages (check 5).

        Findings are ``Severity.STALE``, covering a reference page whose ``updated`` is
        past :data:`~thoth.lint.STALE_DAYS`, an open ``action`` past its ``due_date``,
        and a ``kind: media`` action still ``todo`` whose ``created`` is past
        :data:`~thoth.lint.MEDIA_STALE_DAYS`.

        Returns:
            The stale-content findings.
        """
        return _check_stale(
            self._curated_pages(), self._actionable_pages(), self._today
        )

    def check_contradictions(self) -> list[Finding]:
        """Flag pages marked ``contested`` or carrying ``contradictions`` (check 6).

        Findings are ``Severity.CONTESTED``, for a truthy ``contested`` or a non-empty
        ``contradictions:`` list.

        Returns:
            The contradiction findings.
        """
        return _check_contradictions(self._all_scanned_pages())

    def check_source_drift(self) -> list[Finding]:
        """Flag ``raw/`` pages whose body sha256 differs from frontmatter (check 7).

        Findings are ``Severity.DRIFT``. The body ``sha256`` is recomputed over the same
        body ``python-frontmatter`` splits, matching
        :meth:`thoth.vault.Vault.write_raw`, and a raw page with no ``sha256:`` is
        skipped rather than treated as an error.

        Returns:
            The source-drift findings.
        """
        return _check_source_drift(self._raw_pages())

    def check_quality_signals(self) -> list[Finding]:
        """Flag low-confidence and uncorroborated single-source pages (check 8).

        Findings are ``Severity.STYLE``, for ``confidence: low`` and for a single-entry
        ``sources:`` list with no ``confidence``.

        Returns:
            The quality-signal findings.
        """
        return _check_quality_signals(self._curated_pages())

    def check_page_size(self) -> list[Finding]:
        """Flag curated pages over :data:`~thoth.lint.PAGE_SIZE_LIMIT` body lines

        Findings are ``Severity.STYLE``. Only a curated knowledge page is sized, and
        exactly :data:`~thoth.lint.PAGE_SIZE_LIMIT` lines passes.

        Returns:
            The page-size findings.
        """
        return _check_page_size(self._curated_pages())

    def check_tag_audit(self) -> list[Finding]:
        """Flag pages using a tag absent from ``SCHEMA.md``'s taxonomy (check 10).

        Findings are ``Severity.STYLE``, for a ``tags:`` entry absent from the ``## Tag
        Taxonomy`` section :func:`~thoth.lint.parse_taxonomy_tags` parses out of
        ``SCHEMA.md``.

        Returns:
            The tag-audit findings.

        Raises:
            thoth.lint.LintError: when ``SCHEMA.md`` is missing, leaving the audit no
                source of truth.
        """
        try:
            schema_text = self._read_text("SCHEMA.md")
        except LintError as exc:
            raise LintError(
                "SCHEMA.md is missing; cannot audit tags against the taxonomy"
            ) from exc
        return _check_tag_audit(schema_text, self._all_scanned_pages())

    def check_image_hygiene(self) -> list[Finding]:
        """Flag orphan assets, broken embeds and surviving sidecars (check 11).

        Findings are ``Severity.BROKEN``, for an unembedded binary in ``raw/assets/``, a
        page embedding a missing asset, and any legacy ``raw/assets/*.md`` sidecar.

        Returns:
            The image-hygiene findings.
        """
        return _check_image_hygiene(
            self._all_scanned_pages(),
            assets=self._asset_names(
                lambda p: ASSET_SLUG_RE.fullmatch(p.name) is not None
            ),
            sidecars=self._asset_names(lambda p: p.suffix == ".md"),
        )

    def check_log_rotation(self) -> list[Finding]:
        """Flag a ``log.md`` with more than :data:`~thoth.lint.LOG_ROTATE_LIMIT`

        Findings are ``Severity.STYLE``. Entries are counted by the ``## [`` block
        markers, and exceeding the limit suggests rotating to ``log-YYYY.md``.

        Returns:
            The log-rotation findings.
        """
        try:
            log_text = self._read_text("log.md")
        except LintError:
            return []
        return _check_log_rotation(log_text)

    def check_link_style(self) -> list[Finding]:
        """Flag a legacy Obsidian wiki link or embed: OKF wants markdown (check 14).

        Every ``[[wikilink]]``, and every wiki *image* embed such as ``![[photo.png]]``,
        in a scanned content page is flagged ``Severity.STYLE``, because the vault
        adopts the OKF standard markdown ``[text](path.md)`` and ``![alt](path)`` forms
        (issue #189). A Bases (``.base``) or Excalidraw (``.excalidraw``) embed is
        exempt, having no markdown equivalent and staying in the Obsidian ``![[...]]``
        form. Spine files, ``raw/`` and ``_bases/`` are out of scan scope, so the
        dashboards are never flagged.

        Returns:
            One :class:`~thoth.lint.Finding` per legacy wiki link or non-exempt embed.
        """
        return _check_link_style(self._all_scanned_pages())

    # ---- internal page walks ---------------------------------------------------------

    def _curated_pages(self) -> list[_Page]:
        """Return parsed pages in :data:`CURATED_DIRS`, skipping spine files.

        These are the lifecycle-free reference folders, ``entities``, ``notes`` and
        ``memories``, and the orphan, index-completeness and stale checks scope to them.
        """
        return self._pages_in(CURATED_DIRS)

    def _actionable_pages(self) -> list[_Page]:
        """Return parsed pages in :data:`ACTIONABLE_DIRS`, skipping spine files.

        These are the lifecycle-bearing folders, ``actions/``, which also holds the
        media queue as actions with ``kind: media``, and the overdue and cold-media
        checks scope to them.
        """
        return self._pages_in(ACTIONABLE_DIRS)

    def _all_scanned_pages(self) -> list[_Page]:
        """Return the reference, actionable and inbox pages most checks scan.

        An ``inbox/`` holding page is machinery, exempt from the orphan and index checks
        that scope to :data:`CURATED_DIRS`, but it still carries the common frontmatter
        contract, so it is scanned here for the frontmatter and broken-link checks.
        """
        return [
            *self._curated_pages(),
            *self._actionable_pages(),
            *self._pages_in(("inbox",)),
        ]

    def _raw_pages(self) -> list[_Page]:
        """Return parsed pages in ``raw/{articles,papers,transcripts}``."""
        return self._pages_in(tuple(f"raw/{sub}" for sub in _RAW_DIRS))

    def _pages_in(self, folders: Iterable[str]) -> list[_Page]:
        """Parse every ``*.md`` in each folder, skipping spine and malformed pages.

        Each folder is confined through the vault, then walked recursively. A spine file
        from :data:`SPINE_FILES`, and anything under an :data:`EXCLUDED_DIRS` directory,
        is skipped. A page whose frontmatter cannot be parsed is skipped too, mirroring
        ``summary._iter_pages``, so a malformed page never wedges the whole run.

        Args:
            folders: Vault-relative folder names to walk.

        Returns:
            The parsed :class:`_Page` list, sorted by path.
        """
        root = self._vault.root
        pages: list[_Page] = []
        for folder in folders:
            base = root / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*.md"):
                if not path.is_file():
                    continue
                if path.name in SPINE_FILES:
                    continue
                rel = path.relative_to(root).as_posix()
                if _under_excluded_dir(rel):
                    continue
                try:
                    post = frontmatter.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
                    continue
                pages.append(
                    _Page(
                        path=rel,
                        slug=PurePosixPath(rel).stem,
                        meta=dict(post.metadata),
                        body=post.content,
                    )
                )
        pages.sort(key=lambda page: page.path)
        return pages

    def _asset_names(self, keep: Callable[[Path], bool]) -> set[str]:
        """Return the ``raw/assets/`` filenames whose path satisfies ``keep``."""
        base = self._vault.root / _ASSETS_DIR
        if not base.is_dir():
            return set()
        return {path.name for path in base.iterdir() if path.is_file() and keep(path)}

    def _read_text(self, vault_relative_path: str) -> str:
        """Confine and read a spine file's full text, or raise :class:`LintError`.

        Args:
            vault_relative_path: A vault-relative path (for example ``index.md``).

        Returns:
            The file's UTF-8 text.

        Raises:
            LintError: when the path escapes the vault, the file is missing, or it
                cannot be read or decoded.
        """
        try:
            absolute = self._vault.resolve(vault_relative_path)
        except Exception as exc:
            raise LintError(f"cannot resolve {vault_relative_path!r}: {exc}") from exc
        if not absolute.is_file():
            raise LintError(f"{vault_relative_path!r} does not exist")
        try:
            return absolute.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LintError(f"cannot read {vault_relative_path!r}: {exc}") from exc


def _under_excluded_dir(rel: str) -> bool:
    """Return ``True`` when any path segment of ``rel`` is an excluded directory."""
    return any(segment in EXCLUDED_DIRS for segment in PurePosixPath(rel).parts)
