"""The canonical page-type, source and folder vocabulary, and the slug grammar.

The single source of the folder-by-type contract (issue #19). The classify prompt, the
lint folder walks, the summary scans and the file-plan validator all import these
constants rather than restating them, so the contract and its consumers never drift.
"""

from __future__ import annotations

import re

# --- module-level constants: the folder x type contract ---------------------------

# Frozensets have no meaningful order, so the classify prompt derives its "one of ..."
# list from this tuple rather than restating the vocabulary. Inbox is machinery and
# never a classify target, so it is excluded here and added to VALID_TYPES instead
TYPE_ENUMERATION: tuple[str, ...] = (
    "entity",
    "note",
    "memory",
    "action",
    "media",
)
"""Canonical ordering of the five content types offered to the classifier.

ADR 0005 collapsed eight folders into flat, equal ones, so a capture is exactly one of
``entity`` for nouns, ``note`` for everything written and differentiated by a ``tags:``
value, ``memory`` for personal reference, ``action`` for a todo carrying
``status``/``due``, and ``media`` for a to-consume item carrying the same lifecycle plus
``media_type`` and ``url``. ADR 0015 promoted media from an ``action`` with
``kind: media`` to its own type and retired the ``kind`` facet, so moving an item
between the actionable dashboards is now one ``type`` edit. ``summary`` is no longer a
content type and survives only as the label on the spine ``index.md``.
"""

INBOX_TYPE: str = "inbox"
"""The machinery ``type`` for a durable pre-curate ``inbox/`` holding page (ADR 0004).

The classifier may not pick it, so it is absent from :data:`TYPE_ENUMERATION`, but
:meth:`thoth.vault.Vault.write_page` accepts it for the ``inbox/`` folder, so it is a
member of :data:`VALID_TYPES`.
"""

VALID_TYPES: frozenset[str] = frozenset(TYPE_ENUMERATION) | {INBOX_TYPE}
"""Every legal frontmatter ``type`` value (the five content types plus ``inbox``)."""

REFERENCE_TYPES: frozenset[str] = frozenset({"entity", "note", "memory"})
"""The lifecycle-free reference content types, the non-actionable ones (ADR 0005).

This is the default recall scope for knowledge Q&A. With the old knowledge and
life-admin families gone, "what do I know about X?" excludes ``action`` and ``media`` by
scoping to these instead.
"""

VALID_SOURCES: frozenset[str] = frozenset(
    {"slack", "mcp", "web", "manual", "cron", "import"}
)
"""Every legal frontmatter ``source`` value (SPEC frontmatter contract).

``import`` is the provenance of a page filed by the ``thoth capture <path>`` backfill
(issue #80), content that already lived on disk and went through the same ingest
pipeline as a Slack or MCP capture. :meth:`thoth.vault.Vault.write_page` validates
``source`` against this set, so the value must live here to be writable."""

FOLDER_TYPE_CONTRACT: dict[str, frozenset[str]] = {
    "entities": frozenset({"entity"}),
    "notes": frozenset({"note"}),
    "memories": frozenset({"memory"}),
    "actions": frozenset({"action"}),
    "media": frozenset({"media"}),
    "inbox": frozenset({"inbox"}),
}
"""Each top-level folder and the ``type`` values allowed in it (ADR 0005).

Five flat content folders plus the ``inbox/`` hold. ``entities/`` absorbed the old
``people/`` and ``notes/`` absorbed ``concepts/``, ``comparisons/`` and ``queries/``,
which a ``tags:`` value now differentiates. ADR 0015 gave ``media`` its own type and so
its own folder again. The folder a page lives in is a loose browsing convenience: thoth
writes each type to its canonical folder, but the Bases dashboards key off ``type``, so
a manual move never hides a page. ``inbox/`` and ``raw/`` stay folder-strict machinery.
"""

CURATED_DIRS: tuple[str, ...] = ("entities", "notes", "memories")
"""The lifecycle-free reference folders, in catalog order (ADR 0005).

Canonical here so :mod:`thoth.lint` and :mod:`thoth.summary` derive the same list rather
than restate it. These pages carry a one-line ``summary:`` gloss and get the orphan and
stale checks, and they carry no ``status`` or ``due`` lifecycle.
"""

ACTIONABLE_DIRS: tuple[str, ...] = ("actions", "media")
"""The lifecycle-bearing folders scanned for overdue and cold checks (ADR 0015).

A page here carries ``status`` and ``due`` and shows in the actionable Bases dashboards:
``actions/`` holds todos and errands, ``media/`` the to-consume queue. Together with
:data:`CURATED_DIRS` and the ``inbox/`` hold these are exactly the
:data:`FOLDER_TYPE_CONTRACT` folders, a consistency the tests assert, so adding a folder
is a one-place edit.
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

The five content folders plus the ``inbox/`` hold and the ``raw/`` subdirectories, so a
freshly seeded vault has the full browsable skeleton in Obsidian before any page is
filed. Derived from the canonical dir constants, so adding a folder is a one-place
edit."""

SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Slug grammar: lowercase alphanumerics in single-hyphen-separated groups."""

ASSET_SLUG_RE: re.Pattern[str] = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)+$"
)
"""Asset filename grammar: ``<slug>`` plus one or more lowercase extensions.

A single extension is the common case, but a compound one such as
``<slug>.excalidraw.md`` is accepted too, so the editable Excalidraw reconstruction
(issue #68) validates as an asset. The grammar still forbids ``..``, since every dot
must be followed by an ``[a-z0-9]`` group, and forbids a leading dot, uppercase and
spaces.
"""

REQUIRED_COMMON_FIELDS: tuple[str, ...] = (
    "title",
    "type",
    "created",
    "updated",
    "source",
    "tags",
)
"""Frontmatter fields required on every content page, enforced at write time.

The write-gate set :meth:`thoth.vault.Vault.write_page` and
:func:`thoth.llm.validate_file_plan` enforce. The curate pass fills the richer
:data:`CONTENT_COMMON_FIELDS` superset, but ``summary`` and ``personal`` must not be
required here: the plan validator requires exactly these keys, and ``summary`` arrives
via the page-level plan field after validation, so requiring it would reject every plan.
"""

CONTENT_COMMON_FIELDS: tuple[str, ...] = (
    *REQUIRED_COMMON_FIELDS,
    "summary",
    "personal",
)
"""The universal frontmatter set every content page carries (ADR 0013).

The write-gate set plus the two curate-authored universals: ``summary``, a crisp
one-line gloss now on actions too, and ``personal``, a real boolean separating
private-life items from work ones that the Work and Personal Bases views filter on.
Enforced by lint check 4, not by the write gate.
"""

INBOX_REQUIRED_FIELDS: tuple[str, ...] = (
    "title",
    "type",
    "created",
    "updated",
    "source",
    "sha256",
)
"""The frontmatter set for ``inbox/`` holding pages (machinery, ADR 0013).

A hold is pre-curate machinery rather than content, so it carries the body digest that
is the idempotency key, no ``tags``, since the taxonomy describes curated content and a
hold has not been classified yet, and none of the content universals.
"""

ACTION_STATUS_VOCAB: tuple[str, ...] = ("todo", "in_progress", "done", "cancelled")
"""The single ``status`` lifecycle shared by ``action`` and ``media`` (ADR 0013).

One vocabulary covers every actionable page whatever its type, because media-ness is
carried by the ``type`` (ADR 0015) rather than by the parallel media statuses, which are
gone. Ordered for prompt rendering.
"""

PRIORITY_VOCAB: tuple[str, ...] = ("Urgent", "High", "Medium", "Low")
"""Allowed ``priority`` values, ordered high to low for the curate prompt.

These are bare severity labels rather than the old sort-prefixed ``1 - Urgent`` form.
The Bases dashboards recover the ordering through a ``prio_rank`` formula in
``actions.base`` and sort on that, so the stored value stays a clean label and an
ascending string sort never sees it (ADR 0013).
"""

MEDIA_TYPE_VOCAB: tuple[str, ...] = (
    "book",
    "film",
    "tv",
    "podcast",
    "article",
    "video",
    "music",
)
"""Allowed ``media_type`` values, ordered for prompt rendering (SPEC table)."""

SUMMARY_TYPES: frozenset[str] = frozenset(TYPE_ENUMERATION)
"""Page ``type`` values that carry a one-line ``summary:`` gloss (issue #72).

All five content types, since ADR 0013 extended the gloss to actions and ADR 0015 to
media so the Bases dashboards have a Summary column. An ``inbox`` hold is machinery and
gets none. The gloss is plain frontmatter that round-trips like any other field, so it
needs no special write path, and this constant exists so the curate contract and the
lint invariant share one definition of which pages are glossed.
"""

# write_page stamps created and updated, so a caller supplies neither. The remaining
# required fields must be in the input frontmatter, and content pages and inbox holds
# have different sets because a hold is machinery with no tags (ADR 0013)
_STAMPED_FIELDS: frozenset[str] = frozenset({"created", "updated"})
_AUTHOR_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    field for field in REQUIRED_COMMON_FIELDS if field not in _STAMPED_FIELDS
)
_AUTHOR_REQUIRED_INBOX_FIELDS: tuple[str, ...] = tuple(
    field for field in INBOX_REQUIRED_FIELDS if field not in _STAMPED_FIELDS
)

# The actions append_log accepts (SPEC log.md seed template)
_LOG_ACTIONS: frozenset[str] = frozenset(
    {"ingest", "create", "update", "query", "lint", "archive", "delete", "reindex"}
)
