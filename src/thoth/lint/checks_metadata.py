"""Frontmatter / metadata checks: 3 (summary gloss), 4 (frontmatter), 6
(contradictions), 8 (quality signals) and 10 (tag audit), plus the Metadata-Menu
vocabularies they validate against.

Each check is a pure function over the parsed pages handed to it by
:class:`thoth.lint.LintEngine`. The folder / type / slug contract constants are
imported from :mod:`thoth.vault` so the closed-surface contract stays
single-sourced; the Metadata-Menu vocabularies (which the vault writer does not
enforce) are defined here from the SPEC frontmatter table.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from thoth.fmfields import _is_truthy, _page_tags, _str_field
from thoth.vault import (
    FOLDER_TYPE_CONTRACT,
    REQUIRED_COMMON_FIELDS,
    SUMMARY_TYPES,
    VALID_SOURCES,
    VALID_TYPES,
)

from .model import Finding, Severity, _finding, _Page
from .parse import parse_taxonomy_tags

__all__ = [
    "TYPE_REQUIRED_FIELDS",
    "STATUS_VOCAB",
    "PRIORITY_VOCAB",
    "MEDIA_TYPE_VOCAB",
]

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


def _check_summaries(pages: list[_Page]) -> list[Finding]:
    """Flag reference pages missing a one-line ``summary:`` gloss (check 3)."""
    findings: list[Finding] = []
    for page in pages:
        page_type = _str_field(page.meta.get("type"))
        if page_type not in SUMMARY_TYPES:
            continue
        if _str_field(page.meta.get("summary")) is None:
            findings.append(
                _finding(
                    3,
                    "summary-gloss",
                    Severity.STYLE,
                    page.path,
                    "reference page has no one-line summary: frontmatter",
                )
            )
    return findings


def _check_frontmatter(pages: list[_Page]) -> list[Finding]:
    """Validate frontmatter on every curated and life-admin page (check 4)."""
    findings: list[Finding] = []
    for page in pages:
        findings.extend(_frontmatter_findings(page))
    return findings


def _frontmatter_findings(page: _Page) -> list[Finding]:
    """Return the frontmatter findings for one scanned page."""
    meta = page.meta
    out: list[Finding] = []

    def flag(message: str) -> None:
        out.append(_finding(4, "frontmatter", Severity.STYLE, page.path, message))

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


def _check_contradictions(pages: list[_Page]) -> list[Finding]:
    """Flag pages marked ``contested`` or carrying ``contradictions`` (check 6)."""
    findings: list[Finding] = []
    for page in pages:
        if _is_truthy(page.meta.get("contested")):
            findings.append(
                _finding(
                    6,
                    "contested",
                    Severity.CONTESTED,
                    page.path,
                    "page is marked contested: true",
                )
            )
        contradictions = page.meta.get("contradictions")
        if isinstance(contradictions, list) and contradictions:
            joined = ", ".join(str(item) for item in contradictions)
            findings.append(
                _finding(
                    6,
                    "contradictions",
                    Severity.CONTESTED,
                    page.path,
                    f"page declares contradictions: {joined}",
                )
            )
    return findings


def _check_quality_signals(pages: list[_Page]) -> list[Finding]:
    """Flag low-confidence and uncorroborated single-source pages (check 8)."""
    findings: list[Finding] = []
    for page in pages:
        confidence = _str_field(page.meta.get("confidence"))
        if confidence == "low":
            findings.append(
                _finding(
                    8,
                    "quality",
                    Severity.STYLE,
                    page.path,
                    "page has confidence: low",
                )
            )
            continue
        sources = page.meta.get("sources")
        if isinstance(sources, list) and len(sources) == 1 and confidence is None:
            findings.append(
                _finding(
                    8,
                    "quality",
                    Severity.STYLE,
                    page.path,
                    "single-source page has no confidence field",
                )
            )
    return findings


def _check_tag_audit(schema_text: str, pages: list[_Page]) -> list[Finding]:
    """Flag pages using a tag absent from ``SCHEMA.md``'s taxonomy (check 10)."""
    taxonomy = parse_taxonomy_tags(schema_text)
    findings: list[Finding] = []
    for page in pages:
        for tag in _page_tags(page.meta):
            if tag not in taxonomy:
                findings.append(
                    _finding(
                        10,
                        "tag-audit",
                        Severity.STYLE,
                        page.path,
                        f"tag {tag!r} is not in the SCHEMA.md taxonomy",
                    )
                )
    return findings
