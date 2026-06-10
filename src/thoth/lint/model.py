"""Data model for the lint scan: severities, findings, the report, and errors.

These are pure value types shared by every check module: the :class:`Severity`
ordering, the frozen :class:`Finding` record, the aggregated :class:`LintReport`,
the :class:`LintError` failure type, and the internal parsed-page record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Severity",
    "Finding",
    "LintReport",
    "LintError",
]


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


def _finding(
    check: int, name: str, severity: Severity, path: str, message: str
) -> Finding:
    """Build one :class:`Finding` (positional shorthand used by every check)."""
    return Finding(
        check=check, name=name, severity=severity, path=path, message=message
    )


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
