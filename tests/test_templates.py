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


def test_each_base_is_one_class_with_date_window_views() -> None:
    """Each base is one item class; its views differ only by date window (ADR 0014).

    The old Work·Personal·All triplication is gone. ``actions`` (work) and ``personal``
    each ship 7 / 30 / All day windows; ``media`` (the consume queue, work + personal)
    the same; ``recent`` ships 7 / 30 / 60; ``inbox`` is a single unfiled view.
    """
    for base in ("actions", "personal", "media"):
        data: Any = yaml.safe_load(base_text(base))
        assert {v["name"] for v in data["views"]} == {"7 Days", "30 Days", "All"}, base
    recent: Any = yaml.safe_load(base_text("recent"))
    assert {v["name"] for v in recent["views"]} == {"7 Days", "30 Days", "60 Days"}
    inbox: Any = yaml.safe_load(base_text("inbox"))
    assert {v["name"] for v in inbox["views"]} == {"Inbox"}


def test_class_split_lives_in_each_base_filter_not_per_view() -> None:
    """Item class (work / personal / media) is the base-level filter, so views differ
    by date only. The class is read off the ``type`` property (ADR 0015), not the
    folder: work todos are ``type == "action"`` + ``personal != true`` (unset counts as
    work -- Bases has no coalesce); personal todos are ``type == "action"`` +
    ``personal == true``; media is ``type == "media"``. The closed statuses are excluded
    at the base level too."""
    actions = yaml.safe_load(base_text("actions"))["filters"]["and"]
    assert 'type == "action"' in actions and "personal != true" in actions
    assert 'status != "done"' in actions and 'status != "cancelled"' in actions
    personal = yaml.safe_load(base_text("personal"))["filters"]["and"]
    assert 'type == "action"' in personal and "personal == true" in personal
    media = yaml.safe_load(base_text("media"))["filters"]["and"]
    assert 'type == "media"' in media
    assert not any("personal" in str(c) for c in media), "media spans both personal"


def test_date_window_views_always_include_undated_and_sort_expired_first() -> None:
    """Every dated window keeps undated items and orders overdue -> undated -> upcoming.

    The lost-action bug: an urgent todo with no ``due_date`` fell out of every dated
    view. A *missing* ``due_date`` is matched with ``due_date.isEmpty()`` (Obsidian
    Bases; ``== ""`` does NOT match an absent property -- the live failure that hid
    undated todos from the bounded windows), so the windows ``or:`` it in and undated
    items always show (a nag to add a date). The ``date_bucket`` formula then sorts
    overdue (0) first, then upcoming (1), then undated (2) last -- real deadlines lead
    and undated items trail as a backlog, but nothing hides.
    """
    # The todo bases (action/personal) sort by due date; media sorts by recency
    # instead (see test_media_sorts_by_priority_then_recency), so it is excluded here.
    for base in ("actions", "personal"):
        data: Any = yaml.safe_load(base_text(base))
        # The bucket formula is defined and is the primary sort key on every view.
        assert "date_bucket" in data["formulas"], base
        for view in data["views"]:
            assert view["sort"][0]["property"] == "formula.date_bucket", view["name"]
        # The bounded windows keep undated items via a ``.isEmpty()`` or-arm (an absent
        # due_date, which ``== ""`` would miss).
        for name in ("7 Days", "30 Days"):
            view = next(v for v in data["views"] if v["name"] == name)
            assert "due_date.isEmpty()" in view["filters"]["or"], (base, name)
        # The bucket formula also uses ``.isEmpty()`` so undated items bucket correctly.
        assert "due_date.isEmpty()" in data["formulas"]["date_bucket"], base
        # The All window has no date filter at all (every open item of the class).
        all_view = next(v for v in data["views"] if v["name"] == "All")
        assert "filters" not in all_view, base


def test_media_base_shows_a_personal_column() -> None:
    """Media spans work + personal, so it carries a personal column to tell them apart
    (the distinction is minor for leisure media, but visible)."""
    media: Any = yaml.safe_load(base_text("media"))
    assert "personal" in media["properties"]
    for view in media["views"]:
        assert "personal" in view["order"], view["name"]


def test_media_sorts_by_priority_then_recency_with_created_column() -> None:
    """The consume queue surfaces recent additions: it sorts by priority, then by
    ``created`` descending (most-recently-added first), and shows an Added column.

    Media is mostly undated, so the due-date bucketing the todo bases use is moot here;
    recency is the useful signal instead.
    """
    media: Any = yaml.safe_load(base_text("media"))
    assert "created" in media["properties"]
    for view in media["views"]:
        assert "created" in view["order"], view["name"]
        assert view["sort"][0] == {"property": "formula.prio_rank", "direction": "ASC"}
        assert view["sort"][1] == {"property": "created", "direction": "DESC"}


def test_recent_base_excludes_archive_via_nested_not() -> None:
    """``recent.base`` nests a ``not: file.inFolder("_archive")`` in its and: list."""
    data: Any = yaml.safe_load(base_text("recent"))
    and_list = data["filters"]["and"]
    assert any(isinstance(c, dict) and "not" in c for c in and_list), (
        "recent.base must nest a not: inside the top-level and: list"
    )


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
    """``index.md`` is the attention dashboard (issue #62 / ADR 0014).

    One callout per item-class base, embedding a default date window. The two todo
    lists (work, personal) lead expanded ('+'); media / inbox / recent are collapsed.
    """
    text = template_text("index.md")
    assert "> [!danger]+" in text  # Work todos lead in an expanded red callout.
    assert "> [!tip]+" in text  # Personal todos expanded.
    # The remaining sections are collapsed ('-') colour-coded callouts.
    for marker in ("> [!example]-", "> [!warning]-", "> [!info]-"):
        assert marker in text, marker
    # The work callout is the first callout on the page.
    assert text.index("[!danger]+") < text.index("[!example]-")
    # Each item-class base is embedded by a default view.
    assert "![[_bases/actions.base#7 Days]]" in text
    assert "![[_bases/personal.base#7 Days]]" in text
    assert "![[_bases/media.base#All]]" in text
    assert "![[_bases/inbox.base#Inbox]]" in text
    assert "![[_bases/recent.base#7 Days]]" in text
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
        "_bases/personal.base",
        "_bases/media.base",
        "_bases/inbox.base",
        "_bases/recent.base",
        "_bases/reference.base",
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
    """The taxonomy carries no type/personal tags (ADR 0013, 0015).

    Tags are purely descriptive now: the view-critical facets (the page ``type`` --
    which since ADR 0015 subsumes the old action ``kind``, including ``media`` -- and
    ``personal``) are frontmatter properties, so listing them as taxonomy tags would
    invite the LLM to duplicate them -- the exact drift that broke the original
    dashboards.
    """
    lint = pytest.importorskip("thoth.lint")
    from thoth.vault import TYPE_ENUMERATION

    tags = lint.parse_taxonomy_tags(template_text("SCHEMA.md"))
    promoted = set(TYPE_ENUMERATION) | {"personal"}
    leaked = promoted & tags
    assert not leaked, f"promoted facets leaked into the taxonomy: {sorted(leaked)}"
