"""Tests for :mod:`thoth.vault` -- the closed, path-confined vault surface.

These tests build a minimal vault under ``tmp_path`` (a seeded ``index.md`` + ``log.md``
plus the folder skeleton) and exercise the security core (path confinement), the
slug/folder/type grammar, frontmatter read/write with date stamping, the append-only
navigation edits, asset moves, and secret redaction. No network, no subprocess.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import frontmatter
import pytest

from thoth.config import Config, load_config
from thoth.vault import (
    ASSET_SLUG_RE,
    FOLDER_TYPE_CONTRACT,
    KNOWLEDGE_DIRS,
    KNOWLEDGE_TYPES,
    LIFE_ADMIN_DIRS,
    LIFE_ADMIN_TYPES,
    RAW_SUBDIRS,
    REQUIRED_COMMON_FIELDS,
    SLUG_RE,
    TYPE_ENUMERATION,
    VALID_SOURCES,
    VALID_TYPES,
    Page,
    PathConfinementError,
    SchemaError,
    SlugError,
    Vault,
    VaultError,
    redact_secrets,
    slugify,
)

# Obviously-fake, concatenated token shapes only (gitleaks scans the commit). Building
# them by concatenation keeps any single literal from looking like a live secret.
FAKE_OPENAI = "sk-" + "x" * 24
FAKE_GITHUB = "ghp_" + "A" * 24
FAKE_AWS = "AKIA" + "X" * 16
FAKE_BEARER_TOKEN = "a" * 30


# --- fixtures ----------------------------------------------------------------------

_INDEX_SEED = """\
---
title: Home
type: summary
updated: 2026-05-30
---

# Home

## Knowledge catalog

### Entities

### Concepts

### Comparisons

### Queries

### People
"""

_LOG_SEED = """\
# Vault Log

> Append-only.

## [2026-05-30] create | Vault initialized
- structure seeded
"""

_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "concepts",
    "comparisons",
    "queries",
    "actions",
    "media",
    "memories",
    "people",
    "inbox",
)


def _seed_vault(root: Path) -> None:
    """Write the minimal vault skeleton (folders + index.md + log.md) under ``root``."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
    (root / "log.md").write_text(_LOG_SEED, encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """A Vault over a freshly seeded tmp_path vault (no network, no subprocess)."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_vault(root)
    config = load_config({"PKM_VAULT": str(root)})
    return Vault(config)


def _valid_frontmatter(**overrides: object) -> dict[str, object]:
    """Return a complete, valid common-frontmatter mapping with optional overrides."""
    meta: dict[str, object] = {
        "title": "Program Motion Controller",
        "type": "entity",
        "source": "slack",
        "tags": ["controls", "embedded-systems"],
    }
    meta.update(overrides)
    return meta


# --- module constants --------------------------------------------------------------


def test_type_constants_partition() -> None:
    """VALID_TYPES is exactly the union of knowledge and life-admin, no overlap."""
    assert VALID_TYPES == KNOWLEDGE_TYPES | LIFE_ADMIN_TYPES
    assert KNOWLEDGE_TYPES.isdisjoint(LIFE_ADMIN_TYPES)
    assert VALID_SOURCES == {"slack", "mcp", "web", "manual", "cron"}


def test_folder_type_contract_values_are_valid_types() -> None:
    """Every type permitted by the folder contract is a known VALID_TYPE."""
    for allowed in FOLDER_TYPE_CONTRACT.values():
        assert allowed <= VALID_TYPES
    assert FOLDER_TYPE_CONTRACT["people"] == {"entity"}
    assert RAW_SUBDIRS == {"articles", "papers", "transcripts", "assets"}
    assert REQUIRED_COMMON_FIELDS == (
        "title",
        "type",
        "created",
        "updated",
        "source",
        "tags",
    )


def test_type_enumeration_is_canonical_ordering_of_valid_types() -> None:
    """TYPE_ENUMERATION (used by the classify prompt) covers VALID_TYPES exactly (#19).

    The classify prompt derives its "type (one of ...)" list from this tuple, so it must
    enumerate every legal type once with no extras -- otherwise the prompt and the
    enforcement gate would drift.
    """
    assert set(TYPE_ENUMERATION) == VALID_TYPES
    assert len(TYPE_ENUMERATION) == len(VALID_TYPES)  # no duplicates
    # Knowledge types lead, life-admin types follow (a stable, human-readable order).
    knowledge_prefix = TYPE_ENUMERATION[: len(KNOWLEDGE_TYPES)]
    assert set(knowledge_prefix) == KNOWLEDGE_TYPES


def test_folder_dir_tuples_partition_the_folder_contract() -> None:
    """KNOWLEDGE_DIRS + LIFE_ADMIN_DIRS partition the folder contract keys (#19).

    These canonical folder lists are derived from here by :mod:`thoth.lint` and
    :mod:`thoth.summary`; together they must be exactly the top-level folders the
    contract governs, with no overlap, so a folder is added in one place only.
    """
    assert set(KNOWLEDGE_DIRS).isdisjoint(LIFE_ADMIN_DIRS)
    assert set(KNOWLEDGE_DIRS) | set(LIFE_ADMIN_DIRS) == set(FOLDER_TYPE_CONTRACT)
    # Each knowledge folder admits only knowledge types (people admits the 'entity'
    # knowledge type but scans as life-admin, so it lives in LIFE_ADMIN_DIRS).
    for folder in KNOWLEDGE_DIRS:
        assert FOLDER_TYPE_CONTRACT[folder] <= KNOWLEDGE_TYPES


# --- slugify (the one shared slug builder, issue #10) ------------------------------


def test_slugify_transliterates_and_caps() -> None:
    """slugify transliterates unicode and applies the word/length caps (#10)."""
    assert slugify("café notes") == "cafe-notes"
    assert slugify("naïve Bayes") == "naive-bayes"
    long = slugify("one two three four five six seven eight nine ten eleven")
    assert len(long.split("-")) <= 8


def test_slugify_fallback_for_empty_and_symbols() -> None:
    """An input with no slug-able characters returns the fallback word (#10)."""
    assert slugify("") == "untitled"
    assert slugify("   ", fallback="query") == "query"
    assert slugify("!!!", fallback="query") == "query"


@pytest.mark.parametrize(
    "text",
    ["café", "naïve Bayes", "Hello, World!", "日本語", "", "!!!", "Trailing---"],
)
def test_slugify_result_always_validates(text: str) -> None:
    """Every slugify output is non-empty and accepted by Vault.validate_slug (#10)."""
    slug = slugify(text)
    assert SLUG_RE.fullmatch(slug)
    assert Vault.validate_slug(slug) == slug


# --- path confinement (the security core) ------------------------------------------


def test_resolve_happy_path(vault: Vault) -> None:
    """A clean relative path resolves to an absolute path inside the root."""
    resolved = vault.resolve("entities/foo.md")
    assert resolved == vault.root / "entities" / "foo.md"
    assert resolved.is_absolute()
    assert vault.root in resolved.parents


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "/entities/x.md"],
    ids=["absolute", "leading-slash"],
)
def test_resolve_rejects_absolute(vault: Vault, bad: str) -> None:
    """Absolute and leading-slash paths are rejected."""
    with pytest.raises(PathConfinementError):
        vault.resolve(bad)


@pytest.mark.parametrize(
    "bad",
    ["entities/../../etc/passwd", "../x.md", "a/../../b", "entities/../secret.md"],
    ids=["deep-escape", "leading-dotdot", "mid-escape", "single-dotdot"],
)
def test_resolve_rejects_dotdot(vault: Vault, bad: str) -> None:
    """Any '..' part is rejected even when it would normalise back inside."""
    with pytest.raises(PathConfinementError):
        vault.resolve(bad)


@pytest.mark.parametrize(
    "bad",
    ["", ".", "./foo.md", "entities/./foo.md"],
    ids=["empty", "dot", "leading-dot", "mid-dot"],
)
def test_resolve_rejects_empty_and_dot(vault: Vault, bad: str) -> None:
    """The empty string and any '.' current-dir part are rejected."""
    with pytest.raises(PathConfinementError):
        vault.resolve(bad)


def test_resolve_rejects_symlink_escape(vault: Vault) -> None:
    """A symlink pointing outside the vault is caught even without a '..' part."""
    outside = vault.root.parent / "outside"
    outside.mkdir()
    (vault.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathConfinementError):
        vault.resolve("link/secret.md")


def test_resolve_allows_inside_symlink(vault: Vault) -> None:
    """A symlink that stays inside the vault is allowed (no over-rejection)."""
    (vault.root / "inlink").symlink_to(
        vault.root / "entities", target_is_directory=True
    )
    resolved = vault.resolve("inlink/bar.md")
    assert vault.root.resolve() in resolved.resolve().parents


def test_is_inside(vault: Vault) -> None:
    """is_inside is True for a clean path and False for an escaping one."""
    assert vault.is_inside("entities/foo.md") is True
    assert vault.is_inside("../escape.md") is False
    assert vault.is_inside("") is False


# --- slug / asset / folder-type validation -----------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["program-motion-controller", "cap-theorem", "a", "a1", "foo-2026"],
)
def test_validate_slug_accepts(slug: str) -> None:
    """Lowercase hyphenated slugs are accepted and returned unchanged."""
    assert Vault.validate_slug(slug) == slug
    assert SLUG_RE.fullmatch(slug)


@pytest.mark.parametrize(
    "slug",
    ["Foo", "a b", "a/b", "a--b", "-lead", "trail-", "", "Foo-Bar", "a_b"],
    ids=[
        "uppercase",
        "space",
        "slash",
        "double-hyphen",
        "leading-hyphen",
        "trailing-hyphen",
        "empty",
        "mixed-case",
        "underscore",
    ],
)
def test_validate_slug_rejects(slug: str) -> None:
    """Uppercase, spaces, slashes, hyphen-runs, edge hyphens, empty -> SlugError."""
    with pytest.raises(SlugError):
        Vault.validate_slug(slug)


@pytest.mark.parametrize(
    "name",
    ["motor-control-diagram-e4a408.png", "a.png", "scan-2026.jpg", "x1.webp"],
)
def test_validate_asset_filename_accepts(name: str) -> None:
    """Valid '<slug>.<ext>' asset filenames are accepted."""
    assert Vault.validate_asset_filename(name) == name
    assert ASSET_SLUG_RE.fullmatch(name)


@pytest.mark.parametrize(
    "name",
    ["no-ext", "Bad.PNG", "a b.png", "foo.PNG", ".png", "foo.", "a--b.png"],
    ids=["no-ext", "upper-ext", "space", "upper-ext2", "no-slug", "no-ext2", "run"],
)
def test_validate_asset_filename_rejects(name: str) -> None:
    """Missing extension, uppercase, spaces, hyphen-runs -> SlugError."""
    with pytest.raises(SlugError):
        Vault.validate_asset_filename(name)


def test_validate_folder_type_ok(vault: Vault) -> None:
    """Allowed folder/type pairs pass silently."""
    Vault.validate_folder_type("entities", "entity")
    Vault.validate_folder_type("people", "entity")
    Vault.validate_folder_type("actions", "action")
    Vault.validate_folder_type("inbox", "inbox")


@pytest.mark.parametrize(
    ("folder", "page_type"),
    [
        ("entities", "action"),
        ("people", "action"),
        ("actions", "entity"),
        ("concepts", "entity"),
    ],
)
def test_validate_folder_type_mismatch(
    vault: Vault, folder: str, page_type: str
) -> None:
    """A type not permitted in a folder raises SchemaError."""
    with pytest.raises(SchemaError):
        Vault.validate_folder_type(folder, page_type)


def test_validate_folder_type_unknown_folder(vault: Vault) -> None:
    """An unknown top-level folder raises SchemaError."""
    with pytest.raises(SchemaError):
        Vault.validate_folder_type("raw", "entity")


# --- obsidian:// link (delegates to the canonical builder) -------------------------


def test_obsidian_uri_matches_spec(vault: Vault) -> None:
    """obsidian_uri encodes the path exactly per the SPEC Appendix table."""
    assert (
        vault.obsidian_uri("entities/exa-search.md")
        == "obsidian://open?vault=pkm-vault&file=entities%2Fexa-search.md"
    )


def test_obsidian_uri_confines_first(vault: Vault) -> None:
    """obsidian_uri runs confinement before encoding, so '..' is rejected."""
    with pytest.raises(PathConfinementError):
        vault.obsidian_uri("../x.md")


# --- read --------------------------------------------------------------------------


def test_read_page_round_trip(vault: Vault) -> None:
    """A written page reads back with matching frontmatter and body."""
    rel = vault.write_page(
        "entities", "foo", _valid_frontmatter(), "# Foo\n\nBody text."
    )
    page = vault.read_page(rel)
    assert isinstance(page, Page)
    assert page.path == "entities/foo.md"
    assert page.frontmatter["title"] == "Program Motion Controller"
    assert page.frontmatter["type"] == "entity"
    assert page.body.strip() == "# Foo\n\nBody text."


def test_read_page_missing_raises(vault: Vault) -> None:
    """Reading a non-existent page raises VaultError."""
    with pytest.raises(VaultError):
        vault.read_page("entities/nope.md")


def test_read_page_confines(vault: Vault) -> None:
    """read_page rejects an escaping path before any disk access."""
    with pytest.raises(PathConfinementError):
        vault.read_page("../../etc/passwd")


def test_page_exists(vault: Vault) -> None:
    """page_exists reflects whether a confined path is a file."""
    assert vault.page_exists("entities/foo.md") is False
    vault.write_page("entities", "foo", _valid_frontmatter(), "body")
    assert vault.page_exists("entities/foo.md") is True


def test_body_sha256_matches_hashlib(vault: Vault) -> None:
    """body_sha256 equals hashlib.sha256 of the UTF-8 body and is stable."""
    body = "Some body text\nwith two lines.\n"
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert vault.body_sha256(body) == expected
    assert vault.body_sha256(body) == vault.body_sha256(body)


# --- write_page --------------------------------------------------------------------


def test_write_page_stamps_dates(vault: Vault) -> None:
    """write_page stamps created and updated to the given date and returns the path."""
    fixed = date(2026, 5, 30)
    rel = vault.write_page("entities", "foo", _valid_frontmatter(), "body", today=fixed)
    assert rel == "entities/foo.md"
    page = vault.read_page(rel)
    assert page.frontmatter["created"] == fixed
    assert page.frontmatter["updated"] == fixed


def test_write_page_preserves_created_bumps_updated(vault: Vault) -> None:
    """On update the original created is preserved and updated is bumped."""
    created_day = date(2026, 5, 1)
    vault.write_page("entities", "foo", _valid_frontmatter(), "v1", today=created_day)
    updated_day = date(2026, 5, 30)
    vault.write_page("entities", "foo", _valid_frontmatter(), "v2", today=updated_day)
    page = vault.read_page("entities/foo.md")
    assert page.frontmatter["created"] == created_day
    assert page.frontmatter["updated"] == updated_day
    assert page.body.strip() == "v2"


def test_write_page_field_order_is_deterministic(vault: Vault) -> None:
    """Frontmatter is serialised in the assembled key order (not alphabetised)."""
    rel = vault.write_page(
        "entities", "foo", _valid_frontmatter(), "body", today=date(2026, 5, 30)
    )
    text = (vault.root / rel).read_text(encoding="utf-8")
    # title appears before type before source -> insertion order preserved.
    assert text.index("title:") < text.index("type:") < text.index("source:")


@pytest.mark.parametrize("missing_field", ["title", "type", "source", "tags"])
def test_write_page_missing_required_field(vault: Vault, missing_field: str) -> None:
    """A missing required common field raises SchemaError naming the field."""
    meta = _valid_frontmatter()
    del meta[missing_field]
    with pytest.raises(SchemaError, match=missing_field):
        vault.write_page("entities", "foo", meta, "body")


def test_write_page_rejects_bad_type(vault: Vault) -> None:
    """A type outside VALID_TYPES raises SchemaError (caught at folder check)."""
    with pytest.raises(SchemaError):
        vault.write_page("entities", "foo", _valid_frontmatter(type="bogus"), "body")


def test_write_page_rejects_folder_type_mismatch(vault: Vault) -> None:
    """A valid type in the wrong folder raises SchemaError."""
    with pytest.raises(SchemaError):
        vault.write_page("entities", "foo", _valid_frontmatter(type="action"), "body")


def test_write_page_rejects_bad_slug(vault: Vault) -> None:
    """An invalid slug raises SlugError before any disk write."""
    with pytest.raises(SlugError):
        vault.write_page("entities", "Bad Slug", _valid_frontmatter(), "body")
    assert list((vault.root / "entities").glob("*.md")) == []


def test_write_page_rejects_invalid_source(vault: Vault) -> None:
    """A source outside VALID_SOURCES raises SchemaError."""
    with pytest.raises(SchemaError, match="source"):
        vault.write_page("entities", "foo", _valid_frontmatter(source="email"), "body")


def test_write_page_non_string_type(vault: Vault) -> None:
    """A non-string type is rejected before the folder check."""
    with pytest.raises(SchemaError):
        vault.write_page("entities", "foo", _valid_frontmatter(type=123), "body")


# --- write_raw ---------------------------------------------------------------------


def test_write_raw_stamps_ingested_and_sha(vault: Vault) -> None:
    """write_raw writes under raw/<subdir>/ and stamps ingested + body sha256."""
    body = "Extracted article text."
    rel = vault.write_raw(
        "papers",
        "attention-is-all-you-need",
        {"source_url": "https://example.com/paper"},
        body,
        today=date(2026, 5, 30),
    )
    assert rel == "raw/papers/attention-is-all-you-need.md"
    page = vault.read_page(rel)
    assert page.frontmatter["ingested"] == date(2026, 5, 30)
    assert page.frontmatter["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert page.frontmatter["source_url"] == "https://example.com/paper"


def test_write_raw_rejects_assets(vault: Vault) -> None:
    """write_raw refuses the binary-only 'assets' subdir."""
    with pytest.raises(SchemaError, match="assets"):
        vault.write_raw("assets", "foo", {}, "body")


def test_write_raw_rejects_unknown_subdir(vault: Vault) -> None:
    """write_raw refuses a subdir not in RAW_SUBDIRS."""
    with pytest.raises(SchemaError):
        vault.write_raw("notes", "foo", {}, "body")


def test_write_raw_rejects_bad_slug(vault: Vault) -> None:
    """write_raw validates the slug."""
    with pytest.raises(SlugError):
        vault.write_raw("articles", "Bad Slug", {}, "body")


# --- save_asset --------------------------------------------------------------------


def test_save_asset_moves_binary(vault: Vault, tmp_path: Path) -> None:
    """save_asset moves binary bytes verbatim into raw/assets and returns the path."""
    payload = bytes(range(256))  # non-UTF-8 binary content
    src = tmp_path / "download.bin"
    src.write_bytes(payload)
    rel = vault.save_asset(src, "motor-control-diagram-e4a408.png")
    assert rel == "raw/assets/motor-control-diagram-e4a408.png"
    written = (vault.root / rel).read_bytes()
    assert written == payload  # intact, not base64
    assert not src.exists()  # moved, not copied


def test_save_asset_refuses_overwrite(vault: Vault, tmp_path: Path) -> None:
    """save_asset refuses to overwrite an existing asset."""
    (vault.root / "raw" / "assets" / "diagram-abc123.png").write_bytes(b"old")
    src = tmp_path / "new.bin"
    src.write_bytes(b"new")
    with pytest.raises(VaultError, match="overwrite"):
        vault.save_asset(src, "diagram-abc123.png")
    assert (vault.root / "raw" / "assets" / "diagram-abc123.png").read_bytes() == b"old"


def test_save_asset_rejects_bad_filename(vault: Vault, tmp_path: Path) -> None:
    """save_asset rejects an invalid asset filename."""
    src = tmp_path / "x.bin"
    src.write_bytes(b"x")
    with pytest.raises(SlugError):
        vault.save_asset(src, "Bad Name.PNG")


def test_save_asset_rejects_path_escape(vault: Vault, tmp_path: Path) -> None:
    """A path-traversal asset filename is rejected by the filename grammar."""
    src = tmp_path / "x.bin"
    src.write_bytes(b"x")
    with pytest.raises(SlugError):
        vault.save_asset(src, "../escape.png")


def test_save_asset_missing_source(vault: Vault, tmp_path: Path) -> None:
    """A missing source file raises VaultError."""
    with pytest.raises(VaultError):
        vault.save_asset(tmp_path / "nope.bin", "ok-name.png")


# --- remove_page -------------------------------------------------------------------


def test_remove_page_deletes_existing_and_is_idempotent(vault: Vault) -> None:
    """remove_page deletes a confined page (True) then no-ops if absent (False)."""
    rel = vault.write_page(
        "inbox",
        "hold-abc123",
        {"title": "Held", "type": "inbox", "source": "slack", "tags": ["inbox"]},
        "held body",
    )
    assert vault.page_exists(rel)
    assert vault.remove_page(rel) is True
    assert not vault.page_exists(rel)
    # A second removal of the now-missing page is a harmless no-op.
    assert vault.remove_page(rel) is False


def test_remove_page_rejects_path_escape(vault: Vault) -> None:
    """remove_page confines the path: an escaping path is rejected, never deleted."""
    with pytest.raises(PathConfinementError):
        vault.remove_page("../escape.md")


# --- asset idempotency helpers -----------------------------------------------------


def test_bytes_sha256_matches_hashlib() -> None:
    """bytes_sha256 equals hashlib over the same bytes (the asset idempotency key)."""
    payload = bytes(range(256))
    assert Vault.bytes_sha256(payload) == hashlib.sha256(payload).hexdigest()


def test_asset_exists_reports_presence(vault: Vault, tmp_path: Path) -> None:
    """asset_exists is False before a save and True after, for the same filename."""
    assert vault.asset_exists("diagram-abc123.png") is False
    src = tmp_path / "d.bin"
    src.write_bytes(b"bytes")
    vault.save_asset(src, "diagram-abc123.png")
    assert vault.asset_exists("diagram-abc123.png") is True


def test_asset_exists_validates_filename(vault: Vault) -> None:
    """asset_exists rejects a malformed/escaping asset filename (not 'absent')."""
    with pytest.raises(SlugError):
        vault.asset_exists("../escape.png")


def test_asset_sha256_returns_digest_of_bytes(vault: Vault, tmp_path: Path) -> None:
    """asset_sha256 returns the SHA-256 of the stored asset's bytes."""
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
    src = tmp_path / "img.bin"
    src.write_bytes(payload)
    vault.save_asset(src, "photo-aa11bb.png")
    assert vault.asset_sha256("photo-aa11bb.png") == hashlib.sha256(payload).hexdigest()


def test_asset_sha256_missing_raises(vault: Vault) -> None:
    """asset_sha256 raises VaultError for an absent asset."""
    with pytest.raises(VaultError, match="does not exist"):
        vault.asset_sha256("absent-000000.png")


# --- append_index ------------------------------------------------------------------


def test_append_index_inserts_sorted_and_deduped(vault: Vault) -> None:
    """append_index keeps entries sorted within a section and dedupes by wikilink."""
    vault.append_index("Entities", "zebra", "last alphabetically")
    vault.append_index("Entities", "alpha", "first alphabetically")
    # Idempotent: re-adding the same wikilink does not duplicate it.
    vault.append_index("Entities", "alpha", "first alphabetically")
    text = (vault.root / "index.md").read_text(encoding="utf-8")
    section = text.split("### Entities", 1)[1].split("###", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.startswith("- [[")]
    assert lines == [
        "- [[alpha]] - first alphabetically",
        "- [[zebra]] - last alphabetically",
    ]


def test_append_index_does_not_disturb_other_sections(vault: Vault) -> None:
    """Adding under one heading leaves the other headings intact."""
    vault.append_index("People", "jane-doe", "collaborator")
    text = (vault.root / "index.md").read_text(encoding="utf-8")
    for heading in ("### Entities", "### Concepts", "### Comparisons", "### Queries"):
        assert heading in text
    assert "- [[jane-doe]] - collaborator" in text


def test_append_index_unknown_section(vault: Vault) -> None:
    """An unknown catalog section raises SchemaError."""
    with pytest.raises(SchemaError):
        vault.append_index("Widgets", "foo", "bar")


def test_append_index_round_trips_as_markdown(vault: Vault) -> None:
    """index.md remains parseable and the new entry survives a second add."""
    vault.append_index("Concepts", "cap-theorem", "consistency trade-offs")
    vault.append_index("Concepts", "raft", "consensus")
    text = (vault.root / "index.md").read_text(encoding="utf-8")
    assert text.count("- [[cap-theorem]]") == 1
    assert text.count("- [[raft]]") == 1


# --- append_log --------------------------------------------------------------------


def test_append_log_appends_dated_block(vault: Vault) -> None:
    """append_log appends a dated block listing the touched files."""
    vault.append_log(
        "ingest", "new article", ["raw/articles/foo.md", "entities/foo.md"]
    )
    text = (vault.root / "log.md").read_text(encoding="utf-8")
    today = date.today().isoformat()
    assert f"## [{today}] ingest | new article" in text
    assert "- raw/articles/foo.md" in text
    assert "- entities/foo.md" in text


def test_append_log_accumulates(vault: Vault) -> None:
    """Multiple append_log calls accumulate; the seed block is preserved."""
    vault.append_log("create", "first", ["a.md"])
    vault.append_log("update", "second", ["b.md"])
    text = (vault.root / "log.md").read_text(encoding="utf-8")
    assert "Vault initialized" in text  # seed preserved
    assert text.index("first") < text.index("second")


def test_append_log_unknown_action(vault: Vault) -> None:
    """An action outside the allowed set raises SchemaError."""
    with pytest.raises(SchemaError):
        vault.append_log("explode", "subject", [])


# --- embed_asset_markdown ----------------------------------------------------------


def test_embed_asset_markdown(vault: Vault) -> None:
    """embed_asset_markdown returns the validated Obsidian embed string."""
    assert (
        vault.embed_asset_markdown("motor-control-diagram-e4a408.png")
        == "![[motor-control-diagram-e4a408.png]]"
    )


def test_embed_asset_markdown_validates(vault: Vault) -> None:
    """embed_asset_markdown rejects an invalid filename."""
    with pytest.raises(SlugError):
        vault.embed_asset_markdown("Bad Name.PNG")


# --- secret redaction --------------------------------------------------------------


def test_redact_secrets_masks_tokens() -> None:
    """Provider keys, AWS ids, Bearer headers, and long blobs are masked."""
    blob_hex = "deadbeef" * 5  # 40 hex chars
    text = (
        f"key is {FAKE_OPENAI} and {FAKE_GITHUB}\n"
        f"aws {FAKE_AWS}\n"
        f"Authorization: Bearer {FAKE_BEARER_TOKEN}\n"
        f"digest {blob_hex}\n"
        f"api_key={FAKE_BEARER_TOKEN}"
    )
    redacted = redact_secrets(text)
    for secret in (FAKE_OPENAI, FAKE_GITHUB, FAKE_AWS, blob_hex):
        assert secret not in redacted
    assert "Bearer " + FAKE_BEARER_TOKEN not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_leaves_prose_untouched() -> None:
    """Ordinary prose and short words are not masked."""
    prose = "The quick brown fox jumps over the lazy dog. Cost was 42 dollars."
    assert redact_secrets(prose) == prose


def test_redact_secrets_never_raises_on_non_string() -> None:
    """redact_secrets tolerates a non-string input (returns it unchanged)."""
    assert redact_secrets(123) == 123  # type: ignore[arg-type]


def test_write_page_redacts_body(vault: Vault) -> None:
    """A secret embedded in the body is redacted before it lands on disk."""
    body = f"Here is my key {FAKE_OPENAI} please keep it safe."
    vault.write_page("entities", "foo", _valid_frontmatter(), body)
    page = vault.read_page("entities/foo.md")
    assert FAKE_OPENAI not in page.body
    assert "[REDACTED]" in page.body


def test_write_page_redacts_frontmatter_string(vault: Vault) -> None:
    """A secret in a string frontmatter value is redacted; dates are preserved."""
    meta = _valid_frontmatter(aliases=[f"token {FAKE_GITHUB}"])
    vault.write_page("entities", "foo", meta, "body", today=date(2026, 5, 30))
    page = vault.read_page("entities/foo.md")
    aliases = page.frontmatter["aliases"]
    assert isinstance(aliases, list)
    assert FAKE_GITHUB not in aliases[0]
    assert page.frontmatter["created"] == date(2026, 5, 30)  # non-string untouched


def test_write_raw_redacts_body_and_hashes_redacted(vault: Vault) -> None:
    """write_raw redacts the body and stores the sha256 of the redacted text."""
    body = f"leaked {FAKE_AWS} here"
    rel = vault.write_raw("articles", "leak", {}, body, today=date(2026, 5, 30))
    page = vault.read_page(rel)
    assert FAKE_AWS not in page.body
    # The stored sha256 is over the redacted body, matching what is on disk.
    assert page.frontmatter["sha256"] == vault.body_sha256(page.body)


# --- config seam -------------------------------------------------------------------


def test_root_equals_config_vault_path(tmp_path: Path) -> None:
    """Vault.root is exactly the resolved config vault_path."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_vault(root)
    config: Config = load_config({"PKM_VAULT": str(root)})
    assert Vault(config).root == config.vault_path


def test_written_file_is_loadable_by_frontmatter(vault: Vault) -> None:
    """The on-disk format is valid frontmatter (independent parser confirms)."""
    vault.write_page(
        "concepts",
        "raft",
        _valid_frontmatter(type="concept", title="Raft"),
        "# Raft\n\nConsensus algorithm.",
        today=date(2026, 5, 30),
    )
    raw = (vault.root / "concepts" / "raft.md").read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    assert post.metadata["title"] == "Raft"
    assert post.content.strip() == "# Raft\n\nConsensus algorithm."
    assert raw.endswith("\n")  # always newline-terminated
