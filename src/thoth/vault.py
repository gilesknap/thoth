"""The closed, path-confined read/write surface over the Obsidian vault.

This module is the security core of the appliance (SPEC section 3): the LLM never
gets a shell or arbitrary file access, so every byte that reaches the vault passes
through the helpers here. They (a) confine paths to the resolved vault root, rejecting
anything that resolves outside it (absolute paths, ``..`` segments, and symlink
escapes are all caught *before* any disk is touched); (b) validate the folder-by-type
contract and the slug/asset-filename grammar; (c) read and write YAML frontmatter via
``python-frontmatter`` + ``pyyaml``; (d) stamp the required ``created``/``updated``
(and ``ingested``/``sha256`` for raw) fields; (e) make append-only, deduplicated edits
to ``index.md`` and ``log.md``; (f) move binary assets into ``raw/assets/`` (never
base64); and (g) redact secret-looking strings from body and frontmatter before
filing.

The module is pure filesystem and fully unit-testable on a temporary vault. It reuses
the frozen :class:`thoth.config.Config` for the vault root and name, and delegates the
single canonical ``obsidian://`` link encoding to :meth:`Config.obsidian_uri`; the
confinement check lives here so there is exactly one encoder and one confiner.

Only the standard library plus ``frontmatter``, ``yaml`` and ``slugify``
(``python-slugify``, pure-python) are imported at module level, so importing this module
is always CI-safe.

This module is also the single canonical source of the page-type / source / folder
vocabulary (issue #19): the classify prompt (:mod:`thoth.ingest`), the lint folder walks
(:mod:`thoth.lint`), the summary scans (:mod:`thoth.summary`) and the file-plan
validator (:mod:`thoth.llm`) all import these constants rather than restating them, and
:func:`slugify` is the one slug builder every caller routes through so the slug rule and
:data:`SLUG_RE` validation grammar never drift apart.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

import frontmatter
import yaml
from slugify import slugify as _slugify_lib

from thoth.config import Config

# --- module-level constants: the folder x type contract ---------------------------

KNOWLEDGE_TYPES: frozenset[str] = frozenset(
    {"entity", "concept", "comparison", "query", "summary"}
)
"""``type`` values for curated knowledge pages (SPEC section 5)."""

LIFE_ADMIN_TYPES: frozenset[str] = frozenset({"action", "media", "memory", "inbox"})
"""``type`` values for life-admin pages (SPEC section 9)."""

VALID_TYPES: frozenset[str] = KNOWLEDGE_TYPES | LIFE_ADMIN_TYPES
"""Every legal frontmatter ``type`` value (knowledge union life-admin)."""

# A stable, human-ordered enumeration of every type for prompt text. Frozensets have no
# meaningful order, so the classify prompt (thoth.ingest) derives its "one of ..." list
# from this tuple rather than restating the vocabulary. ``set(TYPE_ENUMERATION) ==
# VALID_TYPES`` is asserted in the tests, so a new type cannot land in one place only.
TYPE_ENUMERATION: tuple[str, ...] = (
    "entity",
    "concept",
    "comparison",
    "query",
    "summary",
    "action",
    "media",
    "memory",
    "inbox",
)
"""Canonical knowledge-then-life-admin ordering of :data:`VALID_TYPES` for prompts."""

VALID_SOURCES: frozenset[str] = frozenset({"slack", "mcp", "web", "manual", "cron"})
"""Every legal frontmatter ``source`` value (SPEC frontmatter contract)."""

FOLDER_TYPE_CONTRACT: dict[str, frozenset[str]] = {
    "entities": frozenset({"entity"}),
    "concepts": frozenset({"concept"}),
    "comparisons": frozenset({"comparison"}),
    "queries": frozenset({"query"}),
    "people": frozenset({"entity"}),
    "actions": frozenset({"action"}),
    "media": frozenset({"media"}),
    "memories": frozenset({"memory"}),
    "inbox": frozenset({"inbox"}),
}
"""Top-level vault folder -> the ``type`` values allowed to be written there."""

KNOWLEDGE_DIRS: tuple[str, ...] = ("entities", "concepts", "comparisons", "queries")
"""Curated knowledge folders, in catalog order (the four single-knowledge-type folders).

Canonical here so :mod:`thoth.lint` (its ``KNOWLEDGE_DIRS``) and :mod:`thoth.summary`
(its curated-scan dirs) derive the same list instead of restating it. Every entry is a
:data:`FOLDER_TYPE_CONTRACT` folder whose only allowed ``type`` is a knowledge type;
``people`` is a knowledge-typed folder too but is life-admin in the scans, so it is
listed under :data:`LIFE_ADMIN_DIRS` rather than here.
"""

LIFE_ADMIN_DIRS: tuple[str, ...] = (
    "actions",
    "media",
    "memories",
    "people",
    "inbox",
)
"""Life-admin folders, additionally scanned for frontmatter / stale / overdue checks.

Canonical here so :mod:`thoth.lint` derives the same list. Together with
:data:`KNOWLEDGE_DIRS` these partition the :data:`FOLDER_TYPE_CONTRACT` folder set (a
consistency the tests assert), so adding a folder is a one-place edit.
"""

RAW_SUBDIRS: frozenset[str] = frozenset({"articles", "papers", "transcripts", "assets"})
"""The ``raw/`` subdirectories (SPEC vault tree); ``assets`` is binary-only."""

SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Slug grammar: lowercase alphanumerics in single-hyphen-separated groups."""

# Caps applied by :func:`slugify` (the one slug builder). A slug keeps at most this many
# hyphen-separated words and this many characters, so a long title yields a short,
# filesystem-friendly slug that still satisfies :data:`SLUG_RE`.
_MAX_SLUG_WORDS: int = 8
_MAX_SLUG_LEN: int = 80

ASSET_SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+$")
"""Asset filename grammar: ``<slug>.<ext>`` (e.g. ``motor-diagram-e4a408.png``)."""

REQUIRED_COMMON_FIELDS: tuple[str, ...] = (
    "title",
    "type",
    "created",
    "updated",
    "source",
    "tags",
)
"""Frontmatter fields required on every curated/life-admin page."""

# created/updated are stamped by write_page, so the caller need not supply them; the
# remaining required common fields must be present in the input frontmatter.
_STAMPED_FIELDS: frozenset[str] = frozenset({"created", "updated"})
_AUTHOR_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    field for field in REQUIRED_COMMON_FIELDS if field not in _STAMPED_FIELDS
)

# Headings under "## Knowledge catalog" in index.md that append_index may target.
INDEX_SECTIONS: frozenset[str] = frozenset(
    {"Entities", "Concepts", "Comparisons", "Queries", "People"}
)
"""Valid ``index.md`` catalog section headings :meth:`Vault.append_index` may target.

Public so the curate prompt (:func:`thoth.llm.file_plan_contract_text`) can offer the
exact section names to the model, and the contract the model is given cannot drift from
the one :meth:`Vault.append_index` enforces.
"""

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
    is **always** a non-empty string satisfying :data:`SLUG_RE`. This is the single slug
    builder; every caller routes through it so the slug rule and the :data:`SLUG_RE`
    validation grammar cannot drift apart.

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


class VaultError(Exception):
    """Base error for vault-surface violations."""


class PathConfinementError(VaultError):
    """Raised when a path escapes the vault root (abs, ``..``, or symlink)."""


class SlugError(VaultError):
    """Raised when a slug or asset filename is malformed."""


class SchemaError(VaultError):
    """Raised when the frontmatter / type / folder contract is violated."""


@dataclass(frozen=True, slots=True)
class Page:
    """A parsed vault page: vault-relative path, frontmatter mapping, body text."""

    path: str
    frontmatter: dict[str, object]
    body: str


class Vault:
    """Path-confined read/write facade over one vault, built from a frozen Config."""

    def __init__(self, config: Config) -> None:
        """Store the config and cache the resolved absolute vault root."""
        self._config = config
        self._root = config.vault_path

    @property
    def root(self) -> Path:
        """Resolved absolute vault root (equals ``config.vault_path``)."""
        return self._root

    def schema_md(self) -> str | None:
        """Return the vault's ``SCHEMA.md`` text, or ``None`` when it is absent.

        The curate pass passes this to the model as ``system_extra`` so curated pages
        are filed to the *live* per-type schema (see :class:`thoth.ingest.Ingestor`'s
        ``schema_md``). A missing ``SCHEMA.md`` is a valid state (a bare/unseeded
        vault), so this returns ``None`` rather than raising; the contract enforced by
        :func:`thoth.llm.validate_file_plan` does not depend on it.
        """
        path = self._root / "SCHEMA.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    # ---- path confinement (the security core) -----------------------------------

    def resolve(self, vault_relative_path: str) -> Path:
        """Validate and resolve a vault-relative path to an absolute path in root.

        Rejects the empty string, absolute paths, any ``..`` or ``.`` part, and any
        result whose resolved location is not under the resolved root (so a symlink
        that points outside the vault is caught). Does not require the path to exist.

        Args:
            vault_relative_path: A POSIX-style path relative to the vault root.

        Returns:
            The absolute path inside the vault root.

        Raises:
            PathConfinementError: if the path is empty, absolute, contains a ``..`` or
                ``.`` segment, or resolves outside the vault root.
        """
        if not vault_relative_path:
            raise PathConfinementError("vault path must be a non-empty relative path")
        if (
            vault_relative_path.startswith("/")
            or PurePosixPath(vault_relative_path).is_absolute()
        ):
            raise PathConfinementError(
                f"vault path must be relative, not absolute: {vault_relative_path!r}"
            )
        # Inspect the raw segments: PurePosixPath silently drops '.' parts, so the
        # check must run on the original string (also catches '' from a '//' run).
        for segment in vault_relative_path.split("/"):
            if segment in ("..", ".", ""):
                raise PathConfinementError(
                    f"vault path may not contain {segment!r} segment: "
                    f"{vault_relative_path!r}"
                )

        pure = PurePosixPath(vault_relative_path)
        candidate = self._root / Path(*pure.parts)
        # Path.resolve follows symlinks in the existing prefix (so a symlinked
        # directory that escapes the vault is caught) and normalises the non-existent
        # tail lexically, so the leaf need not exist yet.
        resolved = candidate.resolve()
        resolved_root = self._root.resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise PathConfinementError(
                f"vault path escapes the vault root: {vault_relative_path!r}"
            )
        return candidate

    def is_inside(self, vault_relative_path: str) -> bool:
        """Return ``True`` if :meth:`resolve` would succeed, else ``False``."""
        try:
            self.resolve(vault_relative_path)
        except PathConfinementError:
            return False
        return True

    # ---- slug / folder / type validation (no disk touch) -------------------------

    @staticmethod
    def validate_slug(slug: str) -> str:
        """Return ``slug`` if it matches :data:`SLUG_RE`, else raise :class:`SlugError`.

        Accepts lowercase alphanumeric groups joined by single hyphens (for example
        ``program-motion-controller``); rejects uppercase, spaces, slashes, leading or
        trailing hyphens, doubled hyphens, and the empty string.
        """
        if not SLUG_RE.fullmatch(slug):
            raise SlugError(f"invalid slug {slug!r}: must match {SLUG_RE.pattern}")
        return slug

    @staticmethod
    def validate_asset_filename(name: str) -> str:
        """Return ``name`` if it matches :data:`ASSET_SLUG_RE`, else raise SlugError.

        Accepts ``<slug>.<ext>`` with a lowercase slug and lowercase extension (for
        example ``motor-control-diagram-e4a408.png``); rejects a missing extension,
        uppercase, and spaces.
        """
        if not ASSET_SLUG_RE.fullmatch(name):
            raise SlugError(
                f"invalid asset filename {name!r}: must match {ASSET_SLUG_RE.pattern}"
            )
        return name

    @staticmethod
    def validate_folder_type(folder: str, page_type: str) -> None:
        """Validate that ``page_type`` may live in ``folder``.

        Args:
            folder: A top-level vault folder name (key of :data:`FOLDER_TYPE_CONTRACT`).
            page_type: The frontmatter ``type`` value.

        Raises:
            SchemaError: if ``folder`` is not a known folder, or ``page_type`` is not
                permitted in that folder per :data:`FOLDER_TYPE_CONTRACT`.
        """
        allowed = FOLDER_TYPE_CONTRACT.get(folder)
        if allowed is None:
            raise SchemaError(
                f"unknown folder {folder!r}; expected one of "
                f"{sorted(FOLDER_TYPE_CONTRACT)}"
            )
        if page_type not in allowed:
            raise SchemaError(
                f"type {page_type!r} is not allowed in folder {folder!r}; "
                f"allowed: {sorted(allowed)}"
            )

    # ---- obsidian:// link (delegates to the ONE canonical builder) ---------------

    def obsidian_uri(self, vault_relative_path: str) -> str:
        """Confine ``vault_relative_path`` then return ``config.obsidian_uri(path)``.

        The path is first run through :meth:`resolve` for full confinement (including
        the symlink check); the percent-encoding itself is delegated to the single
        canonical builder on :class:`~thoth.config.Config`.

        Raises:
            PathConfinementError: if the path escapes the vault root.
        """
        self.resolve(vault_relative_path)
        return self._config.obsidian_uri(vault_relative_path)

    # ---- read --------------------------------------------------------------------

    def read_page(self, vault_relative_path: str) -> Page:
        """Confine, read, and split a page into frontmatter + body.

        Args:
            vault_relative_path: Vault-relative path to a ``.md`` file.

        Returns:
            A :class:`Page` with the vault-relative path, frontmatter mapping, and body.

        Raises:
            PathConfinementError: if the path escapes the vault root.
            VaultError: if the file does not exist.
        """
        absolute = self.resolve(vault_relative_path)
        if not absolute.is_file():
            raise VaultError(f"page does not exist: {vault_relative_path!r}")
        post = frontmatter.loads(absolute.read_text(encoding="utf-8"))
        return Page(
            path=PurePosixPath(vault_relative_path).as_posix(),
            frontmatter=dict(post.metadata),
            body=post.content,
        )

    def page_exists(self, vault_relative_path: str) -> bool:
        """Return ``True`` if a confined ``vault_relative_path`` exists as a file."""
        absolute = self.resolve(vault_relative_path)
        return absolute.is_file()

    @staticmethod
    def body_sha256(body: str) -> str:
        """Return the hex SHA-256 of the body text (the ``raw/`` idempotency key)."""
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @classmethod
    def stored_body_sha256(cls, body: str) -> str:
        """Return the ``sha256`` to stamp on a raw page for body drift detection.

        The digest must equal what any reader re-derives from disk -- namely
        ``body_sha256(post.content)`` where ``post`` is ``python-frontmatter``'s
        parse of the written file (see
        :meth:`thoth.lint.LintEngine.check_source_drift`). ``python-frontmatter``
        normalises ``post.content`` (it drops the leading blank line and trailing
        whitespace), so the digest is taken over a round trip through the exact
        serialisation :meth:`_write_post` writes -- redact, render, re-parse --
        rather than over the raw input string. Stamping the raw digest instead would
        make every body ending in a newline (the normal extractor/article case)
        report spurious drift.

        Args:
            body: The raw page body markdown (pre-redaction).

        Returns:
            The hex SHA-256 of the parse-stable, redacted body.
        """
        rendered = cls._render_page({}, redact_secrets(body))
        return cls.body_sha256(frontmatter.loads(rendered).content)

    @staticmethod
    def bytes_sha256(data: bytes) -> str:
        """Return the hex SHA-256 of raw bytes (the binary-asset idempotency key)."""
        return hashlib.sha256(data).hexdigest()

    def asset_exists(self, asset_filename: str) -> bool:
        """Return ``True`` if ``raw/assets/<asset_filename>`` already exists.

        The filename is validated and confined first (so a malformed or escaping
        name is rejected, not silently reported absent).

        Args:
            asset_filename: The asset filename (validated by
                :meth:`validate_asset_filename`).

        Returns:
            ``True`` if the confined asset path exists as a file, else ``False``.

        Raises:
            SlugError: on an invalid asset filename.
            PathConfinementError: if the destination escapes the vault root.
        """
        self.validate_asset_filename(asset_filename)
        return self.resolve(f"raw/assets/{asset_filename}").is_file()

    def asset_sha256(self, asset_filename: str) -> str:
        """Return the hex SHA-256 of an existing asset's bytes.

        The filename is validated and confined first. Used by the ingest pass to
        decide whether a re-uploaded binary is byte-identical (idempotent skip) or a
        genuine change (drift) before calling :meth:`save_asset`.

        Args:
            asset_filename: The asset filename (validated by
                :meth:`validate_asset_filename`).

        Returns:
            The hex SHA-256 of the asset's bytes.

        Raises:
            SlugError: on an invalid asset filename.
            PathConfinementError: if the destination escapes the vault root.
            VaultError: if the asset does not exist.
        """
        self.validate_asset_filename(asset_filename)
        rel = f"raw/assets/{asset_filename}"
        absolute = self.resolve(rel)
        if not absolute.is_file():
            raise VaultError(f"asset does not exist: {rel!r}")
        return self.bytes_sha256(absolute.read_bytes())

    # ---- write curated / raw pages (validate-then-write) -------------------------

    def write_page(
        self,
        folder: str,
        slug: str,
        frontmatter_in: dict[str, object],
        body: str,
        *,
        today: date | None = None,
    ) -> str:
        """Validate, redact, stamp, and atomically write a curated/life-admin page.

        Validates the folder-by-type contract, the slug grammar, the required common
        fields, and the ``source`` value; redacts secrets from the body and string
        frontmatter values; stamps ``created`` (preserved on update) and ``updated``
        (always the run date); then writes ``<folder>/<slug>.md`` atomically.

        Args:
            folder: A top-level vault folder (key of :data:`FOLDER_TYPE_CONTRACT`).
            slug: The page slug (validated by :meth:`validate_slug`).
            frontmatter_in: The page frontmatter; must contain a valid ``type`` and
                ``source`` and the other :data:`REQUIRED_COMMON_FIELDS`.
            body: The page body markdown.
            today: The date to stamp; defaults to :meth:`date.today`.

        Returns:
            The vault-relative path written (for example ``entities/foo.md``).

        Raises:
            SchemaError: on a folder/type mismatch, missing required field, bad type,
                or invalid source.
            SlugError: on an invalid slug.
        """
        self.validate_slug(slug)
        meta = dict(frontmatter_in)
        page_type = meta.get("type")
        if not isinstance(page_type, str):
            raise SchemaError("frontmatter 'type' must be a string")
        self.validate_folder_type(folder, page_type)
        self._validate_common_fields(meta)

        rel = f"{folder}/{slug}.md"
        stamp = today or date.today()
        existing_created = self._existing_created(rel)
        meta["created"] = existing_created if existing_created is not None else stamp
        meta["updated"] = stamp
        self._write_post(rel, meta, body)
        return rel

    def write_raw(
        self,
        subdir: str,
        slug: str,
        frontmatter_in: dict[str, object],
        body: str,
        *,
        today: date | None = None,
    ) -> str:
        """Write an immutable ``raw/<subdir>/<slug>.md`` source page.

        Stamps ``ingested`` and ``sha256`` (digest of the parse-stable redacted body,
        per :meth:`stored_body_sha256`) and redacts secrets from the body and string
        frontmatter values.

        Args:
            subdir: A ``raw/`` subdirectory in :data:`RAW_SUBDIRS`, excluding
                ``assets`` (which is binary-only -- use :meth:`save_asset`).
            slug: The raw page slug (validated by :meth:`validate_slug`).
            frontmatter_in: Raw frontmatter (for example ``source_url``); ``ingested``
                and ``sha256`` are added/overwritten by this method.
            body: The raw page body markdown.
            today: The date to stamp as ``ingested``; defaults to :meth:`date.today`.

        Returns:
            The vault-relative path written (for example ``raw/papers/foo.md``).

        Raises:
            SchemaError: if ``subdir`` is ``assets`` or not a known raw subdir.
            SlugError: on an invalid slug.
        """
        if subdir == "assets":
            raise SchemaError(
                "raw/assets is binary-only; use save_asset, not write_raw"
            )
        if subdir not in RAW_SUBDIRS:
            raise SchemaError(
                f"unknown raw subdir {subdir!r}; expected one of "
                f"{sorted(RAW_SUBDIRS - {'assets'})}"
            )
        self.validate_slug(slug)
        meta = dict(frontmatter_in)
        stamp = today or date.today()
        meta["ingested"] = stamp
        meta["sha256"] = self.stored_body_sha256(body)
        rel = f"raw/{subdir}/{slug}.md"
        self._write_post(rel, meta, body)
        return rel

    def save_asset(self, tmp_path: Path, asset_filename: str) -> str:
        """Move a downloaded binary into ``raw/assets/<asset_filename>``.

        The filename is validated and confined; the bytes are moved verbatim (never
        base64). Refuses to overwrite an existing asset.

        Args:
            tmp_path: Path to the already-downloaded binary file.
            asset_filename: The destination filename (validated by
                :meth:`validate_asset_filename`).

        Returns:
            The vault-relative path written (for example ``raw/assets/foo.png``).

        Raises:
            SlugError: on an invalid asset filename.
            PathConfinementError: if the destination escapes the vault root.
            VaultError: if the source is missing or the destination already exists.
        """
        self.validate_asset_filename(asset_filename)
        rel = f"raw/assets/{asset_filename}"
        destination = self.resolve(rel)
        if not tmp_path.is_file():
            raise VaultError(f"source asset does not exist: {tmp_path}")
        if destination.exists():
            raise VaultError(f"refusing to overwrite existing asset: {rel!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(tmp_path), os.fspath(destination))
        return rel

    def remove_page(self, vault_relative_path: str) -> bool:
        """Delete a confined ``vault_relative_path`` if it exists (idempotent).

        The path is confined first (so an absolute, ``..``, or symlink-escaping path is
        rejected, never deleted), then the file is unlinked if present. Used to drop a
        superseded ``inbox/`` holding page once a deferred capture has been curated (the
        durable raw/curated pages then carry the content); a missing file is a no-op.

        Args:
            vault_relative_path: Vault-relative path to remove.

        Returns:
            ``True`` if a file was removed, ``False`` if nothing was there.

        Raises:
            PathConfinementError: if the path escapes the vault root.
        """
        absolute = self.resolve(vault_relative_path)
        if not absolute.is_file():
            return False
        absolute.unlink()
        return True

    # ---- navigation edits (append-only / idempotent) ----------------------------

    def append_index(self, section: str, wikilink: str, summary: str) -> None:
        """Add ``- [[wikilink]] - summary`` under ``### <section>`` in ``index.md``.

        The catalog line is kept sorted within its section and deduplicated by
        wikilink, so calling twice with the same wikilink is idempotent (the summary
        of the last call wins).

        Args:
            section: One of ``Entities``, ``Concepts``, ``Comparisons``, ``Queries``,
                ``People``.
            wikilink: The wikilink target (without the ``[[ ]]`` brackets).
            summary: A one-line summary shown after the wikilink.

        Raises:
            SchemaError: if ``section`` is not a known knowledge-catalog section.
            VaultError: if ``index.md`` is missing or lacks the named heading.
        """
        if section not in INDEX_SECTIONS:
            raise SchemaError(
                f"unknown index section {section!r}; expected one of "
                f"{sorted(INDEX_SECTIONS)}"
            )
        absolute = self.resolve("index.md")
        if not absolute.is_file():
            raise VaultError("index.md does not exist")
        lines = absolute.read_text(encoding="utf-8").splitlines()
        heading = f"### {section}"
        try:
            start = lines.index(heading)
        except ValueError as exc:
            raise VaultError(f"index.md is missing the heading {heading!r}") from exc

        # The section body runs to the next heading (## or ###) or end of file.
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].startswith("## ") or lines[index].startswith("### "):
                end = index
                break

        new_entry = f"- [[{wikilink}]] - {summary}"
        entries: list[str] = []
        seen_targets: set[str] = set()
        for line in lines[start + 1 : end]:
            if line.startswith("- [["):
                target = line[len("- [[") :].split("]]", 1)[0]
                if target == wikilink or target in seen_targets:
                    continue
                seen_targets.add(target)
                entries.append(line)
        entries.append(new_entry)
        entries.sort(key=str.lower)

        # Reassemble: heading, blank line, sorted entries, then preserve any trailing
        # blank line that separated this section from the next.
        rebuilt = [heading, *entries]
        if end < len(lines):
            rebuilt.append("")
        new_lines = [*lines[:start], *rebuilt, *lines[end:]]
        absolute.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def append_log(self, action: str, subject: str, files: list[str]) -> None:
        """Append a dated action block plus the touched-file list to ``log.md``.

        Args:
            action: One of ``ingest``, ``create``, ``update``, ``query``, ``lint``,
                ``archive``, ``delete``, ``reindex``.
            subject: A short human-readable subject for the action.
            files: The vault-relative paths touched by the action.

        Raises:
            SchemaError: if ``action`` is not a known log action.
            VaultError: if ``log.md`` is missing.
        """
        if action not in _LOG_ACTIONS:
            raise SchemaError(
                f"unknown log action {action!r}; expected one of {sorted(_LOG_ACTIONS)}"
            )
        absolute = self.resolve("log.md")
        if not absolute.is_file():
            raise VaultError("log.md does not exist")
        stamp = date.today().isoformat()
        block_lines = [f"## [{stamp}] {action} | {subject}"]
        block_lines.extend(f"- {path}" for path in files)
        existing = absolute.read_text(encoding="utf-8")
        separator = "" if existing.endswith("\n") else "\n"
        absolute.write_text(
            existing + separator + "\n" + "\n".join(block_lines) + "\n",
            encoding="utf-8",
        )

    def embed_asset_markdown(self, asset_filename: str) -> str:
        """Return the Obsidian embed string ``![[<asset_filename>]]`` (validated).

        Raises:
            SlugError: if ``asset_filename`` is not a valid asset filename.
        """
        self.validate_asset_filename(asset_filename)
        return f"![[{asset_filename}]]"

    # ---- internals ---------------------------------------------------------------

    def _validate_common_fields(self, meta: dict[str, object]) -> None:
        """Validate author-supplied common fields, the ``type``, and ``source``.

        ``created`` and ``updated`` are intentionally not required here: they are
        stamped by :meth:`write_page` (the caller supplies neither), so requiring them
        pre-stamp would be wrong.
        """
        missing = [
            field for field in _AUTHOR_REQUIRED_FIELDS if meta.get(field) in (None, "")
        ]
        if missing:
            raise SchemaError(
                f"missing required frontmatter field(s): {', '.join(missing)}"
            )
        page_type = meta["type"]
        if page_type not in VALID_TYPES:
            raise SchemaError(
                f"invalid type {page_type!r}; expected one of {sorted(VALID_TYPES)}"
            )
        source = meta["source"]
        if source not in VALID_SOURCES:
            raise SchemaError(
                f"invalid source {source!r}; expected one of {sorted(VALID_SOURCES)}"
            )

    def _existing_created(self, vault_relative_path: str) -> object | None:
        """Return the ``created`` value of an existing page, or ``None``."""
        absolute = self.resolve(vault_relative_path)
        if not absolute.is_file():
            return None
        post = frontmatter.loads(absolute.read_text(encoding="utf-8"))
        return post.metadata.get("created")

    @staticmethod
    def _render_page(meta: dict[str, object], body: str) -> str:
        """Serialise already-redacted ``meta`` + ``body`` to the on-disk page text.

        This is the single source of truth for a page's byte layout. The frontmatter
        block is rendered with :func:`yaml.safe_dump` so the key order we assembled is
        preserved (``frontmatter.dumps`` would re-sort it), and the body is written with
        a single trailing newline.

        Args:
            meta: The redacted frontmatter mapping (already passed through
                :func:`_redact_frontmatter`).
            body: The redacted body markdown (already passed through
                :func:`redact_secrets`).

        Returns:
            The exact text written to disk for this page.
        """
        block = yaml.safe_dump(
            meta,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        return f"---\n{block}---\n\n{body.rstrip(chr(10))}\n"

    def _write_post(
        self, vault_relative_path: str, meta: dict[str, object], body: str
    ) -> None:
        """Redact, serialise (stable key order), and atomically write a page.

        The body bytes are laid out by :meth:`_render_page`; the file is written to a
        sibling ``.tmp`` and atomically replaced so a crash never leaves a half-written
        page in the vault.
        """
        absolute = self.resolve(vault_relative_path)
        text = self._render_page(_redact_frontmatter(meta), redact_secrets(body))
        absolute.parent.mkdir(parents=True, exist_ok=True)
        tmp = absolute.with_name(absolute.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(absolute)


# ---- module-level secret redaction (also importable standalone) -------------------

# Token-shaped patterns. Each is conservative: it matches a recognisable provider
# prefix followed by a run of token characters, or a labelled secret assignment, or a
# long opaque hex/base64 blob. Ordinary prose and short words never match.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider-prefixed API keys: sk-..., sk-ant-..., ghp_/gho_/ghs_..., xoxb-/xapp-...
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"),
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Bearer <token> authorization headers.
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{10,}"),
    # key=VALUE / key: VALUE for a sensitive key name.
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b"
        r"\s*[:=]\s*\S{6,}"
    ),
    # Long opaque hex blob (e.g. a 32+ char digest used as a credential).
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # Long opaque base64-ish blob (mixed case + digits, no spaces).
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)

_REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings with a fixed ``[REDACTED]`` marker.

    Masks provider-prefixed API keys (``sk-...``, ``ghp_...``), AWS access key ids
    (``AKIA...``), ``Bearer <token>`` headers, ``key=VALUE`` assignments for a
    sensitive key set, and long opaque hex/base64 blobs. The match is conservative so
    ordinary prose and short words are left untouched. Applied to body and frontmatter
    before filing (SPEC section 12). Never raises; a non-string input is returned
    unchanged.
    """
    if not isinstance(text, str):
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


# Writer-controlled structural fields: generated from validated inputs (dates and the
# body digest), never user free-text, so they are exempt from redaction. The sha256
# digest in particular is a 64-char hex string the long-hex-blob rule would else mask.
_NEVER_REDACT_FIELDS: frozenset[str] = frozenset(
    {"created", "updated", "ingested", "sha256"}
)


def _redact_frontmatter(meta: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``meta`` with secrets redacted from string values.

    Recurses into list and dict values; non-string scalars (dates, ints, bools) are
    preserved as-is so frontmatter typing and date stamping are not disturbed.
    Writer-controlled structural fields (:data:`_NEVER_REDACT_FIELDS`) are passed
    through verbatim so the generated ``sha256`` digest is not mistaken for a secret.
    """
    return {
        key: (value if key in _NEVER_REDACT_FIELDS else _redact_value(value))
        for key, value in meta.items()
    }


def _redact_value(value: object) -> object:
    """Redact secrets from a frontmatter value, recursing into lists and dicts."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value
