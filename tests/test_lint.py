"""Tests for :mod:`thoth.lint` -- the 13-check pure vault maintenance scan.

These build a real seeded vault under ``tmp_path`` (hand-authored spine + curated /
life-admin / raw pages carrying the relevant frontmatter, written straight to disk so
invalid and edge-case frontmatter can be exercised) and a real
:class:`~thoth.vault.Vault` over it, so every check runs against real files. The single
non-deterministic input -- the current date -- is injected as a frozen ``today`` so the
stale / overdue / media-cold windows are reproducible. No network, no LLM, no subprocess
is touched anywhere (a test asserts the module pulls in no client SDK).
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from thoth.config import Config, load_config
from thoth.lint import (
    ACTIONABLE_DIRS,
    CURATED_DIRS,
    EXCLUDED_DIRS,
    LOG_ROTATE_LIMIT,
    MEDIA_STALE_DAYS,
    PAGE_SIZE_LIMIT,
    SPINE_FILES,
    STALE_DAYS,
    Finding,
    LintEngine,
    LintError,
    LintReport,
    Severity,
    extract_embeds,
    extract_wikilinks,
    parse_taxonomy_tags,
)
from thoth.vault import Vault

# A frozen "today": the SPEC worked-example anchor day.
TODAY = date(2026, 6, 1)

_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "notes",
    "memories",
    "actions",
    "inbox",
    "_archive",
    "_bases",
    "_meta",
)

# A minimal SCHEMA.md whose taxonomy covers every tag the fixtures use.
_SCHEMA = """\
# Vault Schema

## Conventions
- File names: lowercase, hyphens.

## Tag Taxonomy
Add a tag HERE before using it. Seed set:
- Type: entity, note, memory, action
- Note kind: concept, comparison, query, reference, how-to
- Domain: embedded-systems, controls, accelerator, software, ai-ml, home
- People/Orgs: person, org, product, model
- Actionable: task, media, recurring, errand
- Quality: contested, prediction, controversy

## Page Thresholds
- CREATE a page when central.
"""

_INDEX = """\
---
title: Home
type: summary
updated: 2026-05-30
---

# 🏠 PKM Vault — Home

![[_bases/home.base#Recent Captures (7d)]]
"""

_LOG = """\
# Vault Log

> Append-only.

## [2026-05-30] create | Vault initialized
- structure
"""


# --------------------------------------------------------------------------------------
# fixtures + helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """A real Vault over a seeded folder skeleton + spine under tmp_path."""
    for folder in _FOLDERS:
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text(_INDEX, encoding="utf-8")
    (tmp_path / "log.md").write_text(_LOG, encoding="utf-8")
    config = load_config({"PKM_VAULT": str(tmp_path)})
    return Vault(config)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """The frozen Config matching the ``vault`` fixture's root."""
    return load_config({"PKM_VAULT": str(tmp_path)})


def _engine(vault: Vault, config: Config, *, today: date = TODAY) -> LintEngine:
    """Build a LintEngine with the frozen clock."""
    return LintEngine(config, vault, today=today)


def _write(
    vault: Vault, rel: str, frontmatter: dict[str, Any], body: str = "body\n"
) -> None:
    """Author a page file directly on disk (bypassing the validating writer).

    The lint scans read frontmatter straight off disk, so tests can author pages with
    arbitrary (including invalid) frontmatter without going through ``write_page``.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(str(v) for v in value) + "]"
            lines.append(f"{key}: {rendered}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path = vault.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_matching_raw(vault: Vault, subdir: str, slug: str, body: str) -> str:
    """Author a raw page via the real writer so its stored ``sha256`` is authoritative.

    Goes through :meth:`thoth.vault.Vault.write_raw` (the production writer that stamps
    ``sha256``) rather than hand-computing the digest, so the lint scan is exercised
    against the exact contract production emits.

    Returns:
        The vault-relative path written.
    """
    return vault.write_raw(
        subdir,
        slug,
        {"source_url": "https://example.com/x"},
        body,
        today=date(2026, 5, 30),
    )


def _knowledge(
    vault: Vault,
    folder: str,
    slug: str,
    *,
    title: str | None = None,
    page_type: str = "entity",
    body: str = "body\n",
    updated: str = "2026-05-30",
    created: str = "2026-05-30",
    tags: list[str] | None = None,
    summary: str | None = "what this page is about",
    extra: dict[str, Any] | None = None,
) -> None:
    """Author a curated knowledge page with the common contract satisfied.

    A reference page carries a one-line ``summary:`` gloss by default (#72), matching
    production where the curate pass authors one; pass ``summary=None`` to author a
    page that the summary-gloss lint check (check 3) should flag.
    """
    meta: dict[str, Any] = {
        "title": title if title is not None else slug,
        "type": page_type,
        "created": created,
        "updated": updated,
        "source": "slack",
        "tags": tags if tags is not None else ["entity"],
    }
    if summary is not None:
        meta["summary"] = summary
    if extra:
        meta.update(extra)
    _write(vault, f"{folder}/{slug}.md", meta, body)


# --------------------------------------------------------------------------------------
# import safety
# --------------------------------------------------------------------------------------


def test_module_imports_no_client_sdk() -> None:
    """Importing thoth.lint pulls in no Slack/anthropic/mcp/firecrawl SDK.

    Runs in a fresh interpreter: in-process the SDKs may already sit in
    ``sys.modules`` from earlier tests when the runtime extras are installed.
    """
    code = (
        "import sys, thoth.lint; "
        "banned = {'slack_bolt', 'slack_sdk', 'anthropic', 'mcp', 'firecrawl'}; "
        "loaded = banned & sys.modules.keys(); "
        "assert not loaded, sorted(loaded)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_lint_error_is_exception() -> None:
    """LintError is a plain Exception subclass."""
    assert issubclass(LintError, Exception)


# --------------------------------------------------------------------------------------
# happy path: a fully-valid seeded vault is clean
# --------------------------------------------------------------------------------------


def test_clean_vault_yields_no_findings(vault: Vault, config: Config) -> None:
    """A valid vault (linked pages, valid frontmatter, embed, fresh raw) is clean."""
    # Two knowledge pages that link each other (no orphans, no broken links).
    _knowledge(
        vault,
        "entities",
        "alpha",
        page_type="entity",
        body="see [[beta]] and the diagram ![[diagram-ab12.png]]\n",
        tags=["entity", "controls"],
    )
    _knowledge(
        vault,
        "notes",
        "beta",
        page_type="note",
        body="see [[alpha]]\n",
        tags=["concept"],
    )
    # The embedded asset exists.
    (vault.root / "raw/assets/diagram-ab12.png").write_bytes(b"\x89PNG\r\n")
    # A fresh raw file written by the real writer (its stored sha256 is authoritative).
    _write_matching_raw(vault, "articles", "src", "raw article text\n")
    # index.md is static (ADR 0008) -- the seeded spine needs no catalog or page-count.

    report = _engine(vault, config).run()
    assert report.is_clean, [f.message for f in report.findings]
    assert report.total == 0


# --------------------------------------------------------------------------------------
# check 1: orphans
# --------------------------------------------------------------------------------------


def test_orphan_reference_page_flagged_actionable_exempt(
    vault: Vault, config: Config
) -> None:
    """A reference page with no inbound link is an orphan; an action page is not."""
    _knowledge(vault, "entities", "lonely", page_type="entity", body="no links\n")
    # An actionable page with no inbound link must NOT be flagged.
    _write(
        vault,
        "actions/task.md",
        {
            "title": "Task",
            "type": "action",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
        },
    )
    orphans = _engine(vault, config).check_orphans()
    paths = {f.path for f in orphans}
    assert "entities/lonely.md" in paths
    assert "actions/task.md" not in paths
    assert all(f.severity is Severity.ORPHAN for f in orphans)


def test_orphan_resolved_via_alias_target(vault: Vault, config: Config) -> None:
    """A page reachable only through an alias-target wikilink is not an orphan."""
    # 'target' is reachable only as [[PMC]] (an alias of target), not by its slug.
    _knowledge(
        vault,
        "entities",
        "target",
        page_type="entity",
        body="content\n",
        extra={"aliases": ["PMC"]},
    )
    _knowledge(vault, "notes", "pointer", page_type="note", body="see [[PMC]]\n")
    orphans = {f.path for f in _engine(vault, config).check_orphans()}
    assert "entities/target.md" not in orphans
    # 'pointer' itself has no inbound link, so it is the orphan here.
    assert "notes/pointer.md" in orphans


def test_self_link_does_not_rescue_orphan(vault: Vault, config: Config) -> None:
    """A page linking only to its own slug is still an orphan."""
    _knowledge(
        vault, "entities", "selfish", page_type="entity", body="I am [[selfish]]\n"
    )
    orphans = {f.path for f in _engine(vault, config).check_orphans()}
    assert "entities/selfish.md" in orphans


# --------------------------------------------------------------------------------------
# check 2: broken wikilinks
# --------------------------------------------------------------------------------------


def test_broken_wikilink_flagged_highest_severity(vault: Vault, config: Config) -> None:
    """[[no-such-page]] is a BROKEN finding (the highest severity)."""
    _knowledge(
        vault, "entities", "a", page_type="entity", body="link to [[no-such-page]]\n"
    )
    findings = _engine(vault, config).check_broken_wikilinks()
    assert [f.severity for f in findings] == [Severity.BROKEN]
    assert "no-such-page" in findings[0].message
    assert Severity.BROKEN == min(Severity)


def test_label_and_anchor_wikilinks_resolve(vault: Vault, config: Config) -> None:
    """[[real|Label]] and [[real#section]] resolve (label/anchor stripped)."""
    _knowledge(vault, "entities", "real", page_type="entity", body="x\n")
    _knowledge(
        vault,
        "notes",
        "src",
        page_type="note",
        body="[[real|A Label]] and [[real#Heading]]\n",
    )
    broken = _engine(vault, config).check_broken_wikilinks()
    assert broken == []


def test_alias_target_is_not_broken(vault: Vault, config: Config) -> None:
    """A wikilink matching a page's aliases entry is not broken."""
    _knowledge(
        vault,
        "entities",
        "pmc",
        page_type="entity",
        body="x\n",
        extra={"aliases": ["Program Motion Controller"]},
    )
    _knowledge(
        vault,
        "notes",
        "ref",
        page_type="note",
        body="see [[Program Motion Controller]]\n",
    )
    broken = {f.path for f in _engine(vault, config).check_broken_wikilinks()}
    assert "notes/ref.md" not in broken


def test_wikilink_resolves_by_full_path(vault: Vault, config: Config) -> None:
    """A wikilink written as a full vault path (entities/jane) resolves."""
    _write(
        vault,
        "entities/jane-doe.md",
        {
            "title": "Jane",
            "type": "entity",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["person"],
        },
    )
    _knowledge(
        vault,
        "entities",
        "team",
        page_type="entity",
        body="with [[entities/jane-doe]]\n",
    )
    broken = _engine(vault, config).check_broken_wikilinks()
    assert broken == []


# --------------------------------------------------------------------------------------
# check 3: summary gloss (#72 / ADR 0008)
# --------------------------------------------------------------------------------------


def test_summary_gloss_flags_reference_page_missing_summary(
    vault: Vault, config: Config
) -> None:
    """A reference page with no ``summary:`` is flagged; a glossed one is not."""
    _knowledge(vault, "entities", "glossed", page_type="entity", body="[[bare]]\n")
    _knowledge(
        vault,
        "entities",
        "bare",
        page_type="entity",
        body="[[glossed]]\n",
        summary=None,
    )
    findings = _engine(vault, config).check_summaries()
    flagged = {f.path for f in findings if f.name == "summary-gloss"}
    assert "entities/bare.md" in flagged
    assert "entities/glossed.md" not in flagged
    assert all(f.severity is Severity.STYLE for f in findings)


def test_summary_gloss_flags_blank_summary(vault: Vault, config: Config) -> None:
    """A reference page whose ``summary:`` is present but blank is still flagged."""
    _knowledge(vault, "notes", "blank", page_type="note", body="x\n", summary="   ")
    findings = _engine(vault, config).check_summaries()
    assert {f.path for f in findings} == {"notes/blank.md"}


def test_summary_gloss_exempts_action_pages(vault: Vault, config: Config) -> None:
    """An action page needs no ``summary:`` (it is surfaced by the dashboards)."""
    _knowledge(
        vault,
        "actions",
        "todo",
        page_type="action",
        body="do it\n",
        tags=["task"],
        summary=None,
        extra={"status": "todo"},
    )
    findings = _engine(vault, config).check_summaries()
    assert findings == []


# --------------------------------------------------------------------------------------
# check 4: frontmatter validation
# --------------------------------------------------------------------------------------


def test_frontmatter_missing_common_field_flagged(vault: Vault, config: Config) -> None:
    """A page missing a required common field (source) is flagged."""
    _write(
        vault,
        "entities/no-source.md",
        {
            "title": "No source",
            "type": "entity",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "tags": ["entity"],
        },
    )
    msgs = [
        f.message
        for f in _engine(vault, config).check_frontmatter()
        if f.path == "entities/no-source.md"
    ]
    assert any("'source'" in m for m in msgs)


def test_frontmatter_invalid_type_flagged(vault: Vault, config: Config) -> None:
    """An invalid `type` value is flagged."""
    _write(
        vault,
        "inbox/weird.md",
        {
            "title": "Weird",
            "type": "banana",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["task"],
        },
    )
    msgs = [f.message for f in _engine(vault, config).check_frontmatter()]
    assert any("invalid type 'banana'" in m for m in msgs)


def test_frontmatter_action_missing_status_flagged(
    vault: Vault, config: Config
) -> None:
    """An action missing its required `status` field is flagged."""
    _write(
        vault,
        "actions/no-status.md",
        {
            "title": "No status",
            "type": "action",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["task"],
        },
    )
    msgs = [
        f.message
        for f in _engine(vault, config).check_frontmatter()
        if f.path == "actions/no-status.md"
    ]
    assert any("'status'" in m for m in msgs)


def test_frontmatter_bad_vocab_values_flagged(vault: Vault, config: Config) -> None:
    """status/priority/media_type values outside the vocab are each flagged.

    A media item is an ``action`` tagged ``media`` filed under ``actions/`` (ADR 0005).
    """
    _write(
        vault,
        "actions/bad.md",
        {
            "title": "Bad media",
            "type": "action",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["media"],
            "status": "watching",  # not in the action status vocab
            "priority": "super-high",  # not in PRIORITY_VOCAB
            "media_type": "scroll",  # not in MEDIA_TYPE_VOCAB
        },
    )
    msgs = [
        f.message
        for f in _engine(vault, config).check_frontmatter()
        if f.path == "actions/bad.md"
    ]
    assert any("status 'watching'" in m for m in msgs)
    assert any("priority 'super-high'" in m for m in msgs)
    assert any("media_type 'scroll'" in m for m in msgs)


def test_frontmatter_folder_type_mismatch_flagged(vault: Vault, config: Config) -> None:
    """A page whose type is not allowed in its folder is flagged (single-sourced)."""
    # A `note`-typed page placed in entities/ violates the folder-by-type contract.
    _write(
        vault,
        "entities/wrong.md",
        {
            "title": "Wrong",
            "type": "note",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["concept"],
        },
        body="[[other]]\n",
    )
    msgs = [
        f.message
        for f in _engine(vault, config).check_frontmatter()
        if f.path == "entities/wrong.md"
    ]
    assert any("not allowed in folder 'entities'" in m for m in msgs)


def test_frontmatter_valid_page_passes(vault: Vault, config: Config) -> None:
    """A fully-valid action passes the frontmatter check."""
    _write(
        vault,
        "actions/good.md",
        {
            "title": "Good",
            "type": "action",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
            "priority": "2 - High",
        },
    )
    findings = [
        f
        for f in _engine(vault, config).check_frontmatter()
        if f.path == "actions/good.md"
    ]
    assert findings == []


# --------------------------------------------------------------------------------------
# check 5: stale content
# --------------------------------------------------------------------------------------


def test_stale_knowledge_page_flagged(vault: Vault, config: Config) -> None:
    """A knowledge page updated > STALE_DAYS ago is flagged; a fresh one is not."""
    old = (TODAY - _dt.timedelta(days=STALE_DAYS + 1)).isoformat()
    fresh = (TODAY - _dt.timedelta(days=STALE_DAYS - 1)).isoformat()
    _knowledge(
        vault, "entities", "old", page_type="entity", updated=old, body="[[fresh]]\n"
    )
    _knowledge(
        vault,
        "notes",
        "fresh",
        page_type="note",
        updated=fresh,
        body="[[old]]\n",
    )
    stale = {f.path for f in _engine(vault, config).check_stale() if f.name == "stale"}
    assert "entities/old.md" in stale
    assert "notes/fresh.md" not in stale


def test_overdue_open_action_flagged_closed_exempt(
    vault: Vault, config: Config
) -> None:
    """An open action past its due date is flagged; done/cancelled are exempt."""
    past = (TODAY - _dt.timedelta(days=5)).isoformat()
    for slug, status in (
        ("late", "todo"),
        ("finished", "done"),
        ("dropped", "cancelled"),
    ):
        _write(
            vault,
            f"actions/{slug}.md",
            {
                "title": slug,
                "type": "action",
                "created": "2026-05-01",
                "updated": "2026-05-01",
                "source": "slack",
                "tags": ["task"],
                "status": status,
                "due_date": past,
            },
        )
    findings = _engine(vault, config).check_stale()
    overdue = {f.path for f in findings if f.name == "overdue"}
    assert overdue == {"actions/late.md"}


def test_media_to_consume_cold_flagged(vault: Vault, config: Config) -> None:
    """A to_consume media action older than MEDIA_STALE_DAYS is flagged; recent is not.

    ADR 0005: a media item is an ``action`` tagged ``media`` under ``actions/``.
    """
    cold = (TODAY - _dt.timedelta(days=MEDIA_STALE_DAYS + 1)).isoformat()
    warm = (TODAY - _dt.timedelta(days=MEDIA_STALE_DAYS - 1)).isoformat()
    for slug, created in (("cold", cold), ("warm", warm)):
        _write(
            vault,
            f"actions/{slug}.md",
            {
                "title": slug,
                "type": "action",
                "created": created,
                "updated": created,
                "source": "slack",
                "tags": ["media"],
                "status": "to_consume",
            },
        )
    cold_paths = {
        f.path for f in _engine(vault, config).check_stale() if f.name == "media-cold"
    }
    assert cold_paths == {"actions/cold.md"}


# --------------------------------------------------------------------------------------
# check 6: contradictions
# --------------------------------------------------------------------------------------


def test_contested_and_contradictions_flagged(vault: Vault, config: Config) -> None:
    """A contested page and a page with a contradictions list are both flagged."""
    _knowledge(
        vault,
        "entities",
        "disputed",
        page_type="entity",
        body="[[clean]]\n",
        extra={"contested": True},
    )
    _knowledge(
        vault,
        "notes",
        "conflicted",
        page_type="note",
        body="[[clean]]\n",
        extra={"contradictions": ["other-slug"]},
    )
    _knowledge(vault, "entities", "clean", page_type="entity", body="[[disputed]]\n")
    findings = _engine(vault, config).check_contradictions()
    flagged = {f.path for f in findings}
    assert "entities/disputed.md" in flagged
    assert "notes/conflicted.md" in flagged
    assert "entities/clean.md" not in flagged
    assert all(f.severity is Severity.CONTESTED for f in findings)


# --------------------------------------------------------------------------------------
# check 7: source drift
# --------------------------------------------------------------------------------------


def test_source_drift_detects_mismatch_and_passes_match(
    vault: Vault, config: Config
) -> None:
    """A raw body sha256 != stored sha256 is flagged DRIFT; a match passes."""
    # matching raw: written by the real writer, so its stored sha256 is authoritative
    _write_matching_raw(vault, "articles", "good", "the real body text\n")
    # drifted raw: stored hash is an obviously-fake digest, not the real one
    _write(
        vault,
        "raw/articles/drifted.md",
        {
            "source_url": "https://example.com/d",
            "ingested": "2026-05-30",
            "sha256": "x" * 64,
        },
        "the real body text\n",
    )
    findings = _engine(vault, config).check_source_drift()
    assert [f.path for f in findings] == ["raw/articles/drifted.md"]
    assert findings[0].severity is Severity.DRIFT


def test_source_drift_no_false_positive_for_writer_output(
    vault: Vault, config: Config
) -> None:
    """A page written by Vault.write_raw must never self-report drift (regression).

    The stored ``sha256`` is stamped over the parse-stable body, so re-deriving it from
    disk (exactly what check 7 does) matches even for the common case of a body ending
    in a trailing newline -- which previously produced a spurious DRIFT finding.
    """
    for subdir, slug, body in (
        ("articles", "trailing-nl", "the real article body\n"),
        ("papers", "no-trailing-nl", "no trailing newline"),
        ("transcripts", "multiline", "line one\nline two\n\n"),
        ("articles", "leading-blank", "\nbody after a leading blank line\n"),
    ):
        vault.write_raw(subdir, slug, {"source_url": "https://example.com/x"}, body)
    assert _engine(vault, config).check_source_drift() == []


def test_source_drift_skips_raw_without_sha256(vault: Vault, config: Config) -> None:
    """A raw file with no sha256 frontmatter is skipped (not an error)."""
    _write(
        vault,
        "raw/papers/nohash.md",
        {"source_url": "https://example.com/p", "ingested": "2026-05-30"},
        "body\n",
    )
    assert _engine(vault, config).check_source_drift() == []


# --------------------------------------------------------------------------------------
# check 8: quality signals
# --------------------------------------------------------------------------------------


def test_quality_low_confidence_and_single_source(vault: Vault, config: Config) -> None:
    """confidence: low and single-source-without-confidence are both flagged STYLE."""
    _knowledge(
        vault,
        "entities",
        "shaky",
        page_type="entity",
        body="[[ok]]\n",
        extra={"confidence": "low"},
    )
    _knowledge(
        vault,
        "notes",
        "single",
        page_type="note",
        body="[[ok]]\n",
        extra={"sources": ["raw/articles/one.md"]},
    )
    # multi-source page passes; high-confidence page passes
    _knowledge(
        vault,
        "entities",
        "ok",
        page_type="entity",
        body="[[shaky]] [[single]]\n",
        extra={
            "sources": ["raw/articles/one.md", "raw/articles/two.md"],
            "confidence": "high",
        },
    )
    flagged = {f.path for f in _engine(vault, config).check_quality_signals()}
    assert "entities/shaky.md" in flagged
    assert "notes/single.md" in flagged
    assert "entities/ok.md" not in flagged


# --------------------------------------------------------------------------------------
# check 9: page size
# --------------------------------------------------------------------------------------


def test_page_size_over_limit_flagged_at_limit_passes(
    vault: Vault, config: Config
) -> None:
    """A body > PAGE_SIZE_LIMIT lines is flagged; exactly at the limit passes."""
    over_body = "\n".join(["line"] * (PAGE_SIZE_LIMIT + 1)) + "\n"
    at_body = "\n".join(["line"] * PAGE_SIZE_LIMIT) + "\n"
    _knowledge(
        vault, "entities", "big", page_type="entity", body=over_body + "[[small]]\n"
    )
    # Re-author 'big' so its body is exactly OVER the limit (link line bumps it anyway).
    _knowledge(vault, "entities", "big", page_type="entity", body=over_body)
    _knowledge(vault, "notes", "small", page_type="note", body=at_body)
    flagged = {f.path for f in _engine(vault, config).check_page_size()}
    assert "entities/big.md" in flagged
    assert "notes/small.md" not in flagged


def test_page_size_actionable_exempt(vault: Vault, config: Config) -> None:
    """Actionable (actions/) pages are exempt from the page-size check."""
    over_body = "\n".join(["line"] * (PAGE_SIZE_LIMIT + 50)) + "\n"
    _write(
        vault,
        "actions/huge.md",
        {
            "title": "Huge",
            "type": "action",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["task"],
            "status": "todo",
        },
        over_body,
    )
    assert _engine(vault, config).check_page_size() == []


# --------------------------------------------------------------------------------------
# check 10: tag audit
# --------------------------------------------------------------------------------------


def test_tag_audit_flags_unknown_tag(vault: Vault, config: Config) -> None:
    """A tag absent from SCHEMA.md's taxonomy is flagged; taxonomy-only passes."""
    _knowledge(
        vault,
        "entities",
        "stray",
        page_type="entity",
        body="[[clean]]\n",
        tags=["entity", "totally-made-up"],
    )
    _knowledge(
        vault,
        "notes",
        "clean",
        page_type="note",
        body="[[stray]]\n",
        tags=["concept", "controls"],
    )
    findings = _engine(vault, config).check_tag_audit()
    flagged = {(f.path, f.message) for f in findings}
    assert any(p == "entities/stray.md" and "totally-made-up" in m for p, m in flagged)
    assert not any(p == "notes/clean.md" for p, _ in flagged)


def test_tag_audit_missing_schema_raises(vault: Vault, config: Config) -> None:
    """A missing SCHEMA.md degrades to a raised LintError (asserted)."""
    (vault.root / "SCHEMA.md").unlink()
    _knowledge(vault, "entities", "x", page_type="entity", body="y\n")
    with pytest.raises(LintError):
        _engine(vault, config).check_tag_audit()


# --------------------------------------------------------------------------------------
# check 11: image hygiene
# --------------------------------------------------------------------------------------


def test_image_hygiene_orphan_broken_and_sidecar(vault: Vault, config: Config) -> None:
    """Orphan binary, broken embed, and a surviving sidecar are all flagged BROKEN."""
    # An asset that nothing embeds -> orphan binary.
    (vault.root / "raw/assets/orphan-img-aa11.png").write_bytes(b"\x89PNG")
    # An asset that IS embedded -> not orphan.
    (vault.root / "raw/assets/used-img-bb22.png").write_bytes(b"\x89PNG")
    # A legacy per-image sidecar .md -> flagged for merge.
    (vault.root / "raw/assets/legacy-cc33.md").write_text(
        "old description\n", encoding="utf-8"
    )
    _knowledge(
        vault,
        "entities",
        "owner",
        page_type="entity",
        body="![[used-img-bb22.png]] and ![[missing-dd44.png]] [[friend]]\n",
    )
    _knowledge(vault, "notes", "friend", page_type="note", body="[[owner]]\n")

    findings = _engine(vault, config).check_image_hygiene()
    by_name: dict[str, set[str]] = {}
    for f in findings:
        by_name.setdefault(f.name, set()).add(f.path)
        assert f.severity is Severity.BROKEN
    assert "raw/assets/orphan-img-aa11.png" in by_name["orphan-binary"]
    assert "raw/assets/used-img-bb22.png" not in by_name.get("orphan-binary", set())
    assert "entities/owner.md" in by_name["broken-embed"]
    assert "raw/assets/legacy-cc33.md" in by_name["asset-sidecar"]


# --------------------------------------------------------------------------------------
# check 12: log rotation
# --------------------------------------------------------------------------------------


def test_log_rotation_over_limit_flagged_short_passes(
    vault: Vault, config: Config
) -> None:
    """A log.md with > LOG_ROTATE_LIMIT entries is flagged; a short log passes."""
    # short log (seed has 1 entry) passes
    assert _engine(vault, config).check_log_rotation() == []
    # now write an over-limit log
    entries = "\n".join(
        f"## [2026-05-30] create | entry {i}" for i in range(LOG_ROTATE_LIMIT + 1)
    )
    (vault.root / "log.md").write_text(f"# Vault Log\n\n{entries}\n", encoding="utf-8")
    findings = _engine(vault, config).check_log_rotation()
    assert len(findings) == 1
    assert findings[0].severity is Severity.STYLE
    assert "log-YYYY.md" in findings[0].message


# --------------------------------------------------------------------------------------
# run() aggregation + report rendering
# --------------------------------------------------------------------------------------


def test_run_sorts_findings_by_severity_check_path(
    vault: Vault, config: Config
) -> None:
    """run() returns findings sorted by (severity, check, path)."""
    # A broken wikilink (BROKEN, check 2) + an orphan (ORPHAN, check 1) together.
    _knowledge(vault, "entities", "z-orphan", page_type="entity", body="[[ghost]]\n")
    report = _engine(vault, config).run()
    keys = [(int(f.severity), f.check, f.path) for f in report.findings]
    assert keys == sorted(keys)
    # broken (severity 0) must sort before orphan (severity 1)
    severities = [f.severity for f in report.findings]
    assert severities.index(Severity.BROKEN) < severities.index(Severity.ORPHAN)


def test_report_render_groups_with_counts(vault: Vault, config: Config) -> None:
    """render() lists groups in Severity order with a per-group count header."""
    _knowledge(vault, "entities", "orphan-one", page_type="entity", body="[[nope]]\n")
    report = _engine(vault, config).run()
    text = report.render()
    assert text.startswith(f"lint: {report.total} issue(s) found")
    # BROKEN group header precedes ORPHAN group header
    assert "BROKEN (" in text
    assert "ORPHAN (" in text
    assert text.index("BROKEN (") < text.index("ORPHAN (")


def test_report_by_severity_groups_correctly(vault: Vault, config: Config) -> None:
    """by_severity() returns (severity, findings) pairs in ascending severity order."""
    _knowledge(vault, "entities", "orphan-x", page_type="entity", body="[[gone]]\n")
    report = _engine(vault, config).run()
    groups = report.by_severity()
    severities = [sev for sev, _ in groups]
    assert severities == sorted(severities)
    total_in_groups = sum(len(items) for _, items in groups)
    assert total_in_groups == report.total


def test_clean_report_renders_clean_line() -> None:
    """A clean LintReport renders the 'clean' line and is_clean is True."""
    report = LintReport(findings=())
    assert report.is_clean is True
    assert report.total == 0
    assert "0 issues found" in report.render()


def test_finding_and_report_are_frozen() -> None:
    """Finding and LintReport are immutable dataclasses."""
    finding = Finding(
        check=1,
        name="orphan",
        severity=Severity.ORPHAN,
        path="entities/x.md",
        message="m",
    )
    report = LintReport(findings=(finding,))
    with pytest.raises(AttributeError):
        finding.message = "y"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        report.findings = ()  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# record() (check 13)
# --------------------------------------------------------------------------------------


def test_record_appends_exactly_one_log_block(vault: Vault, config: Config) -> None:
    """record() appends exactly one '## [..] lint | N issues found' block."""
    _knowledge(vault, "entities", "broken", page_type="entity", body="[[ghost]]\n")
    engine = _engine(vault, config)
    report = engine.run()
    assert report.total >= 1

    before = (vault.root / "log.md").read_text(encoding="utf-8")
    engine.record(report)
    after = (vault.root / "log.md").read_text(encoding="utf-8")

    new_blocks = after.count("## [") - before.count("## [")
    assert new_blocks == 1
    assert f"lint | {report.total} issues found" in after


def test_record_clean_report_still_logs_zero(vault: Vault, config: Config) -> None:
    """record() on a clean report logs a '0 issues found' entry."""
    engine = _engine(vault, config)
    report = LintReport(findings=())
    before = (vault.root / "log.md").read_text(encoding="utf-8").count("## [")
    engine.record(report)
    text = (vault.root / "log.md").read_text(encoding="utf-8")
    assert text.count("## [") - before == 1
    assert "lint | 0 issues found" in text


# --------------------------------------------------------------------------------------
# edge cases: malformed pages, excluded dirs, confinement, today default
# --------------------------------------------------------------------------------------


def test_malformed_yaml_page_skipped_run_survives(vault: Vault, config: Config) -> None:
    """A malformed-YAML page is skipped by the scans without crashing the run."""
    _knowledge(vault, "entities", "good", page_type="entity", body="[[friend]]\n")
    _knowledge(vault, "notes", "friend", page_type="note", body="[[good]]\n")
    (vault.root / "entities/garbage.md").write_text(
        "---\n: : : not yaml : :\n---\nbody\n", encoding="utf-8"
    )
    # run() must not raise; the good pages are still scanned.
    report = _engine(vault, config).run()
    assert isinstance(report, LintReport)


def test_excluded_dirs_not_scanned(vault: Vault, config: Config) -> None:
    """Pages under _archive/_bases/_meta are not scanned for orphans/size/etc."""
    # An archived page with no inbound link would be an orphan if scanned.
    _write(
        vault,
        "_archive/old-entity.md",
        {
            "title": "Archived",
            "type": "entity",
            "created": "2026-01-01",
            "updated": "2026-01-01",
            "source": "slack",
            "tags": ["totally-made-up"],  # would also trip tag-audit if scanned
        },
    )
    engine = _engine(vault, config)
    assert engine.check_orphans() == []
    assert all(f.path != "_archive/old-entity.md" for f in engine.check_tag_audit())
    assert all(not f.path.startswith("_archive/") for f in engine.check_frontmatter())


def test_today_defaults_to_a_real_date(vault: Vault, config: Config) -> None:
    """Omitting today uses the current London date (a real date)."""
    engine = LintEngine(config, vault)
    assert isinstance(engine.today, date)


def test_missing_folders_yield_clean_run(tmp_path: Path) -> None:
    """A bare vault (only the spine) lints clean without crashing."""
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text(_INDEX, encoding="utf-8")
    (tmp_path / "log.md").write_text(_LOG, encoding="utf-8")
    config = load_config({"PKM_VAULT": str(tmp_path)})
    vault = Vault(config)
    report = LintEngine(config, vault, today=TODAY).run()
    assert report.is_clean


# --------------------------------------------------------------------------------------
# module-level helpers
# --------------------------------------------------------------------------------------


def test_extract_wikilinks_brackets_pipes_anchors_and_fences() -> None:
    """extract_wikilinks handles pipes/anchors and ignores code-fenced links."""
    body = (
        "plain [[alpha]], aliased [[beta|Beta Label]], anchored [[gamma#Section]].\n"
        "an embed ![[asset.png]] is NOT a wikilink.\n"
        "```\n[[in-fence]]\n```\n"
        "inline `[[in-code]]` too.\n"
    )
    links = extract_wikilinks(body)
    assert links == ["alpha", "beta|Beta Label", "gamma#Section"]
    assert "in-fence" not in " ".join(links)
    assert "in-code" not in " ".join(links)
    assert "asset.png" not in " ".join(links)


def test_extract_embeds_distinguishes_embeds_from_links() -> None:
    """extract_embeds returns only ![[...]] filenames, stripping alias/anchor."""
    body = (
        "![[diagram-aa11.png]] and a link [[not-an-embed]] "
        "and ![[photo.jpg|caption]].\n"
        "```\n![[in-fence.png]]\n```\n"
    )
    embeds = extract_embeds(body)
    assert embeds == ["diagram-aa11.png", "photo.jpg"]
    assert "not-an-embed" not in embeds
    assert "in-fence.png" not in embeds


def test_parse_taxonomy_tags_reads_bullets() -> None:
    """parse_taxonomy_tags parses comma-separated tags under the heading only."""
    tags = parse_taxonomy_tags(_SCHEMA)
    # a sample of the documented seed set
    for expected in ("entity", "concept", "task", "media", "memory", "controls"):
        assert expected in tags
    # a bullet from a different section ('CREATE a page when central.') is excluded
    assert "CREATE a page when central." not in tags


def test_parse_taxonomy_tags_absent_heading_is_empty() -> None:
    """A SCHEMA.md without a Tag Taxonomy heading yields an empty set."""
    assert parse_taxonomy_tags("# Vault Schema\n\n## Conventions\n- nope\n") == set()


# --------------------------------------------------------------------------------------
# constants sanity (single-sourcing + contract alignment)
# --------------------------------------------------------------------------------------


def test_constants_align_with_contract() -> None:
    """The folder/spine/excluded constants match the SPEC shape."""
    assert CURATED_DIRS == ("entities", "notes", "memories")
    assert ACTIONABLE_DIRS == ("actions",)
    assert SPINE_FILES == frozenset({"index.md", "SCHEMA.md", "log.md"})
    assert {"_archive", "_bases", "_meta"} <= EXCLUDED_DIRS
    assert PAGE_SIZE_LIMIT == 200
    assert LOG_ROTATE_LIMIT == 500
