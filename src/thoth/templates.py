"""Read-only accessor over thoth's packaged vault-spine and dashboard templates.

This module ships the canonical *seed* of a thoth vault as package data under
``src/thoth/templates/``, exposed through :mod:`importlib.resources`, so a later phase
such as ``migrate.py`` can read the spine and Bases definitions without re-deriving
them. The data directory is **not** a Python subpackage: it has no ``__init__.py`` and
holds only ``.md`` and ``.base`` files, so pytest never imports it as a doctest module
and Sphinx autosummary never documents it. This module is the importable surface.

Two kinds of template ship:

* **Spine files**, :data:`SPINE_NAMES`, which are frontmatter and Markdown: ``index.md``
  the Home landing page, ``SCHEMA.md`` the frontmatter contract plus the
  ``## Tag Taxonomy`` section :func:`thoth.lint.parse_taxonomy_tags` reads as its single
  source of truth, and ``log.md`` the append-only action log.
* **Bases dashboards**, :data:`BASE_NAMES`, which are YAML ``.base`` files under
  ``_bases/`` embedded by the ``_bases/index.md`` Dashboards page. Each base is one
  *class* of item whose views differ only by date window (ADR 0014): ``actions`` and
  ``personal`` hold open work and personal todos over ``7 Days``, ``30 Days`` and
  ``All``, ``media`` is the consume queue with a personal column, ``inbox`` the unfiled
  holding queue, ``recent`` vault-wide activity at ``7``, ``30`` and ``60 Days``, and
  ``reference`` the curated Notes, Entities and Memories layer. Every ``filters:`` block
is an object keyed by exactly one of ``and:``, ``or:`` or ``not:``, because a bare YAML
  list is a Bases parse error.

**Bases vs Dataview is a VPS and Obsidian-time decision (SPEC section 15, open item
2), so this module ships and documents BOTH.** The *v1 target is Bases* if the
installed Obsidian build ships Bases, and the ``.base`` filter and date syntax
validates, in particular the date arithmetic ``due_date < now() + "7 days"``. If it
does not, fall back to **Dataview**, a ``dataview`` code block per view on the
relevant index or Home page. The canonical open-actions fallback, recorded here so
neither option is lost, is::

    ```dataview
    TABLE status, due_date, priority, kind
    FROM "actions"
    WHERE status != "done" AND status != "cancelled"
    SORT priority ASC, due_date ASC
    ```

A second fallback is status-only Bases filters, with no date arithmetic, and the cron
daily briefing doing all the date maths from frontmatter. The packaged ``.base`` files
are the Bases v1 target, and this docstring is the Dataview fallback of record.

The accessor confines every lookup to the templates resource root. A name with a parent
(``..``) or absolute component, or any name that does not resolve to a shipped file,
raises :class:`TemplateError`. The appliance LLM never reaches this module, which is
deterministic plumbing for vault provisioning.
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

#: Markdown landing pages shipped under ``_bases/``, as forward-slash paths under the
#: templates root, alongside the ``.base`` files. ``_bases/index.md`` is the
#: **Dashboards** page that embeds every ``.base`` view through an Obsidian
#: ``![[…#View]]`` Bases embed. It lives under ``_bases/``, machinery that lint and the
#: capture walk both exempt, so those Bases embeds are an allowed OKF exception *by
#: location* rather than by special case. The root :data:`SPINE_NAMES` ``index.md`` is a
#: thin OKF-compliant stub that links here with a standard markdown link (issue #191).
BASE_DOC_NAMES: tuple[str, ...] = ("_bases/index.md",)

#: The three vault-spine file names shipped as package data.
SPINE_NAMES: tuple[str, ...] = ("index.md", "SCHEMA.md", "log.md")

#: Vault-root dotfiles shipped with the spine, which :meth:`thoth.vault.Vault.seed`
#: writes into the vault root. ``.gitattributes`` gives committed Markdown a
#: ``merge=union`` strategy, so concurrent appends from two devices both survive a merge
#: rather than conflict. ``.gitignore`` keeps per-device Obsidian state out of the
#: synced repo, such as ``workspace.json``, caches and ``.trash``, along with the
#: desktop-only ``obsidian-git`` plugin, which mobile cannot run.
ROOT_NAMES: tuple[str, ...] = (".gitattributes", ".gitignore")

#: Owning package whose ``templates`` data subdirectory holds the templates. That data
#: directory is deliberately NOT a package, having no ``__init__.py``, so it is reached
#: as a resource *under* ``thoth`` rather than imported as ``thoth.templates``.
_PACKAGE: str = "thoth"
#: Name of the data subdirectory under :data:`_PACKAGE`.
_DATA_DIR: str = "templates"


class TemplateError(Exception):
    """Raised when a requested template name is unknown or unreadable."""


def base_names() -> tuple[str, ...]:
    """Return the Bases dashboard names, with no ``.base`` suffix."""
    return BASE_NAMES


def spine_names() -> tuple[str, ...]:
    """Return the three vault-spine file names."""
    return SPINE_NAMES


def _root() -> Traversable:
    """Return the templates resource root as a :class:`Traversable`.

    Resolved as the ``templates`` data subdirectory *under* the importable ``thoth``
    package, because the data directory itself is not a package.
    """
    return resources.files(_PACKAGE).joinpath(_DATA_DIR)


def _resolve(name: str) -> Traversable:
    """Resolve a relative template ``name`` confined to the resource root.

    Splits the name on ``/`` and rejects it when empty, absolute, or holding a ``.``,
    ``..`` or backslash component, so a lookup can never escape the packaged
    ``thoth.templates`` directory. Returns the located :class:`Traversable`, or raises
    :class:`TemplateError` when no such file ships.
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

    Walks the packaged ``templates/.obsidian`` tree, so dropping a new Obsidian config
    file in seeds it into a fresh vault with no code change. Returns forward-slash paths
    under the templates root, each prefixed ``.obsidian/``, sorted for a deterministic
    seed order. Empty when no ``.obsidian`` directory ships.
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
#: templates root, each prefixed ``.obsidian/``. :meth:`thoth.vault.Vault.seed` writes
#: each verbatim into ``<vault>/.obsidian/``, giving a fresh vault thoth's plugin set,
#: theme choice, and the ``dashboard-full-width`` snippet that the shipped
#: ``appearance.json`` enables.
OBSIDIAN_NAMES: tuple[str, ...] = _discover_obsidian_names()


def template_text(name: str) -> str:
    """Return the UTF-8 text of a packaged template by relative name.

    ``name`` is a forward-slash path under the templates root, such as ``index.md`` or
    ``_bases/home.base``. Raises :class:`TemplateError` when the name is unknown, or
    when it escapes the templates resource root by ``..`` or an absolute component.
    """
    resource = _resolve(name)
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - data is sane
        raise TemplateError(f"could not read template: {name!r}") from exc


def base_text(name: str) -> str:
    """Return the text of the ``_bases/<name>.base`` dashboard.

    ``name`` is a bare dashboard name from :data:`BASE_NAMES`, with no ``.base``
    suffix. Raises :class:`TemplateError` for an unknown dashboard.
    """
    return template_text(f"_bases/{name}.base")


def iter_templates() -> list[tuple[str, str]]:
    """Return ``(relative-name, text)`` for every packaged template.

    The result pairs each with its UTF-8 text: the three spine files, the
    ``_bases/*.base`` dashboards, the ``_bases/`` landing pages
    (:data:`BASE_DOC_NAMES`), the ``.obsidian`` config files and the root dotfiles.
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
