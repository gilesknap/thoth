"""Read-only accessor over thoth's packaged vault-spine and dashboard templates.

The canonical seed of a vault ships as package data and is exposed through
:mod:`importlib.resources`, so a fresh vault can be laid down without re-deriving the
spine and Bases definitions. The data directory is deliberately not a package, so pytest
never imports it as a doctest module and autosummary never documents it. Two kinds of
template ship:

* **Spine files** (:data:`SPINE_NAMES`) -- ``index.md`` is the Home landing page, an
  OKF-compliant stub linking to the Dashboards page; ``SCHEMA.md`` carries the
  frontmatter contract and the ``## Tag Taxonomy`` section
  :func:`thoth.lint.parse_taxonomy_tags` reads as its single source of truth; ``log.md``
  is the append-only action log.
* **Bases dashboards** (:data:`BASE_NAMES`) -- YAML ``.base`` files under ``_bases/``
  embedded by the ``_bases/index.md`` page. Each base is one class of item whose views
  differ only by date window (ADR 0014): ``actions`` and ``personal`` are open todos,
  ``media`` the consume queue, ``inbox`` the unfiled holding queue, ``recent``
  vault-wide activity, ``reference`` the curated layer. Every ``filters:`` block must be
  an object keyed by exactly one of ``and:``, ``or:`` or ``not:``, because a bare YAML
  list is a Bases parse error.

Bases against Dataview is an Obsidian-time decision (SPEC section 15), so both are
recorded here. Bases is the v1 target if the installed build ships it and the date
arithmetic validates. If it does not, fall back to a ``dataview`` code block per view.
The canonical open-actions fallback, recorded so neither option is lost, is::

    ```dataview
    TABLE status, due_date, priority
    FROM "actions"
    WHERE status != "done" AND status != "cancelled"
    SORT priority ASC, due_date ASC
    ```

A second fallback is status-only filters with the cron briefing doing all the date maths
from frontmatter. The packaged ``.base`` files are the v1 target and this docstring is
the Dataview fallback of record.

Every lookup is confined to the resource root, so a name with a parent or absolute
component, or one that resolves to no shipped file, raises. The appliance LLM never
reaches this module: it is deterministic plumbing for vault provisioning.
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable

__all__ = [
    "BASE_NAMES",
    "BASE_DOC_NAMES",
    "SPINE_NAMES",
    "OBSIDIAN_NAMES",
    "ROOT_NAMES",
    "TemplateError",
    "template_text",
    "base_text",
    "base_names",
    "spine_names",
    "iter_templates",
]

#: The Bases dashboard names (without the ``.base`` suffix), in the order
#: ``index.md`` embeds them.
BASE_NAMES: tuple[str, ...] = (
    "actions",
    "personal",
    "media",
    "inbox",
    "recent",
    "notes",
    "entities",
    "memories",
)

#: Markdown landing pages shipped under ``_bases/`` (forward-slash paths under the
#: templates root, alongside the ``.base`` files). ``_bases/index.md`` is the
#: **Dashboards** page that embeds every ``.base`` view via Obsidian ``![[…#View]]``
#: Bases embeds. It lives under ``_bases/`` -- machinery that lint and the capture
#: walk both exempt -- so those Bases embeds are an allowed OKF exception *by
#: location*, not by special-case. The root :data:`SPINE_NAMES` ``index.md`` is a thin
#: OKF-compliant stub that links here with a standard markdown link (issue #191).
BASE_DOC_NAMES: tuple[str, ...] = ("_bases/index.md",)

#: The three vault-spine file names shipped as package data.
SPINE_NAMES: tuple[str, ...] = ("index.md", "SCHEMA.md", "log.md")

#: Vault-root dotfiles shipped with the spine and seeded into the vault root by
#: :meth:`thoth.vault.Vault.seed`. ``.gitattributes`` gives committed Markdown a
#: ``merge=union`` strategy (so concurrent appends from two devices both survive a
#: merge instead of conflicting); ``.gitignore`` keeps per-device Obsidian state
#: (``workspace.json``, caches, ``.trash``) and the desktop-only ``obsidian-git``
#: plugin out of the synced repo (mobile cannot run it).
ROOT_NAMES: tuple[str, ...] = (".gitattributes", ".gitignore")

#: Owning package whose ``templates`` data subdirectory holds the templates. The
#: data directory is deliberately NOT a package (no ``__init__.py``), so it is
#: reached as a resource *under* ``thoth`` rather than imported as ``thoth.templates``.
_PACKAGE: str = "thoth"
#: Name of the data subdirectory under :data:`_PACKAGE`.
_DATA_DIR: str = "templates"


class TemplateError(Exception):
    """Raised when a requested template name is unknown or unreadable."""


def base_names() -> tuple[str, ...]:
    """Returns the Bases dashboard names, without the suffix."""
    return BASE_NAMES


def spine_names() -> tuple[str, ...]:
    """Returns the three vault-spine file names."""
    return SPINE_NAMES


def _root() -> Traversable:
    """Returns the templates resource root.

    Resolved as the data subdirectory under the importable package, because the data
    directory itself is not a package.
    """
    return resources.files(_PACKAGE).joinpath(_DATA_DIR)


def _resolve(name: str) -> Traversable:
    """Resolves a relative template name, confined to the resource root.

    The name is rejected when empty, absolute, or carrying a dot or backslash component,
    so a lookup can never escape the packaged directory.
    """
    if not name or name.startswith("/") or "\\" in name:
        raise TemplateError(f"invalid template name: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TemplateError(f"invalid template name: {name!r}")
    resource: Traversable = _root()
    for part in parts:
        resource = resource.joinpath(part)
    if not resource.is_file():
        raise TemplateError(f"unknown template: {name!r}")
    return resource


def _discover_obsidian_names() -> tuple[str, ...]:
    """Discovers every shipped Obsidian config file, recursively.

    Walking the packaged tree means a new config file is seeded into fresh vaults just
    by dropping it in, with no code change. The result is sorted for a deterministic
    seed order, and empty when no config directory ships.
    """
    root = _root().joinpath(".obsidian")
    if not root.is_dir():
        return ()
    names: list[str] = []
    stack: list[tuple[str, Traversable]] = [(".obsidian", root)]
    while stack:
        prefix, node = stack.pop()
        for child in node.iterdir():
            rel = f"{prefix}/{child.name}"
            if child.is_dir():
                stack.append((rel, child))
            else:
                names.append(rel)
    return tuple(sorted(names))


#: Obsidian-config files shipped with the spine, as forward-slash paths under the
#: templates root (each prefixed ``.obsidian/``). Discovered by walking the
#: packaged ``templates/.obsidian`` tree, so dropping a new config file in seeds
#: it into fresh vaults with no code change. :meth:`thoth.vault.Vault.seed` writes
#: each verbatim into ``<vault>/.obsidian/``, giving a fresh vault thoth's plugin
#: set, theme choice, and the ``dashboard-full-width`` snippet (enabled via the
#: shipped ``appearance.json``).
OBSIDIAN_NAMES: tuple[str, ...] = _discover_obsidian_names()


def template_text(name: str) -> str:
    """Returns the UTF-8 text of a packaged template by relative name.

    The name is a forward-slash path under the templates root. It raises when the name
    is unknown or would escape that root.
    """
    resource = _resolve(name)
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - data is sane
        raise TemplateError(f"could not read template: {name!r}") from exc


def base_text(name: str) -> str:
    """Returns the text of one Bases dashboard.

    The name is a bare dashboard name from :data:`BASE_NAMES`, without the suffix, and
    an unknown dashboard raises.
    """
    return template_text(f"_bases/{name}.base")


def iter_templates() -> list[tuple[str, str]]:
    """Returns the name and text of every packaged template.

    Covers the spine files, the Bases dashboards and their landing pages, the Obsidian
    config files, and the vault-root dotfiles.
    """
    items: list[tuple[str, str]] = []
    for spine in SPINE_NAMES:
        items.append((spine, template_text(spine)))
    for base in BASE_NAMES:
        rel = f"_bases/{base}.base"
        items.append((rel, template_text(rel)))
    for doc in BASE_DOC_NAMES:
        items.append((doc, template_text(doc)))
    for obsidian in OBSIDIAN_NAMES:
        items.append((obsidian, template_text(obsidian)))
    for root in ROOT_NAMES:
        items.append((root, template_text(root)))
    return items
