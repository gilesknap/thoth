"""The 13 SPEC section 11 / Appendix maintenance checks as a pure vault scan.

This module is the appliance's deterministic maintenance pass (SPEC section 11 and the
Appendix "Lint checks" table). It is a *pure programmatic markdown scan* over a real
:class:`thoth.vault.Vault`: no network, no LLM, no subprocess. Each of checks 1-12 is a
method returning ``list[Finding]``; :meth:`LintEngine.run` aggregates them into a
:class:`LintReport` grouped and counted by :class:`Severity`; check 13
(:meth:`LintEngine.record`) appends **exactly one** ``log.md`` entry via
:meth:`thoth.vault.Vault.append_log` carrying the issue count.

The 13 checks (SPEC Appendix table):

1.  **Orphan pages** -- curated knowledge pages with zero inbound ``[[wikilinks]]``
    (life-admin pages are exempt; Bases surface them).
2.  **Broken wikilinks** -- ``[[target]]`` references that resolve to no page, honouring
    ``aliases`` frontmatter. Highest severity.
3.  **Summary gloss** -- every reference page (``entity``/``note``/``memory``) carries a
    non-empty one-line ``summary:`` frontmatter gloss (issue #72 / ADR 0008): the
    canonical, rebuildable home of the per-page gloss that replaced the old
    agent-maintained ``index.md`` catalog.
4.  **Frontmatter validation** -- required common fields present, ``type`` valid,
    type-specific required fields present, and ``status`` / ``priority`` /
    ``media_type`` values within the Metadata-Menu vocabularies.
5.  **Stale content** -- a knowledge page whose ``updated`` is older than
    :data:`STALE_DAYS`; an ``action`` past its ``due_date`` and not done/cancelled; a
    ``media`` ``to_consume`` older than :data:`MEDIA_STALE_DAYS`.
6.  **Contradictions** -- every page with ``contested: true`` or a non-empty
    ``contradictions:`` list.
7.  **Source drift** -- a ``raw/`` page whose recomputed body sha256 differs from its
    stored ``sha256`` frontmatter.
8.  **Quality signals** -- ``confidence: low`` pages and single-source pages with no
    ``confidence``.
9.  **Page size** -- curated pages whose body exceeds :data:`PAGE_SIZE_LIMIT` lines.
10. **Tag audit** -- every tag in use must appear in ``SCHEMA.md``'s
    ``## Tag Taxonomy`` section.
11. **Image hygiene** -- orphan binaries in ``raw/assets/`` with no embed anywhere,
    pages embedding a missing asset, and surviving per-image sidecar ``.md`` files.
12. **Log rotation** -- a ``log.md`` with more than :data:`LOG_ROTATE_LIMIT` entries.
13. **Report + log** -- group by severity and append one ``log.md`` line.

All folder / type / slug contract constants are imported from :mod:`thoth.vault` so the
closed-surface contract stays single-sourced; the Metadata-Menu vocabularies (which the
vault writer does not enforce) are defined here from the SPEC frontmatter table. The
only injected non-determinism is ``today`` (a :class:`~datetime.date`) so the
stale / overdue / media-cold windows are reproducible under a frozen clock.

Only the standard library plus ``frontmatter`` / ``yaml`` and the frozen
:class:`thoth.config.Config` / :class:`thoth.vault.Vault` are imported at module level,
so importing this module at pytest collection is always CI-safe.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import frontmatter
import yaml

from thoth.config import Config
from thoth.vault import (
    ACTIONABLE_DIRS,
    ASSET_SLUG_RE,
    CURATED_DIRS,
    FOLDER_TYPE_CONTRACT,
    REQUIRED_COMMON_FIELDS,
    SUMMARY_TYPES,
    VALID_SOURCES,
    VALID_TYPES,
    Vault,
)

__all__ = [
    "LONDON",
    "CURATED_DIRS",
    "ACTIONABLE_DIRS",
    "SPINE_FILES",
    "EXCLUDED_DIRS",
    "PAGE_SIZE_LIMIT",
    "LOG_ROTATE_LIMIT",
    "STALE_DAYS",
    "MEDIA_STALE_DAYS",
    "TYPE_REQUIRED_FIELDS",
    "STATUS_VOCAB",
    "PRIORITY_VOCAB",
    "MEDIA_TYPE_VOCAB",
    "Severity",
    "Finding",
    "LintReport",
    "LintError",
    "LintEngine",
    "parse_taxonomy_tags",
    "extract_wikilinks",
    "extract_embeds",
]

LONDON: ZoneInfo = ZoneInfo("Europe/London")
"""The Europe/London timezone used to derive a default ``today`` (SPEC section 9).

Resolved via :class:`zoneinfo.ZoneInfo`; the ``tzdata`` package is a base dependency so
this resolves identically across the 3.11-3.14 matrix even on a minimal container.
"""

# CURATED_DIRS / ACTIONABLE_DIRS are the canonical folder vocabulary owned by
# thoth.vault (ADR 0005); they are imported above and re-exported here so the __all__
# surface and lint consumers derive the same list instead of restating it.
# "Curated page" means a lifecycle-free reference page in one of the CURATED_DIRS
# folders (entities/notes/memories): the orphan, index-completeness, page-size and
# quality-signal checks scope to these. The ACTIONABLE_DIRS pages (actions/, which also
# holds the media queue as actions tagged 'media') are exempt from the orphan /
# index-completeness checks (Bases dashboards surface them) but still carry the common
# frontmatter contract and get the overdue / cold-media checks instead.

SPINE_FILES: frozenset[str] = frozenset({"index.md", "SCHEMA.md", "log.md"})
"""Structural backbone files (matches ``reindex.SKIP_FILES``); not curated knowledge."""

EXCLUDED_DIRS: frozenset[str] = frozenset({"_bases", "_meta", "_archive", ".obsidian"})
"""Structural directories excluded from the orphan / index / size scans (SPEC 5)."""

PAGE_SIZE_LIMIT: int = 200
"""Body line count above which a knowledge page is a split candidate (SPEC check 9)."""

LOG_ROTATE_LIMIT: int = 500
"""``## [`` entry count above which ``log.md`` should rotate (SPEC check 12)."""

STALE_DAYS: int = 90
"""A knowledge page is stale when ``updated`` is older than this many days (check 5)."""

MEDIA_STALE_DAYS: int = 180
"""A ``to_consume`` media item is cold this many days after ``created`` (check 5)."""

# Immutable raw source subdirs whose sha256 frontmatter is drift-checked (check 7).
_RAW_DIRS: tuple[str, ...] = ("articles", "papers", "transcripts")

# The raw subdirectory holding binary assets (image-hygiene check 11).
_ASSETS_DIR: str = "raw/assets"

# Action statuses that exempt an overdue action from the stale check (SPEC check 5).
_ACTION_CLOSED_STATUSES: frozenset[str] = frozenset({"done", "completed", "cancelled"})

# The media status whose backlog ages out (SPEC check 5 / frontmatter contract).
_MEDIA_OPEN_STATUS: str = "to_consume"

# The SCHEMA.md heading under which the tag taxonomy bullets live (SPEC Appendix).
_TAXONOMY_HEADING: str = "## Tag Taxonomy"

TYPE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "action": ("status",),
}
"""Type-specific required frontmatter fields beyond the common set (SPEC check 4).

ADR 0005 folded the media queue into ``actions/`` (a media item is an ``action`` tagged
``media``), so the single ``action`` type carries the ``status`` requirement.
"""

STATUS_VOCAB: dict[str, frozenset[str]] = {
    "action": frozenset(
        {
            "todo",
            "in_progress",
            "done",
            "completed",
            "cancelled",
            "to_consume",
            "consuming",
            "consumed",
        }
    ),
}
"""Allowed ``status`` values per ``type`` (Metadata-Menu vocab, SPEC table).

ADR 0005: ``action`` covers both the todo lifecycle (``todo``..``cancelled``) and the
media consume queue (``to_consume``/``consuming``/``consumed``), since a media item is
now an ``action`` tagged ``media``.
"""

PRIORITY_VOCAB: frozenset[str] = frozenset(
    {"1 - Urgent", "2 - High", "3 - Medium", "4 - Low"}
)
"""Allowed ``priority`` values (Metadata-Menu vocab, SPEC frontmatter table)."""

MEDIA_TYPE_VOCAB: frozenset[str] = frozenset(
    {"book", "film", "tv", "podcast", "article", "video", "music"}
)
"""Allowed ``media_type`` values (Metadata-Menu vocab, SPEC frontmatter table)."""

# A wikilink token: [[target]], [[target|alias]], or [[target#heading]]. The capture
# group is the raw inner text; the helper strips the alias / anchor to the bare target.
_WIKILINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[\[([^\[\]]+?)\]\]")

# An embed token: ![[asset.ext]] (the leading '!' distinguishes it from a wikilink).
_EMBED_RE: re.Pattern[str] = re.compile(r"\!\[\[([^\[\]]+?)\]\]")

# Fenced code spans (``` ... ``` or ~~~ ... ~~~) and inline code (`...`); their contents
# must not produce false-positive wikilinks/embeds (SPEC: code-fenced false positives).
_FENCE_RE: re.Pattern[str] = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

# A log entry header line "## [YYYY-MM-DD] ...".
_LOG_ENTRY_RE: re.Pattern[str] = re.compile(r"^## \[", re.MULTILINE)


class Severity(IntEnum):
    """Lint finding severity; lower value sorts first in the grouped report.

    Order (SPEC check 13): broken links/embeds > orphans > source drift > contested >
    stale/overdue > style.
    """

    BROKEN = 0
    ORPHAN = 1
    DRIFT = 2
    CONTESTED = 3
    STALE = 4
    STYLE = 5


@dataclass(frozen=True, slots=True)
class Finding:
    """One lint issue: its check number/name, severity, the page, and a message."""

    check: int
    """The 1-based SPEC check number that produced this finding."""
    name: str
    """A short check name (e.g. ``broken-wikilinks``)."""
    severity: Severity
    """The :class:`Severity` used to group and sort the finding."""
    path: str
    """The vault-relative path of the page the finding concerns (``""`` if none)."""
    message: str
    """A human-readable description of the issue."""


@dataclass(frozen=True, slots=True)
class LintReport:
    """All findings from one lint pass, grouped and counted by severity."""

    findings: tuple[Finding, ...]
    """Every finding, pre-sorted by ``(severity, check, path)``."""

    @property
    def total(self) -> int:
        """The total number of findings in the report."""
        return len(self.findings)

    @property
    def is_clean(self) -> bool:
        """``True`` when the pass found no issues."""
        return not self.findings

    def by_severity(self) -> list[tuple[Severity, list[Finding]]]:
        """Group findings by severity, ascending (most severe first).

        Returns:
            A list of ``(severity, findings)`` pairs in :class:`Severity` order; only
            severities that actually occur are included.
        """
        groups: dict[Severity, list[Finding]] = {}
        for finding in self.findings:
            groups.setdefault(finding.severity, []).append(finding)
        return [(sev, groups[sev]) for sev in sorted(groups)]

    def render(self) -> str:
        """Render the report grouped by severity as plain text.

        Each group is a header line ``<SEVERITY> (<count>)`` followed by one indented
        ``check N <name>: <path> -- <message>`` line per finding (most severe group
        first). A clean report renders a single ``lint: clean`` line.

        Returns:
            The grouped plain-text report.
        """
        if self.is_clean:
            return "lint: clean - 0 issues found"
        lines: list[str] = [f"lint: {self.total} issue(s) found"]
        for severity, group in self.by_severity():
            lines.append(f"\n{severity.name} ({len(group)})")
            for finding in group:
                where = finding.path or "-"
                lines.append(
                    f"  check {finding.check} {finding.name}: "
                    f"{where} -- {finding.message}"
                )
        return "\n".join(lines)


class LintError(Exception):
    """Raised when the scan cannot run (e.g. a missing vault root or SCHEMA.md)."""


class LintEngine:
    """Pure, deterministic 13-check vault linter built from a frozen Config + Vault.

    All retrieval is a pure read over the vault folders; no LLM and no network are used.
    The single non-deterministic input -- the current calendar date -- is injected as
    ``today`` so the stale / overdue / media-cold windows are reproducible under a
    frozen clock in tests.
    """

    def __init__(
        self, config: Config, vault: Vault, *, today: date | None = None
    ) -> None:
        """Store collaborators and resolve the injected clock to a London date.

        Args:
            config: The frozen runtime config (carried for symmetry with
                :class:`~thoth.summary.SummaryEngine`; lint reads no new field).
            vault: The path-confined vault facade (the only disk surface used).
            today: The calendar date used for every stale / overdue window; when
                ``None``, the current Europe/London date is used.
        """
        self._config = config
        self._vault = vault
        self._today = today if today is not None else datetime.now(LONDON).date()

    @property
    def today(self) -> date:
        """The calendar date used for the stale / overdue / media-cold windows."""
        return self._today

    # ---- aggregate -------------------------------------------------------------------

    def run(self) -> LintReport:
        """Run checks 1-12 and aggregate into a sorted :class:`LintReport`.

        Findings are concatenated across the twelve checks and sorted by
        ``(severity, check, path)`` so the report is deterministic. Check 13
        (:meth:`record`) is *not* run here -- the caller decides whether to log.

        Returns:
            The aggregated :class:`LintReport`.

        Raises:
            LintError: if a check cannot run (for example a missing vault root or a
                missing ``SCHEMA.md`` for the tag audit).
        """
        findings: list[Finding] = []
        findings.extend(self.check_orphans())
        findings.extend(self.check_broken_wikilinks())
        findings.extend(self.check_summaries())
        findings.extend(self.check_frontmatter())
        findings.extend(self.check_stale())
        findings.extend(self.check_contradictions())
        findings.extend(self.check_source_drift())
        findings.extend(self.check_quality_signals())
        findings.extend(self.check_page_size())
        findings.extend(self.check_tag_audit())
        findings.extend(self.check_image_hygiene())
        findings.extend(self.check_log_rotation())
        findings.sort(key=lambda f: (int(f.severity), f.check, f.path))
        return LintReport(findings=tuple(findings))

    def record(self, report: LintReport) -> None:
        """Append exactly one ``log.md`` entry for ``report`` (SPEC check 13).

        Delegates to :meth:`thoth.vault.Vault.append_log` with the ``lint`` action and a
        ``"<N> issues found"`` subject, so a single ``## [YYYY-MM-DD] lint | N issues
        found`` block is appended (``files`` is empty -- the grouped findings are in the
        rendered report, not the log). A clean report still logs ``0 issues found``.

        Args:
            report: The report whose ``total`` is logged.

        Raises:
            thoth.vault.VaultError: if ``log.md`` is missing.
        """
        self._vault.append_log("lint", f"{report.total} issues found", [])

    # ---- check 1: orphan pages -------------------------------------------------------

    def check_orphans(self) -> list[Finding]:
        """Flag curated reference pages with zero inbound wikilinks (check 1).

        Actionable (``actions/``) pages are exempt (Bases dashboards surface them). A
        page is reachable if any *other* page links to its slug or to one of its
        ``aliases``; a page linking only to itself does not count as inbound.

        Returns:
            One :class:`Finding` (``Severity.ORPHAN``) per orphaned reference page.
        """
        pages = self._curated_pages()
        inbound = self._inbound_targets(pages)
        findings: list[Finding] = []
        for page in pages:
            handles = {page.slug, *page.aliases}
            if handles & inbound:
                continue
            findings.append(
                Finding(
                    check=1,
                    name="orphan",
                    severity=Severity.ORPHAN,
                    path=page.path,
                    message="reference page has no inbound [[wikilinks]]",
                )
            )
        return findings

    # ---- check 2: broken wikilinks ---------------------------------------------------

    def check_broken_wikilinks(self) -> list[Finding]:
        """Flag ``[[target]]`` links resolving to no page, honouring aliases (check 2).

        A target resolves if it matches any page's slug, its full vault-relative path
        (with or without the ``.md`` suffix), or one of its ``aliases``. The alias /
        anchor portions of a link are stripped before resolution. Highest severity
        (:class:`Severity.BROKEN`).

        Returns:
            One :class:`Finding` per unresolved wikilink occurrence.
        """
        pages = self._all_scanned_pages()
        resolvable = self._resolvable_targets(pages)
        findings: list[Finding] = []
        for page in pages:
            for target in extract_wikilinks(page.body):
                if self._normalise_target(target) in resolvable:
                    continue
                findings.append(
                    Finding(
                        check=2,
                        name="broken-wikilink",
                        severity=Severity.BROKEN,
                        path=page.path,
                        message=f"wikilink [[{target}]] resolves to no page",
                    )
                )
        return findings

    # ---- check 3: summary gloss ------------------------------------------------------

    def check_summaries(self) -> list[Finding]:
        """Flag reference pages missing a one-line ``summary:`` gloss (check 3).

        Every reference page (:data:`~thoth.vault.SUMMARY_TYPES`:
        ``entity``/``note``/``memory``) must carry a non-empty one-line ``summary:``
        frontmatter field -- the canonical, rebuildable per-page gloss that replaced the
        old agent-maintained ``index.md`` catalog (issue #72 / ADR 0008). A page whose
        ``summary`` is absent or blank is flagged ``Severity.STYLE`` (the tier the old
        catalog-completeness check used), preserving the "every reference page is
        glossed" guarantee on the page instead of the index. ``index.md`` is now a
        static set of Bases dashboards and is not scanned.

        Returns:
            The summary-gloss findings.
        """
        findings: list[Finding] = []
        for page in self._curated_pages():
            page_type = _str_field(page.meta.get("type"))
            if page_type not in SUMMARY_TYPES:
                continue
            if _str_field(page.meta.get("summary")) is None:
                findings.append(
                    Finding(
                        check=3,
                        name="summary-gloss",
                        severity=Severity.STYLE,
                        path=page.path,
                        message="reference page has no one-line summary: frontmatter",
                    )
                )
        return findings

    # ---- check 4: frontmatter validation ---------------------------------------------

    def check_frontmatter(self) -> list[Finding]:
        """Validate frontmatter on every curated and life-admin page (check 4).

        Checks the required common fields, that ``type`` and ``source`` are in the vault
        vocabularies, that type-specific required fields (:data:`TYPE_REQUIRED_FIELDS`)
        are present, and that ``status`` / ``priority`` / ``media_type`` values are in
        the Metadata-Menu vocabularies. All findings are ``Severity.STYLE``.

        Returns:
            The frontmatter findings.
        """
        findings: list[Finding] = []
        for page in self._all_scanned_pages():
            findings.extend(self._frontmatter_findings(page))
        return findings

    def _frontmatter_findings(self, page: _Page) -> list[Finding]:
        """Return the frontmatter findings for one scanned page."""
        meta = page.meta
        out: list[Finding] = []

        def flag(message: str) -> None:
            out.append(
                Finding(
                    check=4,
                    name="frontmatter",
                    severity=Severity.STYLE,
                    path=page.path,
                    message=message,
                )
            )

        for field in REQUIRED_COMMON_FIELDS:
            if meta.get(field) in (None, "", []):
                flag(f"missing required common field {field!r}")
        page_type = _str_field(meta.get("type"))
        if page_type is not None and page_type not in VALID_TYPES:
            flag(f"invalid type {page_type!r}")
        top_folder = PurePosixPath(page.path).parts[0]
        allowed_types = FOLDER_TYPE_CONTRACT.get(top_folder)
        if (
            page_type is not None
            and allowed_types is not None
            and page_type not in allowed_types
        ):
            flag(f"type {page_type!r} is not allowed in folder {top_folder!r}")
        source = _str_field(meta.get("source"))
        if source is not None and source not in VALID_SOURCES:
            flag(f"invalid source {source!r}")
        for field in TYPE_REQUIRED_FIELDS.get(page_type or "", ()):
            if meta.get(field) in (None, "", []):
                flag(f"{page_type} page is missing required field {field!r}")
        status = _str_field(meta.get("status"))
        allowed_status = STATUS_VOCAB.get(page_type or "")
        if (
            status is not None
            and allowed_status is not None
            and (status not in allowed_status)
        ):
            flag(f"status {status!r} is not in the {page_type} vocabulary")
        priority = _str_field(meta.get("priority"))
        if priority is not None and priority not in PRIORITY_VOCAB:
            flag(f"priority {priority!r} is not in the Metadata-Menu vocabulary")
        media_type = _str_field(meta.get("media_type"))
        if media_type is not None and media_type not in MEDIA_TYPE_VOCAB:
            flag(f"media_type {media_type!r} is not in the Metadata-Menu vocabulary")
        return out

    # ---- check 5: stale content ------------------------------------------------------

    def check_stale(self) -> list[Finding]:
        """Flag stale reference pages and overdue / cold actionable pages (check 5).

        A curated reference page whose ``updated`` is more than :data:`STALE_DAYS`
        before :attr:`today` is flagged; an open ``action`` past its ``due_date`` is
        flagged (done/completed/cancelled exempt); an ``action`` tagged ``media`` whose
        ``status`` is ``to_consume`` and whose ``created`` is more than
        :data:`MEDIA_STALE_DAYS` ago is flagged. All findings are ``Severity.STALE``.

        Returns:
            The stale-content findings.
        """
        findings: list[Finding] = []
        stale_floor = self._today - _dt.timedelta(days=STALE_DAYS)
        media_floor = self._today - _dt.timedelta(days=MEDIA_STALE_DAYS)
        for page in self._curated_pages():
            updated = _parse_date(page.meta.get("updated") or page.meta.get("created"))
            if updated is not None and updated < stale_floor:
                findings.append(
                    Finding(
                        check=5,
                        name="stale",
                        severity=Severity.STALE,
                        path=page.path,
                        message=(
                            f"reference page updated {updated.isoformat()} is older "
                            f"than {STALE_DAYS} days"
                        ),
                    )
                )
        for page in self._actionable_pages():
            findings.extend(self._stale_actionable(page, media_floor))
        return findings

    def _stale_actionable(self, page: _Page, media_floor: date) -> list[Finding]:
        """Return overdue-action / cold-media findings for one actionable page.

        ADR 0005: the media queue lives in ``actions/`` as an ``action`` tagged
        ``media``, so the cold-media check keys off the ``media`` tag plus the
        ``to_consume`` status rather than a separate ``media`` type / folder.
        """
        out: list[Finding] = []
        page_type = _str_field(page.meta.get("type"))
        status = _str_field(page.meta.get("status"))
        if page_type != "action":
            return out
        if status not in _ACTION_CLOSED_STATUSES:
            due = _parse_date(page.meta.get("due_date"))
            if due is not None and due < self._today:
                out.append(
                    Finding(
                        check=5,
                        name="overdue",
                        severity=Severity.STALE,
                        path=page.path,
                        message=f"action is past its due date {due.isoformat()}",
                    )
                )
        if status == _MEDIA_OPEN_STATUS and "media" in _page_tags(page.meta):
            added = _parse_date(page.meta.get("created"))
            if added is not None and added < media_floor:
                out.append(
                    Finding(
                        check=5,
                        name="media-cold",
                        severity=Severity.STALE,
                        path=page.path,
                        message=(
                            f"media to_consume since {added.isoformat()} is older "
                            f"than {MEDIA_STALE_DAYS} days"
                        ),
                    )
                )
        return out

    # ---- check 6: contradictions -----------------------------------------------------

    def check_contradictions(self) -> list[Finding]:
        """Flag pages marked ``contested`` or carrying ``contradictions`` (check 6).

        A page whose frontmatter has a truthy ``contested`` value, or a non-empty
        ``contradictions:`` list, is surfaced (``Severity.CONTESTED``).

        Returns:
            The contradiction findings.
        """
        findings: list[Finding] = []
        for page in self._all_scanned_pages():
            if _is_truthy(page.meta.get("contested")):
                findings.append(
                    Finding(
                        check=6,
                        name="contested",
                        severity=Severity.CONTESTED,
                        path=page.path,
                        message="page is marked contested: true",
                    )
                )
            contradictions = page.meta.get("contradictions")
            if isinstance(contradictions, list) and contradictions:
                joined = ", ".join(str(item) for item in contradictions)
                findings.append(
                    Finding(
                        check=6,
                        name="contradictions",
                        severity=Severity.CONTESTED,
                        path=page.path,
                        message=f"page declares contradictions: {joined}",
                    )
                )
        return findings

    # ---- check 7: source drift -------------------------------------------------------

    def check_source_drift(self) -> list[Finding]:
        """Flag ``raw/`` pages whose body sha256 differs from frontmatter (check 7).

        For each ``raw/{articles,papers,transcripts}/*.md`` page with a ``sha256:``
        frontmatter field, the body sha256 is recomputed (over the same body
        ``python-frontmatter`` splits, matching :meth:`thoth.vault.Vault.write_raw`); a
        mismatch is flagged ``Severity.DRIFT``. A raw page with no ``sha256`` is skipped
        (not an error).

        Returns:
            The source-drift findings.
        """
        findings: list[Finding] = []
        for page in self._raw_pages():
            stored = _str_field(page.meta.get("sha256"))
            if stored is None:
                continue
            recomputed = Vault.body_sha256(page.body)
            if recomputed != stored:
                findings.append(
                    Finding(
                        check=7,
                        name="source-drift",
                        severity=Severity.DRIFT,
                        path=page.path,
                        message=(
                            "raw body sha256 has drifted from its frontmatter "
                            "(raw edited or source changed)"
                        ),
                    )
                )
        return findings

    # ---- check 8: quality signals ----------------------------------------------------

    def check_quality_signals(self) -> list[Finding]:
        """Flag low-confidence and uncorroborated single-source pages (check 8).

        Every curated page with ``confidence: low`` is listed; so is every page with a
        single-entry ``sources:`` list and no ``confidence`` field (corroborate or
        demote). All findings are ``Severity.STYLE``.

        Returns:
            The quality-signal findings.
        """
        findings: list[Finding] = []
        for page in self._curated_pages():
            confidence = _str_field(page.meta.get("confidence"))
            if confidence == "low":
                findings.append(
                    Finding(
                        check=8,
                        name="quality",
                        severity=Severity.STYLE,
                        path=page.path,
                        message="page has confidence: low",
                    )
                )
                continue
            sources = page.meta.get("sources")
            if isinstance(sources, list) and len(sources) == 1 and confidence is None:
                findings.append(
                    Finding(
                        check=8,
                        name="quality",
                        severity=Severity.STYLE,
                        path=page.path,
                        message="single-source page has no confidence field",
                    )
                )
        return findings

    # ---- check 9: page size ----------------------------------------------------------

    def check_page_size(self) -> list[Finding]:
        """Flag curated pages over :data:`PAGE_SIZE_LIMIT` body lines (check 9).

        Only curated knowledge pages are sized (life-admin pages are exempt). A body of
        exactly :data:`PAGE_SIZE_LIMIT` lines passes; one line more is flagged
        ``Severity.STYLE``.

        Returns:
            The page-size findings.
        """
        findings: list[Finding] = []
        for page in self._curated_pages():
            line_count = len(page.body.splitlines())
            if line_count > PAGE_SIZE_LIMIT:
                findings.append(
                    Finding(
                        check=9,
                        name="page-size",
                        severity=Severity.STYLE,
                        path=page.path,
                        message=(
                            f"body is {line_count} lines (> {PAGE_SIZE_LIMIT}); "
                            "split into sub-topics"
                        ),
                    )
                )
        return findings

    # ---- check 10: tag audit ---------------------------------------------------------

    def check_tag_audit(self) -> list[Finding]:
        """Flag pages using a tag absent from ``SCHEMA.md``'s taxonomy (check 10).

        The taxonomy is parsed from ``SCHEMA.md``'s ``## Tag Taxonomy`` section
        (:func:`parse_taxonomy_tags`); any ``tags:`` entry not in that set is flagged
        ``Severity.STYLE``.

        Returns:
            The tag-audit findings.

        Raises:
            LintError: if ``SCHEMA.md`` is missing (the audit has no source of truth).
        """
        try:
            schema_text = self._read_text("SCHEMA.md")
        except LintError as exc:
            raise LintError(
                "SCHEMA.md is missing; cannot audit tags against the taxonomy"
            ) from exc
        taxonomy = parse_taxonomy_tags(schema_text)
        findings: list[Finding] = []
        for page in self._all_scanned_pages():
            for tag in _page_tags(page.meta):
                if tag not in taxonomy:
                    findings.append(
                        Finding(
                            check=10,
                            name="tag-audit",
                            severity=Severity.STYLE,
                            path=page.path,
                            message=f"tag {tag!r} is not in the SCHEMA.md taxonomy",
                        )
                    )
        return findings

    # ---- check 11: image hygiene -----------------------------------------------------

    def check_image_hygiene(self) -> list[Finding]:
        """Flag orphan assets, broken embeds and surviving sidecars (check 11).

        Three sub-checks (all ``Severity.BROKEN``): a binary in ``raw/assets/`` embedded
        by no page is an orphan binary; a page embedding an asset that does not exist is
        a broken embed; any ``raw/assets/*.md`` (a legacy per-image sidecar) is flagged
        for merge into its owning page.

        Returns:
            The image-hygiene findings.
        """
        pages = self._all_scanned_pages()
        embedded: set[str] = set()
        findings: list[Finding] = []
        assets = self._asset_filenames()
        for page in pages:
            for embed in extract_embeds(page.body):
                embedded.add(embed)
                if embed not in assets:
                    findings.append(
                        Finding(
                            check=11,
                            name="broken-embed",
                            severity=Severity.BROKEN,
                            path=page.path,
                            message=f"embeds missing asset ![[{embed}]]",
                        )
                    )
        for asset in sorted(assets):
            if asset not in embedded:
                findings.append(
                    Finding(
                        check=11,
                        name="orphan-binary",
                        severity=Severity.BROKEN,
                        path=f"{_ASSETS_DIR}/{asset}",
                        message="binary asset is embedded by no page",
                    )
                )
        for sidecar in sorted(self._asset_sidecars()):
            findings.append(
                Finding(
                    check=11,
                    name="asset-sidecar",
                    severity=Severity.BROKEN,
                    path=f"{_ASSETS_DIR}/{sidecar}",
                    message="legacy per-image sidecar; merge into its owning page",
                )
            )
        return findings

    # ---- check 12: log rotation ------------------------------------------------------

    def check_log_rotation(self) -> list[Finding]:
        """Flag a ``log.md`` with more than :data:`LOG_ROTATE_LIMIT` entries (check 12).

        Entries are counted by the ``## [`` block markers. At or below the limit
        passes; above it suggests rotating to ``log-YYYY.md`` (``Severity.STYLE``). A
        missing ``log.md`` yields no finding (nothing to rotate).

        Returns:
            The log-rotation findings.
        """
        try:
            log_text = self._read_text("log.md")
        except LintError:
            return []
        count = len(_LOG_ENTRY_RE.findall(log_text))
        if count > LOG_ROTATE_LIMIT:
            return [
                Finding(
                    check=12,
                    name="log-rotation",
                    severity=Severity.STYLE,
                    path="log.md",
                    message=(
                        f"log.md has {count} entries (> {LOG_ROTATE_LIMIT}); rotate to "
                        "log-YYYY.md"
                    ),
                )
            ]
        return []

    # ---- internal page model + walks -------------------------------------------------

    def _curated_pages(self) -> list[_Page]:
        """Return parsed pages in :data:`CURATED_DIRS` (spine files skipped).

        The lifecycle-free reference folders (entities/notes/memories): the orphan,
        index-completeness and stale checks scope to these.
        """
        return self._pages_in(CURATED_DIRS)

    def _actionable_pages(self) -> list[_Page]:
        """Return parsed pages in :data:`ACTIONABLE_DIRS` (spine files skipped).

        The lifecycle-bearing folder(s) (actions/, which also holds the media queue as
        actions tagged 'media'): the overdue / cold-media checks scope to these.
        """
        return self._pages_in(ACTIONABLE_DIRS)

    def _all_scanned_pages(self) -> list[_Page]:
        """Return reference + actionable + inbox pages (the set most checks scan).

        ``inbox/`` holding pages are machinery (exempt from the orphan / index checks,
        which scope to :data:`CURATED_DIRS`) but still carry the common frontmatter
        contract, so they are scanned here for the frontmatter / broken-link checks.
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
        """Parse every ``*.md`` in each folder, skipping spine + malformed pages.

        Each folder is confined through the vault, then walked recursively. Spine files
        (:data:`SPINE_FILES`) and anything under an :data:`EXCLUDED_DIRS` directory are
        skipped. A page whose frontmatter cannot be parsed is skipped (mirrors
        ``summary._iter_pages``) so a malformed page never wedges the whole run.

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

    def _asset_filenames(self) -> set[str]:
        """Return the set of binary (non-``.md``) filenames in ``raw/assets/``."""
        base = self._vault.root / _ASSETS_DIR
        if not base.is_dir():
            return set()
        return {
            path.name
            for path in base.iterdir()
            if path.is_file() and ASSET_SLUG_RE.fullmatch(path.name)
        }

    def _asset_sidecars(self) -> set[str]:
        """Return the set of ``*.md`` filenames in ``raw/assets/`` (legacy sidecars)."""
        base = self._vault.root / _ASSETS_DIR
        if not base.is_dir():
            return set()
        return {
            path.name
            for path in base.iterdir()
            if path.is_file() and path.suffix == ".md"
        }

    def _read_text(self, vault_relative_path: str) -> str:
        """Confine and read a spine file's full text, or raise :class:`LintError`.

        Args:
            vault_relative_path: A vault-relative path (for example ``index.md``).

        Returns:
            The file's UTF-8 text.

        Raises:
            LintError: if the path escapes the vault, the file is missing, or it cannot
                be read/decoded.
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

    @staticmethod
    def _inbound_targets(pages: list[_Page]) -> set[str]:
        """Return the set of normalised wikilink targets across ``pages``.

        Self-links (a page linking to its own slug) are excluded so a page cannot rescue
        itself from the orphan check.
        """
        inbound: set[str] = set()
        for page in pages:
            for target in extract_wikilinks(page.body):
                normalised = LintEngine._normalise_target(target)
                if normalised == page.slug or normalised == page.path.removesuffix(
                    ".md"
                ):
                    continue
                inbound.add(normalised)
        return inbound

    @staticmethod
    def _resolvable_targets(pages: list[_Page]) -> set[str]:
        """Return every handle a wikilink may resolve to: slug, path, and aliases."""
        resolvable: set[str] = set()
        for page in pages:
            resolvable.add(page.slug)
            resolvable.add(page.path)
            resolvable.add(page.path.removesuffix(".md"))
            resolvable.update(page.aliases)
        return resolvable

    @staticmethod
    def _normalise_target(target: str) -> str:
        """Strip the ``|alias`` and ``#anchor`` parts and trim a wikilink target."""
        head = target.split("|", 1)[0]
        head = head.split("#", 1)[0]
        return head.strip()


@dataclass(frozen=True, slots=True)
class _Page:
    """A parsed page used internally by the linter (path, slug, frontmatter, body)."""

    path: str
    slug: str
    meta: dict[str, object]
    body: str

    @property
    def aliases(self) -> set[str]:
        """The page's ``aliases`` frontmatter as a set of trimmed strings."""
        raw = self.meta.get("aliases")
        if isinstance(raw, list):
            return {
                item.strip() for item in raw if isinstance(item, str) and item.strip()
            }
        if isinstance(raw, str) and raw.strip():
            return {raw.strip()}
        return set()


# ---- module-level pure helpers (also unit-tested directly) ------------------------


def extract_wikilinks(body: str) -> list[str]:
    """Return the bare targets of every ``[[wikilink]]`` in ``body``.

    Recognises ``[[target]]``, ``[[target|alias]]`` and ``[[target#heading]]``; the
    alias and anchor portions are *not* stripped here (the caller normalises). An
    ``![[embed]]`` is *not* a wikilink (the leading ``!`` is excluded). Links inside
    fenced or inline code spans are ignored so code examples never produce false
    positives.

    Args:
        body: The page body markdown.

    Returns:
        The raw inner text of each wikilink, in document order.
    """
    stripped = _FENCE_RE.sub("", body)
    return [match.group(1).strip() for match in _WIKILINK_RE.finditer(stripped)]


def extract_embeds(body: str) -> list[str]:
    """Return the filenames of every ``![[asset.ext]]`` embed in ``body``.

    Only embeds (the ``![[...]]`` form, marked by the leading ``!``) are returned; plain
    ``[[wikilinks]]`` are ignored. Any ``|alias`` / ``#anchor`` suffix is stripped.
    Embeds inside fenced or inline code spans are ignored.

    Args:
        body: The page body markdown.

    Returns:
        The embedded filenames, in document order.
    """
    stripped = _FENCE_RE.sub("", body)
    out: list[str] = []
    for match in _EMBED_RE.finditer(stripped):
        inner = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        out.append(inner)
    return out


def parse_taxonomy_tags(schema_text: str) -> set[str]:
    """Return the tag set listed under ``## Tag Taxonomy`` in ``SCHEMA.md``.

    The taxonomy section (SPEC Appendix) lists tags as bullet lines of the form
    ``- <label>: tag-a, tag-b, tag-c``; this collects every comma-separated tag after
    the first colon on each bullet, between the ``## Tag Taxonomy`` heading and the next
    ``##`` heading. A label-less bullet (``- tag-a, tag-b``) is also accepted. The
    result is an empty set if the heading is absent.

    Args:
        schema_text: The full ``SCHEMA.md`` text.

    Returns:
        The set of taxonomy tag strings.
    """
    lines = schema_text.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if line.strip() == _TAXONOMY_HEADING
        )
    except StopIteration:
        return set()
    tags: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        bullet = stripped[2:]
        payload = bullet.split(":", 1)[1] if ":" in bullet else bullet
        for token in payload.split(","):
            tag = token.strip()
            if tag:
                tags.add(tag)
    return tags


def _page_tags(meta: dict[str, object]) -> list[str]:
    """Return a page's ``tags`` frontmatter as a list of trimmed strings."""
    raw = meta.get("tags")
    if isinstance(raw, list):
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _under_excluded_dir(rel: str) -> bool:
    """Return ``True`` if any path segment of ``rel`` is an excluded directory."""
    return any(segment in EXCLUDED_DIRS for segment in PurePosixPath(rel).parts)


def _is_truthy(value: object) -> bool:
    """Return ``True`` for boolean ``True`` or a truthy string (true / yes / 1)."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _str_field(value: object) -> str | None:
    """Return ``value`` as a stripped string, or ``None`` when absent/blank.

    Mirrors ``summary._str_field``: a real string is stripped (blank -> ``None``),
    ``None`` stays ``None``, and any other scalar is stringified.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if value is None:
        return None
    return str(value)


def _parse_date(value: object) -> date | None:
    """Coerce a frontmatter date-ish value to a :class:`date`, else ``None``.

    Mirrors ``summary._parse_date``: accepts a real :class:`~datetime.date` or
    :class:`~datetime.datetime`, and a ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM`` string
    (the trailing time is dropped). Any other value, an empty string, or an unparseable
    string yields ``None`` and never raises.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        head = text.split()[0]
        try:
            return date.fromisoformat(head)
        except ValueError:
            return None
    return None
