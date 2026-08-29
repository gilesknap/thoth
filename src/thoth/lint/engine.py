"""The :class:`LintEngine`: the vault walk plus thin ``check_*`` delegations.

The engine owns the only disk surface of the scan, parsing pages out of the vault
folders and reading spine files, then hands them to the pure check functions in the
``checks_*`` modules. Check 13, :meth:`LintEngine.record`, appends the single ``log.md``
entry.
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

# Immutable raw source subdirs whose sha256 frontmatter is drift-checked (check 7)
_RAW_DIRS: tuple[str, ...] = ("articles", "papers", "transcripts")


class LintEngine:
    """Pure, deterministic vault linter built from a frozen config and vault.

    All retrieval is a pure read over the vault folders, with no LLM and no network. The
    one non-deterministic input, the current calendar date, is injected as ``today``, so
    the stale, overdue and media-cold windows are reproducible under a frozen clock.
    """

    def __init__(
        self, config: Config, vault: Vault, *, today: date | None = None
    ) -> None:
        """Stores collaborators and resolves the injected clock to a London date.

        Args:
            config: Frozen runtime config, carried for symmetry with the summary
                engine. Lint reads no new field from it.
            vault: The path-confined vault facade, the only disk surface used.
            today: Date used for every stale and overdue window. None uses the
                current Europe/London date.
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
        """Runs every check and aggregates them into a sorted report.

        Findings are concatenated and sorted by severity, check and path, so the report
        is deterministic. Check 13 is not run here, because the caller decides whether
        to log.

        Returns:
            The aggregated report.

        Raises:
            thoth.lint.LintError: if a check cannot run, such as a missing vault root
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
        """Appends exactly one ``log.md`` entry for a report (SPEC check 13).

        One dated ``lint`` block is appended carrying the issue count. The file list is
        empty, because the grouped findings live in the rendered report rather than the
        log. A clean report still logs zero issues.

        Args:
            report: The report whose total is logged.

        Raises:
            thoth.vault.VaultError: if ``log.md`` is missing.
        """
        self._vault.append_log("lint", f"{report.total} issues found", [])

    # ---- checks 1-12 (thin delegations to the pure check functions) ------------------

    def check_orphans(self) -> list[Finding]:
        """Flags curated reference pages with zero inbound links (check 1).

        Actionable pages are exempt, because the Bases dashboards surface them. A page
        is reachable when some other page links to its slug or an alias, so a page
        linking only to itself does not count.

        Returns:
            One finding per orphaned reference page.
        """
        return _check_orphans(self._curated_pages())

    def check_broken_links(self) -> list[Finding]:
        """Flags links resolving to no page, honouring aliases (check 2).

        Recognises the OKF standard markdown form and any residual wikilink. A target
        resolves when its bare stem matches a page's slug, its full vault path with or
        without the suffix, or an alias. Alias and anchor portions are stripped first.

        Returns:
            One finding per unresolved link occurrence.
        """
        return _check_broken_links(self._all_scanned_pages())

    def check_summaries(self) -> list[Finding]:
        """Flags content pages missing a one-line ``summary:`` gloss (check 3).

        Every content type must carry a non-empty summary, including ``action`` since
        ADR 0013. It is the canonical, rebuildable gloss that replaced the old
        agent-maintained ``index.md`` catalog (issue #72, ADR 0008) and feeds the
        Summary column on the dashboards. ``index.md`` is now a static set of dashboards
        and is not scanned.

        Returns:
            The summary-gloss findings.
        """
        return _check_summaries([*self._curated_pages(), *self._actionable_pages()])

    def check_frontmatter(self) -> list[Finding]:
        """Validates frontmatter on every scanned page (check 4).

        Checks the required fields, which differ between content pages and inbox holds,
        that ``type`` and ``source`` are in the vault vocabularies, that the
        type-specific required fields are present, that ``personal`` is a real boolean,
        and that the remaining enumerated values are in vocabulary.

        Returns:
            The frontmatter findings.
        """
        return _check_frontmatter(self._all_scanned_pages())

    def check_stale(self) -> list[Finding]:
        """Flags stale reference pages and overdue or cold actionable pages (5).

        A reference page not updated within the stale window is flagged. So is an open
        action past its due date, where done and cancelled are exempt, and a media
        action still in the backlog past the media window.

        Returns:
            The stale-content findings.
        """
        return _check_stale(
            self._curated_pages(), self._actionable_pages(), self._today
        )

    def check_contradictions(self) -> list[Finding]:
        """Flags pages marked ``contested`` or carrying ``contradictions`` (6).

        Returns:
            The contradiction findings.
        """
        return _check_contradictions(self._all_scanned_pages())

    def check_source_drift(self) -> list[Finding]:
        """Flags ``raw/`` pages whose body digest differs from frontmatter (7).

        The digest is recomputed over the same body ``python-frontmatter`` splits, which
        matches what :meth:`thoth.vault.Vault.write_raw` stamped. A raw page carrying no
        digest is skipped rather than treated as an error.

        Returns:
            The source-drift findings.
        """
        return _check_source_drift(self._raw_pages())

    def check_quality_signals(self) -> list[Finding]:
        """Flags low-confidence and uncorroborated single-source pages (8).

        Every page with low confidence is listed, as is every page with a single-entry
        sources list and no confidence field, which should be corroborated or demoted.

        Returns:
            The quality-signal findings.
        """
        return _check_quality_signals(self._curated_pages())

    def check_page_size(self) -> list[Finding]:
        """Flags curated pages over the body-line limit (check 9).

        Only curated knowledge pages are sized, so actionable pages are exempt. A body
        of exactly the limit passes and one line more is flagged.

        Returns:
            The page-size findings.
        """
        return _check_page_size(self._curated_pages())

    def check_tag_audit(self) -> list[Finding]:
        """Flags pages using a tag absent from the ``SCHEMA.md`` taxonomy (10).

        The taxonomy is parsed from the ``## Tag Taxonomy`` section, and any tag outside
        that set is flagged.

        Returns:
            The tag-audit findings.

        Raises:
            thoth.lint.LintError: if ``SCHEMA.md`` is missing, leaving the audit with
                no source of truth.
        """
        try:
            schema_text = self._read_text("SCHEMA.md")
        except LintError as exc:
            raise LintError(
                "SCHEMA.md is missing; cannot audit tags against the taxonomy"
            ) from exc
        return _check_tag_audit(schema_text, self._all_scanned_pages())

    def check_image_hygiene(self) -> list[Finding]:
        """Flags orphan assets, broken embeds and surviving sidecars (check 11).

        Three sub-checks. A binary embedded by no page is an orphan, a page embedding a
        missing asset is a broken embed, and a legacy per-image sidecar is flagged for
        merge into its owning page.

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
        """Flags a ``log.md`` carrying more than the rotation limit (check 12).

        Entries are counted by their block markers. At or below the limit passes, and
        above it suggests rotating to ``log-YYYY.md``. A missing log yields no finding,
        since there is nothing to rotate.

        Returns:
            The log-rotation findings.
        """
        try:
            log_text = self._read_text("log.md")
        except LintError:
            return []
        return _check_log_rotation(log_text)

    def check_link_style(self) -> list[Finding]:
        """Flags legacy wiki links and embeds, since OKF wants markdown (14).

        Every wikilink and wiki image embed in a scanned page is flagged, because the
        vault adopts OKF standard markdown links (issue #189). Bases and Excalidraw
        embeds are exempt, because they have no markdown equivalent and must stay in
        Obsidian form. Spine files, ``raw/`` and ``_bases/`` are out of scan scope, so
        the dashboards are never flagged.

        Returns:
            One finding per legacy wiki link or non-exempt embed.
        """
        return _check_link_style(self._all_scanned_pages())

    # ---- internal page walks ---------------------------------------------------------

    def _curated_pages(self) -> list[_Page]:
        """Returns parsed pages in the curated folders, skipping spine files.

        These are the lifecycle-free reference folders, and the orphan, completeness and
        stale checks scope to them.
        """
        return self._pages_in(CURATED_DIRS)

    def _actionable_pages(self) -> list[_Page]:
        """Returns parsed pages in the actionable folders, skipping spine files.

        These are the lifecycle-bearing folders, which also hold the media queue, and
        the overdue and cold-media checks scope to them.
        """
        return self._pages_in(ACTIONABLE_DIRS)

    def _all_scanned_pages(self) -> list[_Page]:
        """Returns reference, actionable and inbox pages, the set most checks scan.

        Inbox holds are machinery and are exempt from the orphan and completeness
        checks, but they still carry the common frontmatter contract, so they are
        scanned here for the frontmatter and broken-link checks.
        """
        return [
            *self._curated_pages(),
            *self._actionable_pages(),
            *self._pages_in(("inbox",)),
        ]

    def _raw_pages(self) -> list[_Page]:
        """Returns parsed pages in the immutable raw source subdirs."""
        return self._pages_in(tuple(f"raw/{sub}" for sub in _RAW_DIRS))

    def _pages_in(self, folders: Iterable[str]) -> list[_Page]:
        """Parses every ``*.md`` in each folder, skipping spine and malformed pages.

        Each folder is walked recursively. Spine files and anything under an excluded
        directory are skipped, and a page whose frontmatter will not parse is skipped
        too, so one malformed page never wedges the whole run.

        Args:
            folders: Vault-relative folder names to walk.

        Returns:
            The parsed pages, sorted by path.
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
        """Returns the ``raw/assets/`` filenames whose path satisfies a predicate."""
        base = self._vault.root / _ASSETS_DIR
        if not base.is_dir():
            return set()
        return {path.name for path in base.iterdir() if path.is_file() and keep(path)}

    def _read_text(self, vault_relative_path: str) -> str:
        """Confines and reads a spine file's full text.

        Args:
            vault_relative_path: A vault-relative path such as ``index.md``.

        Returns:
            The file's UTF-8 text.

        Raises:
            LintError: if the path escapes the vault, the file is missing, or it
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
    """Reports whether any path segment names an excluded directory."""
    return any(segment in EXCLUDED_DIRS for segment in PurePosixPath(rel).parts)
