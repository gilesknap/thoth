"""Read-only accessor over thoth's packaged vault-spine and dashboard templates.

This module ships the canonical *seed* of a thoth vault as package data under
``src/thoth/templates/`` and exposes it through :mod:`importlib.resources`. Later
phases can then read the spine and Bases definitions without re-deriving them. Those
phases are ``migrate.py``, which lays down a fresh vault, and the dashboards decision.
The data directory is **not** a Python subpackage. It has no ``__init__.py`` and holds
only ``.md`` and ``.base`` files, so pytest never imports it as a doctest module and
Sphinx autosummary never documents it. This module, ``templates.py``, is the importable
surface.

Two kinds of template ship:

* **Spine files** -- :data:`SPINE_NAMES` (``index.md``, ``SCHEMA.md``, ``log.md``):
  frontmatter plus Markdown. ``index.md`` is the Home landing page, a thin,
  OKF-compliant stub that links to the Dashboards page with a standard markdown link.
  The dashboards themselves live in ``_bases/index.md``, see :data:`BASE_DOC_NAMES`.
  ``SCHEMA.md`` carries the frontmatter contract and the ``## Tag Taxonomy`` section
  that :func:`thoth.lint.parse_taxonomy_tags` reads as its single source of truth.
  ``log.md`` is the append-only action log.
* **Bases dashboards** -- :data:`BASE_NAMES` (``actions``, ``personal``, ``media``,
  ``inbox``, ``recent``, ``reference``): YAML ``.base`` files under ``_bases/`` that the
  ``_bases/index.md`` Dashboards page embeds. Each base is one *class* of item, and its
  views differ only by date window (ADR 0014). ``actions`` holds open work todos and
  ``personal`` holds open personal todos, and each ships ``7 Days``, ``30 Days`` and
  ``All``. ``media`` is the consume queue for work and personal items, with a personal
  column. ``inbox`` is the unfiled holding queue. ``recent`` is vault-wide activity over
  ``7``, ``30`` and ``60 Days``. ``reference`` is the curated Notes, Entities and
  Memories layer. Every ``filters:`` block is an object keyed by exactly one of
  ``and:``, ``or:`` or ``not:``. A bare YAML list is a Bases parse error.

**Bases against Dataview is a VPS and Obsidian-time decision (SPEC section 15, open
item 2), so this module ships and documents BOTH.** The *v1 target is Bases*, if the
installed Obsidian build ships Bases and validates the ``.base`` filter and date syntax.
The date arithmetic ``due_date < now() + "7 days"`` is the part that must validate. If
it does not, fall back to **Dataview**: one ``dataview`` code block per view on the
relevant index or Home page. The canonical open-actions fallback, recorded here so
neither option is lost, is::

    ```dataview
    TABLE status, due_date, priority, kind
    FROM "actions"
    WHERE status != "done" AND status != "cancelled"
    SORT priority ASC, due_date ASC
    ```

A second fallback is status-only Bases filters with no date arithmetic, where the cron
daily briefing does all the date math from frontmatter. The packaged ``.base`` files
are the Bases v1 target, and this docstring is the Dataview fallback of record.

The accessor confines every lookup to the templates resource root. A name with a parent
(``..``) or absolute component raises :class:`TemplateError`, and so does any name that
does not resolve to a shipped file. The appliance LLM never reaches this module, which
is deterministic plumbing for vault provisioning.
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

#: Markdown landing pages shipped under ``_bases/``. The paths use forward slashes and
#: sit under the templates root, alongside the ``.base`` files. ``_bases/index.md`` is
#: the **Dashboards** page, which embeds every ``.base`` view with an Obsidian
#: ``![[…#View]]`` Bases embed. The page lives under ``_bases/``, which lint and the
#: capture walk both exempt as machinery, so those Bases embeds are an OKF exception
#: allowed *by location* rather than by special case. The root :data:`SPINE_NAMES`
#: ``index.md`` is a thin OKF-compliant stub that links here with a standard markdown
#: link (issue #191).
BASE_DOC_NAMES: tuple[str, ...] = ("_bases/index.md",)

#: The three vault-spine file names shipped as package data.
SPINE_NAMES: tuple[str, ...] = ("index.md", "SCHEMA.md", "log.md")

#: Vault-root dotfiles shipped with the spine, which :meth:`thoth.vault.Vault.seed`
#: seeds into the vault root. ``.gitattributes`` gives committed Markdown a
#: ``merge=union`` strategy, so concurrent appends from two devices both survive a
#: merge instead of conflicting. ``.gitignore`` keeps per-device Obsidian state
#: (``workspace.json``, caches, ``.trash``) and the desktop-only ``obsidian-git``
#: plugin out of the synced repo, because mobile cannot run that plugin.
ROOT_NAMES: tuple[str, ...] = (".gitattributes", ".gitignore")

#: Owning package whose ``templates`` data subdirectory holds the templates. The data
#: directory is deliberately NOT a package and has no ``__init__.py``, so the code
#: reaches it as a resource *under* ``thoth`` rather than importing it as
#: ``thoth.templates``.
_PACKAGE: str = "thoth"
#: Name of the data subdirectory under :data:`_PACKAGE`.
_DATA_DIR: str = "templates"


class TemplateError(Exception):
    """Raised when a requested template name is unknown or unreadable."""


def base_names() -> tuple[str, ...]:
    """Return the Bases dashboard names (no ``.base`` suffix)."""
    return BASE_NAMES


def spine_names() -> tuple[str, ...]:
    """Return the three vault-spine file names."""
    return SPINE_NAMES


def _root() -> Traversable:
    """Return the templates resource root as a :class:`Traversable`.

    The root is the ``templates`` data subdirectory *under* the importable ``thoth``
    package, because the data directory itself is not a package.
    """
    return resources.files(_PACKAGE).joinpath(_DATA_DIR)


def _resolve(name: str) -> Traversable:
    """Resolve a relative template ``name`` confined to the resource root.

    The function splits the name on ``/``. It rejects a name that is empty or absolute,
    or that holds a ``.``, ``..`` or backslash component, so a lookup can never escape
    the packaged ``thoth.templates`` directory. It returns the located
    :class:`Traversable`, or raises :class:`TemplateError` when no such file ships.
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
    """Discover every shipped ``.obsidian/`` config file, recursively.

    The function walks the packaged ``templates/.obsidian`` tree, so dropping a new
    Obsidian config file in seeds it into fresh vaults with no code change. It returns
    forward-slash paths under the templates root, each prefixed with ``.obsidian/`` and
    sorted for a deterministic seed order. The result is empty when no ``.obsidian``
    directory ships.
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
#: templates root, each prefixed ``.obsidian/``. :func:`_discover_obsidian_names` walks
#: the packaged ``templates/.obsidian`` tree, so dropping a new config file in seeds it
#: into fresh vaults with no code change. :meth:`thoth.vault.Vault.seed` writes each
#: file verbatim into ``<vault>/.obsidian/``, which gives a fresh vault thoth's plugin
#: set, theme choice, and the ``dashboard-full-width`` snippet. The shipped
#: ``appearance.json`` enables that snippet.
OBSIDIAN_NAMES: tuple[str, ...] = _discover_obsidian_names()


def template_text(name: str) -> str:
    """Return the UTF-8 text of a packaged template by relative name.

    ``name`` is a forward-slash path under the templates root, for example ``index.md``
    or ``_bases/home.base``. The function raises :class:`TemplateError` when the name is
    unknown, or when the name escapes the templates resource root through ``..`` or an
    absolute path.
    """
    resource = _resolve(name)
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - data is sane
        raise TemplateError(f"could not read template: {name!r}") from exc


def base_text(name: str) -> str:
    """Return the text of the ``_bases/<name>.base`` dashboard.

    ``name`` is a bare dashboard name from :data:`BASE_NAMES`, with no ``.base`` suffix.
    The function raises :class:`TemplateError` for an unknown dashboard.
    """
    return template_text(f"_bases/{name}.base")


def iter_templates() -> list[tuple[str, str]]:
    """Return ``(relative-name, text)`` for every packaged template.

    The result lists the three spine files, the ``_bases/*.base`` dashboards, the
    ``_bases/`` markdown landing pages (:data:`BASE_DOC_NAMES`), the ``.obsidian``
    config files and the vault-root dotfiles. Each name is paired with its UTF-8 text.
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
