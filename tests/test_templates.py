"""Tests for :mod:`thoth.templates`.

No external boundary: the module is a pure read of package data via
:mod:`importlib.resources`, so every test reads the bundled ``.md``/``.base``
files for real and asserts on their parsed structure and on the accessor's
name-confinement. The data directory ``src/thoth/templates/`` is not a Python
package, so it is reached as a resource under the importable ``thoth`` package;
these tests verify the accessor surfaces exactly the eight shipped templates and
rejects names that escape the resource root. The lint round-trip
(``parse_taxonomy_tags`` over the seed ``SCHEMA.md``) skips cleanly if
:mod:`thoth.lint` is not yet importable, keeping this module's tests independent
of its sibling.
"""

from __future__ import annotations

from typing import Any

import frontmatter
import pytest
import yaml

from thoth.templates import (
    BASE_NAMES,
    SPINE_NAMES,
    TemplateError,
    base_names,
    base_text,
    iter_templates,
    spine_names,
    template_text,
)

# --------------------------------------------------------------------------- #
# Bases ``.base`` dashboards: YAML structure and the critical filter syntax.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", BASE_NAMES)
def test_each_base_parses_with_one_filter_key_and_views(name: str) -> None:
    """Every dashboard is YAML; ``filters:`` keyed by one of and/or/not + views."""
    data: Any = yaml.safe_load(base_text(name))
    assert isinstance(data, dict), name
    top_filter = data["filters"]
    assert isinstance(top_filter, dict), f"{name}: filters must be an object"
    keys = [k for k in ("and", "or", "not") if k in top_filter]
    assert keys == [keys[0]] and len(keys) == 1, (
        f"{name}: filters must use exactly one of and/or/not, got {list(top_filter)}"
    )
    views = data.get("views")
    assert isinstance(views, list) and views, f"{name}: needs a non-empty views list"
    for view in views:
        assert view.get("type") == "table", f"{name}: every view is a table"
        assert view.get("name"), f"{name}: every view has a name"


def test_actions_due_soon_uses_date_arithmetic_string() -> None:
    """``actions.base`` 'Due Soon' carries the literal date-arithmetic condition."""
    data: Any = yaml.safe_load(base_text("actions"))
    due_soon = next(v for v in data["views"] if v["name"] == "Due Soon")
    conditions = due_soon["filters"]["and"]
    assert 'due_date < now() + "7 days"' in conditions


def test_home_base_has_nested_not_inside_and() -> None:
    """``home.base`` nests a ``not:`` object inside its view's ``and:`` list."""
    data: Any = yaml.safe_load(base_text("home"))
    recent = data["views"][0]
    and_list = recent["filters"]["and"]
    assert any(isinstance(c, dict) and "not" in c for c in and_list), (
        "home.base recent view must nest a not: inside the and: list"
    )


def test_media_base_has_an_or_view() -> None:
    """``media.base`` exposes an OR view across the active media states."""
    data: Any = yaml.safe_load(base_text("media"))
    in_progress = next(v for v in data["views"] if v["name"] == "In Progress or Done")
    or_list = in_progress["filters"]["or"]
    assert 'status == "consuming"' in or_list
    assert 'status == "consumed"' in or_list


# --------------------------------------------------------------------------- #
# Spine files: frontmatter / structural anchors used downstream.
# --------------------------------------------------------------------------- #


def test_index_md_frontmatter_is_summary_type() -> None:
    """``index.md`` is the Home page: frontmatter ``type`` is ``summary``."""
    post = frontmatter.loads(template_text("index.md"))
    assert post.metadata.get("type") == "summary"
    assert post.metadata.get("title") == "Home"


def test_schema_md_has_tag_taxonomy_section() -> None:
    """``SCHEMA.md`` ships the ``## Tag Taxonomy`` section lint reads from."""
    text = template_text("SCHEMA.md")
    assert "## Tag Taxonomy" in text
    assert text.startswith("# Vault Schema")


def test_log_md_starts_with_vault_log_heading() -> None:
    """``log.md`` is the append-only action log starting with its title."""
    text = template_text("log.md")
    assert text.startswith("# Vault Log")
    # The seed log already carries one dated ``## [`` block.
    assert "## [" in text


# --------------------------------------------------------------------------- #
# Accessor surface: names match the shipped files; equivalences; rejection.
# --------------------------------------------------------------------------- #


def test_names_match_shipped_files() -> None:
    """``base_names``/``spine_names`` exactly match the package-data contents."""
    from importlib import resources

    root = resources.files("thoth").joinpath("templates")
    top = {p.name for p in root.iterdir() if p.is_file()}
    bases = {p.name for p in root.joinpath("_bases").iterdir() if p.is_file()}
    assert top == set(SPINE_NAMES)
    assert bases == {f"{n}.base" for n in BASE_NAMES}
    # The public accessors echo the constants.
    assert base_names() == BASE_NAMES
    assert spine_names() == SPINE_NAMES


def test_base_text_equivalent_to_template_text() -> None:
    """``base_text(name)`` is ``template_text('_bases/<name>.base')``."""
    assert template_text("_bases/home.base") == base_text("home")
    for name in BASE_NAMES:
        assert base_text(name) == template_text(f"_bases/{name}.base")


@pytest.mark.parametrize(
    "bad",
    [
        "../config.py",
        "_bases/../config.py",
        "/etc/passwd",
        "..",
        ".",
        "",
        "nope.md",
        "_bases/missing.base",
        "_bases",  # a directory is not a readable template
    ],
)
def test_unknown_or_escaping_name_raises(bad: str) -> None:
    """Unknown names and path-traversal-ish names raise :class:`TemplateError`."""
    with pytest.raises(TemplateError):
        template_text(bad)


def test_base_text_unknown_dashboard_raises() -> None:
    """``base_text`` on an unknown dashboard name raises :class:`TemplateError`."""
    with pytest.raises(TemplateError):
        base_text("does-not-exist")


def test_iter_templates_returns_all_eight_non_empty() -> None:
    """``iter_templates`` yields all 8 templates, spine first, each non-empty."""
    items = iter_templates()
    names = [name for name, _ in items]
    assert names == [
        "index.md",
        "SCHEMA.md",
        "log.md",
        "_bases/home.base",
        "_bases/actions.base",
        "_bases/media.base",
        "_bases/memories.base",
        "_bases/inbox.base",
    ]
    for name, text in items:
        assert text.strip(), name
    # Each listed text round-trips through the single-name accessor.
    for name, text in items:
        assert template_text(name) == text


def test_iter_templates_returns_independent_list() -> None:
    """``iter_templates`` returns a fresh list each call (no shared mutation)."""
    first = iter_templates()
    first.clear()
    assert len(iter_templates()) == 8


# --------------------------------------------------------------------------- #
# Round-trip with the lint taxonomy parser (skips if lint not yet present).
# --------------------------------------------------------------------------- #


def test_schema_round_trips_through_lint_taxonomy_parser() -> None:
    """The seed ``SCHEMA.md`` feeds lint's taxonomy parser the documented tags."""
    lint = pytest.importorskip("thoth.lint")
    tags = lint.parse_taxonomy_tags(template_text("SCHEMA.md"))
    assert isinstance(tags, set)
    # One representative tag from each seed category in SCHEMA.md.
    expected = {"entity", "concept", "task", "media", "memory", "contested"}
    assert expected <= tags, sorted(tags)


def test_schema_taxonomy_agrees_with_code_knowledge_types() -> None:
    """SCHEMA.md's human-readable taxonomy lists every code page type (#19).

    vault.py is the single source of the page-type vocabulary; SCHEMA.md carries a
    human-readable copy for the lint tag audit. This guards the copy against drift:
    every canonical :data:`thoth.vault.KNOWLEDGE_TYPES` value must appear in the shipped
    ``## Tag Taxonomy`` so a page type cannot be dropped from the schema while kept in
    the code (which would silently flag every page using it as an unknown tag).
    """
    lint = pytest.importorskip("thoth.lint")
    from thoth.vault import KNOWLEDGE_TYPES

    tags = lint.parse_taxonomy_tags(template_text("SCHEMA.md"))
    missing = KNOWLEDGE_TYPES - tags
    assert not missing, f"page types absent from SCHEMA.md taxonomy: {sorted(missing)}"
