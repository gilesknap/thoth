"""The canonical page-type / source / folder vocabulary and the slug grammar.

The single source of the folder x type contract (issue #19): the classify prompt
(:mod:`thoth.ingest`), the lint folder walks (:mod:`thoth.lint`), the summary scans
(:mod:`thoth.summary`) and the file-plan validator (:mod:`thoth.llm`) all import these
constants rather than restating them, and :func:`slugify` lives next to the
:data:`SLUG_RE` validation grammar so the slug rule and the grammar never drift apart.
"""

from __future__ import annotations

import re

from slugify import slugify as _slugify_lib

# --- module-level constants: the folder x type contract ---------------------------

# A stable, human-ordered enumeration of the four content types a capture may be
# classified into (ADR 0005). Frozensets have no meaningful order, so the classify
# prompt (thoth.ingest) derives its "one of ..." list from this tuple rather than
# restating the vocabulary. ``inbox`` is machinery (the durable pre-curate holding
# type), never a classify target, so it is excluded here and added to VALID_TYPES.
TYPE_ENUMERATION: tuple[str, ...] = (
    "entity",
    "note",
    "memory",
    "action",
)
"""Canonical ordering of the four content :data:`VALID_TYPES` offered to the classifier.

ADR 0005 collapsed the eight folders into four flat, equal folders, so a capture is one
of exactly four content types: ``entity`` (nouns), ``note`` (everything written,
differentiated by a ``tags:`` value such as ``concept``/``comparison``/``query``),
``memory`` (personal reference), and ``action`` (carries ``status``/``due``; a media
item is an ``action`` tagged ``media``). ``summary`` is no longer a content type -- it
survives only as the label on the spine ``index.md`` Home page.
"""

INBOX_TYPE: str = "inbox"
"""The machinery ``type`` for a durable pre-curate ``inbox/`` holding page (ADR 0004).

Not a content type the classifier may pick (so absent from :data:`TYPE_ENUMERATION`),
but a legal frontmatter ``type`` :meth:`thoth.vault.Vault.write_page` accepts for the
``inbox/`` folder, so it is a member of :data:`VALID_TYPES`.
"""

VALID_TYPES: frozenset[str] = frozenset(TYPE_ENUMERATION) | {INBOX_TYPE}
"""Every legal frontmatter ``type`` value (the four content types plus ``inbox``)."""

REFERENCE_TYPES: frozenset[str] = frozenset({"entity", "note", "memory"})
"""The lifecycle-free reference content types (ADR 0005): the non-actionable types.

Replaces the old ``KNOWLEDGE_TYPES`` family. Used as the default recall scope for
knowledge Q&A (:meth:`thoth.query.QueryEngine.recall_paths`): with the knowledge /
life-admin families gone, "what do I know about X?" excludes the actionable ``action``
type (todos and the to-consume media queue) by scoping to these reference types instead.
"""

VALID_SOURCES: frozenset[str] = frozenset(
    {"slack", "mcp", "web", "manual", "cron", "import"}
)
"""Every legal frontmatter ``source`` value (SPEC frontmatter contract).

``import`` is the provenance of a page filed by the ``thoth capture <path>`` CLI
backfill (issue #80): content that already lived on disk (a single file or a walked
directory of Markdown + assets), fed through the same :class:`~thoth.ingest.Ingestor`
pipeline as a Slack/MCP capture. :meth:`thoth.vault.Vault.write_page` validates
``source`` against this set, so the value must live here for an imported page to be
writable."""

FOLDER_TYPE_CONTRACT: dict[str, frozenset[str]] = {
    "entities": frozenset({"entity"}),
    "notes": frozenset({"note"}),
    "memories": frozenset({"memory"}),
    "actions": frozenset({"action"}),
    "inbox": frozenset({"inbox"}),
}
"""Top-level vault folder -> the ``type`` values allowed to be written there (ADR 0005).

Four flat content folders plus the ``inbox/`` holding folder. ``entities/`` absorbs the
old ``people/``; ``notes/`` absorbs ``concepts/``/``comparisons/``/``queries/``
(differentiated by a ``tags:`` value, not a folder); ``actions/`` absorbs ``media/`` (a
media item is an ``action`` tagged ``media``); ``memories/`` is kept as its own folder.
"""

CURATED_DIRS: tuple[str, ...] = ("entities", "notes", "memories")
"""The lifecycle-free reference folders, in catalog order (ADR 0005).

Canonical here so :mod:`thoth.lint` and :mod:`thoth.summary` derive the same list
instead of restating it. These are the reference pages that carry a one-line
``summary:`` frontmatter gloss and get the orphan / stale checks. They carry no
``status``/``due`` lifecycle.
"""

ACTIONABLE_DIRS: tuple[str, ...] = ("actions",)
"""The lifecycle-bearing folder(s) scanned for overdue / cold checks (ADR 0005).

A page here carries ``status``/``due`` and shows in the actionable Bases dashboards; the
to-consume media queue lives here too (an ``action`` tagged ``media``). Together with
:data:`CURATED_DIRS` and the ``inbox/`` holding folder these are the
:data:`FOLDER_TYPE_CONTRACT` folders (a consistency the tests assert), so adding a
folder is a one-place edit.
"""

RAW_SUBDIRS: frozenset[str] = frozenset({"articles", "papers", "transcripts", "assets"})
"""The ``raw/`` subdirectories (SPEC vault tree); ``assets`` is binary-only."""

SEED_DIRS: tuple[str, ...] = (
    CURATED_DIRS
    + ACTIONABLE_DIRS
    + ("inbox",)
    + tuple(f"raw/{subdir}" for subdir in sorted(RAW_SUBDIRS))
)
"""Every empty content folder :meth:`thoth.vault.Vault.seed` creates.

The four flat content folders (:data:`CURATED_DIRS` + :data:`ACTIONABLE_DIRS`) plus the
``inbox/`` holding folder and the ``raw/`` subdirectories, so a freshly seeded vault has
the full browsable skeleton in Obsidian even before any page is filed. Derived from the
same canonical dir constants rather than restating them, so adding a folder is a
one-place edit."""

SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Slug grammar: lowercase alphanumerics in single-hyphen-separated groups."""

# Caps applied by :func:`slugify`. A slug keeps at most this many hyphen-separated
# words and this many characters, so a long title yields a short, filesystem-friendly
# slug that still satisfies :data:`SLUG_RE`.
_MAX_SLUG_WORDS: int = 8
_MAX_SLUG_LEN: int = 80

ASSET_SLUG_RE: re.Pattern[str] = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)+$"
)
"""Asset filename grammar: ``<slug>`` plus one or more lowercase extensions.

A single extension is the common case (``motor-diagram-e4a408.png``); a *compound*
extension such as ``motor-diagram-e4a408.excalidraw.md`` is also accepted, so the
advanced-image artifacts (issue #68) -- the editable Excalidraw reconstruction saved as
``<slug>.excalidraw.md`` -- validate as assets. The grammar still forbids ``..`` (every
dot must be followed by a ``[a-z0-9]`` group), a leading dot, uppercase, and spaces.
"""

REQUIRED_COMMON_FIELDS: tuple[str, ...] = (
    "title",
    "type",
    "created",
    "updated",
    "source",
    "tags",
)
"""Frontmatter fields required on every curated/life-admin page."""

SUMMARY_TYPES: frozenset[str] = REFERENCE_TYPES
"""Page ``type`` values that carry a one-line ``summary:`` frontmatter gloss (#72).

The lifecycle-free reference types (``entity``/``note``/``memory``) each carry an
optional one-line ``summary:`` frontmatter field authored by the curate pass -- the
canonical, rebuildable home of a page's gloss (replacing the old agent-maintained
``index.md`` catalog; ADR 0008). ``action``/``inbox`` pages do not get one (they are
surfaced by the Bases dashboards, not a summary). The ``summary`` is plain frontmatter
that round-trips through :meth:`thoth.vault.Vault.read_page` /
:meth:`thoth.vault.Vault.write_page` like any other field, so it needs no special write
path; this constant exists so the curate contract
(:func:`thoth.llm.file_plan_contract_text`) and the lint invariant
(:meth:`thoth.lint.LintEngine.check_summaries`) share one definition of "which pages are
glossed" with :data:`REFERENCE_TYPES`.
"""

# created/updated are stamped by write_page, so the caller need not supply them; the
# remaining required common fields must be present in the input frontmatter.
_STAMPED_FIELDS: frozenset[str] = frozenset({"created", "updated"})
_AUTHOR_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    field for field in REQUIRED_COMMON_FIELDS if field not in _STAMPED_FIELDS
)

# Actions accepted by append_log (SPEC log.md seed template).
_LOG_ACTIONS: frozenset[str] = frozenset(
    {"ingest", "create", "update", "query", "lint", "archive", "delete", "reindex"}
)


def slugify(text: str, *, fallback: str = "untitled") -> str:
    """Build a vault slug from free text via ``python-slugify``, capped and validated.

    Wraps :func:`slugify.slugify` (``python-slugify``) with the project caps
    (:data:`_MAX_SLUG_WORDS` words, :data:`_MAX_SLUG_LEN` characters, lowercase, word
    boundaries respected) so a long title yields a short, filesystem-friendly slug.
    Unlike the old hand-rolled strippers, ``python-slugify`` *transliterates* non-ASCII
    rather than dropping it: ``"café notes"`` becomes ``cafe-notes`` and ``"naïve
    Bayes"`` becomes ``naive-bayes``. When the input transliterates to nothing usable
    (empty, whitespace, or symbols-only) the ``fallback`` word is returned, so the slug
    is **always** a non-empty string satisfying :data:`SLUG_RE`. The slug rule is
    defined here, next to the :data:`SLUG_RE` validation grammar, so the two cannot
    drift apart.

    Args:
        text: The free text to slugify (typically a page title or question).
        fallback: The slug returned when ``text`` has no slug-able characters; must
            itself satisfy :data:`SLUG_RE` (defaults to ``"untitled"``).

    Returns:
        A slug string guaranteed to satisfy :data:`SLUG_RE`.
    """
    slug = _slugify_lib(
        text,
        max_length=_MAX_SLUG_LEN,
        word_boundary=True,
        separator="-",
        lowercase=True,
    )
    words = slug.split("-")[:_MAX_SLUG_WORDS]
    slug = "-".join(word for word in words if word)
    return slug or fallback
