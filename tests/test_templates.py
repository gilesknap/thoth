"""Tests for :mod:`thoth.templates`.

No external boundary: the module is a pure read of package data via
:mod:`importlib.resources`, so every test reads the bundled ``.md``/``.base``
files for real and asserts on their parsed structure and on the accessor's
name-confinement. The data directory ``src/thoth/templates/`` is not a Python
package, so it is reached as a resource under the importable ``thoth`` package;
these tests verify the accessor surfaces exactly the nine shipped templates and
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


def test_actions_base_has_imminent_view_first() -> None:
    """``actions.base`` leads with an 'Imminent' view (issue #62 dashboard).

    The Imminent view drives the dashboard's expanded red lead callout, so it is the
    first view in the file (the embed pins it by name, but ordering keeps it the
    default if the name pin is ever dropped).
    """
    data: Any = yaml.safe_load(base_text("actions"))
    assert data["views"][0]["name"] == "Imminent"


def test_actions_base_imminent_filters_overdue_and_due_soon() -> None:
    """'Imminent' filters open actions overdue or due within two days (issue #62)."""
    data: Any = yaml.safe_load(base_text("actions"))
    imminent = next(v for v in data["views"] if v["name"] == "Imminent")
    conditions = imminent["filters"]["and"]
    # Open only: a done/completed/cancelled action is never imminent.
    assert 'status != "done"' in conditions
    # Must carry a due date that is on or before now + 2 days; an overdue action
    # (due_date already < now) therefore matches too.
    assert 'due_date != ""' in conditions
    assert 'due_date <= now() + "2 days"' in conditions


def test_home_base_has_nested_not_inside_and() -> None:
    """``home.base`` nests a ``not:`` object inside its view's ``and:`` list."""
    data: Any = yaml.safe_load(base_text("home"))
    recent = data["views"][0]
    and_list = recent["filters"]["and"]
    assert any(isinstance(c, dict) and "not" in c for c in and_list), (
        "home.base recent view must nest a not: inside the and: list"
    )


def test_actions_base_has_a_media_consume_view() -> None:
    """``actions.base`` carries the media-consume queue (ADR 0005: media is action)."""
    data: Any = yaml.safe_load(base_text("actions"))
    media_view = next(v for v in data["views"] if "Media" in v["name"])
    and_list = media_view["filters"]["and"]
    assert 'tags.contains("media")' in and_list
    assert 'status == "to_consume"' in and_list


def test_actions_base_media_views_split_by_consume_status() -> None:
    """The media views split the queue by status: to_consume / consuming / consumed.

    ADR 0005 folded the old ``media/`` folder into ``actions/`` (a media item is an
    ``action`` tagged ``media``); the dashboard must still surface consuming vs consumed
    so the backlog stays visible. Each media view is tag-scoped to ``media`` and pinned
    to exactly one of the three ``status`` values summary.py's media scan reads.
    """
    data: Any = yaml.safe_load(base_text("actions"))
    media_views = [v for v in data["views"] if "Media" in v["name"]]
    statuses: set[str] = set()
    for view in media_views:
        and_list = view["filters"]["and"]
        assert 'tags.contains("media")' in and_list, view["name"]
        status_conds = [
            c for c in and_list if isinstance(c, str) and c.startswith("status == ")
        ]
        assert len(status_conds) == 1, f"{view['name']}: one status condition"
        statuses.add(status_conds[0])
    assert statuses == {
        'status == "to_consume"',
        'status == "consuming"',
        'status == "consumed"',
    }


# --------------------------------------------------------------------------- #
# Spine files: frontmatter / structural anchors used downstream.
# --------------------------------------------------------------------------- #


def test_index_is_a_callout_dashboard() -> None:
    """``index.md`` leads with an expanded red Imminent callout (issue #62)."""
    text = template_text("index.md")
    # Imminent actions lead in an expanded ('+') danger (red) callout.
    assert "> [!danger]+" in text
    # Every other section is a collapsed ('-') colour-coded callout.
    for marker in (
        "> [!todo]-",
        "> [!example]-",
        "> [!tip]-",
        "> [!quote]-",
        "> [!warning]-",
        "> [!info]-",
    ):
        assert marker in text, marker
    # The danger callout is the first callout on the page.
    assert text.index("[!danger]+") < text.index("[!todo]-")
    # It embeds the actions Imminent view.
    assert "![[_bases/actions.base#Imminent]]" in text


def test_index_dashboard_embeds_resolve_to_real_base_views() -> None:
    """Every ``![[_bases/x.base#View]]`` embed names a view that exists (issue #62)."""
    import re

    text = template_text("index.md")
    embeds = re.findall(r"!\[\[_bases/([^#\]]+\.base)#([^\]]+)\]\]", text)
    assert embeds, "the dashboard must embed at least one named Base view"
    for base_name, view_name in embeds:
        name = base_name.removesuffix(".base")
        base = yaml.safe_load(base_text(name))
        view_names = {v["name"] for v in base["views"]}
        assert view_name in view_names, f"{base_name}#{view_name}"


def test_index_preserves_knowledge_catalog_machinery() -> None:
    """The dashboard keeps the catalog headings ``append_index`` / lint depend on."""
    text = template_text("index.md")
    # Vault.append_index writes catalog lines under these headings; lint's
    # index-completeness check reads them, so the seed must keep them.
    for heading in ("### Entities", "### Notes", "### Memories"):
        assert heading in text, heading
    assert "Total pages:" in text


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


def test_iter_templates_returns_all_nine_non_empty() -> None:
    """``iter_templates`` yields all 9 templates, spine first, each non-empty."""
    items = iter_templates()
    names = [name for name, _ in items]
    assert names == [
        "index.md",
        "SCHEMA.md",
        "log.md",
        "_bases/home.base",
        "_bases/actions.base",
        "_bases/memories.base",
        "_bases/inbox.base",
        "_bases/entities.base",
        "_bases/notes.base",
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
    assert len(iter_templates()) == 9


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


def test_schema_taxonomy_agrees_with_code_content_types() -> None:
    """SCHEMA.md's human-readable taxonomy lists every code content type (ADR 0005).

    vault.py is the single source of the page-type vocabulary; SCHEMA.md carries a
    human-readable copy for the lint tag audit. This guards the copy against drift:
    every content type in :data:`thoth.vault.TYPE_ENUMERATION` must appear in the
    shipped ``## Tag Taxonomy`` so a page type cannot be dropped from the schema while
    kept in the code (which would silently flag every page using it as an unknown tag).
    """
    lint = pytest.importorskip("thoth.lint")
    from thoth.vault import TYPE_ENUMERATION

    tags = lint.parse_taxonomy_tags(template_text("SCHEMA.md"))
    missing = set(TYPE_ENUMERATION) - tags
    assert not missing, f"page types absent from SCHEMA.md taxonomy: {sorted(missing)}"
