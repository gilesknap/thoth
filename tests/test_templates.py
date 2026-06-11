"""Tests for :mod:`thoth.templates`.

No external boundary: the module is a pure read of package data via
:mod:`importlib.resources`, so every test reads the bundled ``.md``/``.base``
files for real and asserts on their parsed structure and on the accessor's
name-confinement. The data directory ``src/thoth/templates/`` is not a Python
package, so it is reached as a resource under the importable ``thoth`` package;
these tests verify the accessor surfaces exactly the shipped templates and
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
    OBSIDIAN_NAMES,
    ROOT_NAMES,
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


def test_actions_views_have_work_personal_all_variants() -> None:
    """Imminent/Open/Media each ship the 3 view variants (ADR 0013 dashboard).

    The Work·Personal·All pattern: each action-backed dashboard section gets three
    view variants differing only in the ``personal`` clause, switched in place via
    the embed's view dropdown. The base view (the section's default) plus its two
    variants must all exist for the dropdown to offer them.
    """
    data: Any = yaml.safe_load(base_text("actions"))
    names = {v["name"] for v in data["views"]}
    assert {
        "Imminent",
        "Imminent · Personal",
        "Imminent · All",
        "Open",
        "Open · Personal",
        "Open · All",
        "Media",
        "Media · Work",
        "Media · Personal",
        "All Actions",
    } == names


def test_actions_open_excludes_media_imminent_does_not() -> None:
    """Open filters out kind: media; Imminent is kind-agnostic (a due film shows)."""
    data: Any = yaml.safe_load(base_text("actions"))
    open_view = next(v for v in data["views"] if v["name"] == "Open")
    assert 'kind != "media"' in open_view["filters"]["and"]
    imminent = next(v for v in data["views"] if v["name"] == "Imminent")
    assert not any("kind" in str(c) for c in imminent["filters"]["and"])


def test_actions_work_views_treat_missing_personal_as_work() -> None:
    """Work-default views filter ``personal != true`` so an unset value counts as
    work (Bases has no coalesce; ``== false`` would drop legacy pages)."""
    data: Any = yaml.safe_load(base_text("actions"))
    for name in ("Imminent", "Open", "Media · Work"):
        view = next(v for v in data["views"] if v["name"] == name)
        assert "personal != true" in view["filters"]["and"], name


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


def test_triage_base_excludes_archive_via_nested_not() -> None:
    """``triage.base`` nests a ``not: file.inFolder("_archive")`` in its and: list."""
    data: Any = yaml.safe_load(base_text("triage"))
    and_list = data["filters"]["and"]
    assert any(isinstance(c, dict) and "not" in c for c in and_list), (
        "triage.base must nest a not: inside the top-level and: list"
    )


def test_actions_base_media_views_filter_by_kind() -> None:
    """The media queue keys off ``kind == "media"`` and the open lifecycle.

    ADR 0013: a media item is an ``action`` with ``kind: media`` sharing the single
    status lifecycle, so every media view is kind-scoped and excludes the closed
    statuses (no more to_consume/consuming/consumed split).
    """
    data: Any = yaml.safe_load(base_text("actions"))
    media_views = [v for v in data["views"] if "Media" in v["name"]]
    assert media_views, "actions.base must ship media views"
    for view in media_views:
        and_list = view["filters"]["and"]
        assert 'kind == "media"' in and_list, view["name"]
        assert 'status != "done"' in and_list, view["name"]
        assert 'status != "cancelled"' in and_list, view["name"]
        assert not any("to_consume" in str(c) for c in and_list), view["name"]


def test_bases_never_filter_on_tags() -> None:
    """No view filters on tags: the view-critical facets are properties (ADR 0013).

    The original dashboard bug: filters used flat tags (``tags.contains("media")``)
    while the LLM applied faceted tags, so views were silently near-empty. Facets are
    now frontmatter properties, and tags are purely descriptive -- no base may filter
    on them.
    """
    for name in BASE_NAMES:
        assert "tags.contains" not in base_text(name), name


# --------------------------------------------------------------------------- #
# Spine files: frontmatter / structural anchors used downstream.
# --------------------------------------------------------------------------- #


def test_index_is_a_callout_dashboard() -> None:
    """``index.md`` is the 5-section attention dashboard (issue #62 / ADR 0013)."""
    text = template_text("index.md")
    # Imminent actions lead in an expanded ('+') danger (red) callout.
    assert "> [!danger]+" in text
    # The other four sections are collapsed ('-') colour-coded callouts.
    for marker in (
        "> [!warning]-",
        "> [!todo]-",
        "> [!example]-",
        "> [!info]-",
    ):
        assert marker in text, marker
    # The danger callout is the first callout on the page.
    assert text.index("[!danger]+") < text.index("[!warning]-")
    # It embeds the actions Imminent view.
    assert "![[_bases/actions.base#Imminent]]" in text
    # The reference layer is a link line, not an embedded section (ADR 0013).
    assert "[[_bases/reference.base|" in text


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


def test_index_is_static_no_catalog_or_page_count() -> None:
    """index.md is static (ADR 0008): just the title + dashboards, no catalog/count."""
    text = template_text("index.md")
    # The agent-maintained catalog and its machinery are gone: the per-page gloss now
    # lives in each page's own summary: frontmatter, so no code reads/writes index.md.
    assert "## Knowledge catalog" not in text
    assert "Total pages:" not in text
    assert "### Entities" not in text
    assert "Agents: read SCHEMA.md" not in text
    # What remains is the Home title and the live Bases dashboard embeds.
    assert "PKM Vault — Home" in text
    assert "![[_bases/" in text


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
    assert top == set(SPINE_NAMES) | set(ROOT_NAMES)
    assert bases == {f"{n}.base" for n in BASE_NAMES}
    # The public accessors echo the constants.
    assert base_names() == BASE_NAMES
    assert spine_names() == SPINE_NAMES


def test_base_text_equivalent_to_template_text() -> None:
    """``base_text(name)`` is ``template_text('_bases/<name>.base')``."""
    assert template_text("_bases/actions.base") == base_text("actions")
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


def test_iter_templates_returns_all_templates_non_empty() -> None:
    """``iter_templates`` yields every template, spine first, each non-empty."""
    items = iter_templates()
    names = [name for name, _ in items]
    assert names == [
        "index.md",
        "SCHEMA.md",
        "log.md",
        "_bases/actions.base",
        "_bases/reference.base",
        "_bases/triage.base",
        *OBSIDIAN_NAMES,
        *ROOT_NAMES,
    ]
    # The discovered .obsidian tail includes the snippet and the seeded config.
    assert ".obsidian/snippets/dashboard-full-width.css" in OBSIDIAN_NAMES
    assert ".obsidian/appearance.json" in OBSIDIAN_NAMES
    # The vault-root dotfiles seed git's merge + ignore rules.
    assert ROOT_NAMES == (".gitattributes", ".gitignore")
    for name, text in items:
        assert text.strip(), name
    # Each listed text round-trips through the single-name accessor.
    for name, text in items:
        assert template_text(name) == text


def test_iter_templates_returns_independent_list() -> None:
    """``iter_templates`` returns a fresh list each call (no shared mutation)."""
    first = iter_templates()
    first.clear()
    assert len(iter_templates()) == len(SPINE_NAMES) + len(BASE_NAMES) + len(
        OBSIDIAN_NAMES
    ) + len(ROOT_NAMES)


# --------------------------------------------------------------------------- #
# Round-trip with the lint taxonomy parser (skips if lint not yet present).
# --------------------------------------------------------------------------- #


def test_schema_round_trips_through_lint_taxonomy_parser() -> None:
    """The seed ``SCHEMA.md`` feeds lint's taxonomy parser the documented tags."""
    lint = pytest.importorskip("thoth.lint")
    tags = lint.parse_taxonomy_tags(template_text("SCHEMA.md"))
    assert isinstance(tags, set)
    # One representative tag from each seed category in SCHEMA.md.
    expected = {"concept", "controls", "person", "contested"}
    assert expected <= tags, sorted(tags)


def test_schema_taxonomy_excludes_promoted_facets() -> None:
    """The taxonomy carries no type/kind/personal tags (ADR 0013).

    Tags are purely descriptive now: the view-critical facets (the page ``type``,
    the action ``kind`` values, and ``personal``) are frontmatter properties, so
    listing them as taxonomy tags would invite the LLM to duplicate them -- the
    exact drift that broke the original dashboards.
    """
    lint = pytest.importorskip("thoth.lint")
    from thoth.vault import ACTION_KIND_VOCAB, TYPE_ENUMERATION

    tags = lint.parse_taxonomy_tags(template_text("SCHEMA.md"))
    promoted = set(TYPE_ENUMERATION) | set(ACTION_KIND_VOCAB) | {"personal"}
    leaked = promoted & tags
    assert not leaked, f"promoted facets leaked into the taxonomy: {sorted(leaked)}"
