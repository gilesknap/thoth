"""The errors, page records, and the path-confined :class:`Vault` facade itself."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

import frontmatter
import yaml

from thoth.config import Config

from .contract import (
    _AUTHOR_REQUIRED_FIELDS,
    _AUTHOR_REQUIRED_INBOX_FIELDS,
    _LOG_ACTIONS,
    ASSET_SLUG_RE,
    FOLDER_TYPE_CONTRACT,
    INBOX_TYPE,
    RAW_SUBDIRS,
    SEED_DIRS,
    SLUG_RE,
    VALID_SOURCES,
    VALID_TYPES,
)
from .redact import _redact_frontmatter, redact_secrets


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


@dataclass(frozen=True, slots=True)
class SeedResult:
    """The created/skipped split returned by :meth:`Vault.seed`.

    ``created`` lists the vault-relative spine/dashboard paths written on this run;
    ``skipped`` lists the ones left untouched because they already existed (and
    ``force`` was not set). Empty content folders are not reported either way.
    """

    created: tuple[str, ...]
    skipped: tuple[str, ...]


class Vault:
    """Path-confined read/write facade over one vault, built from a frozen Config."""

    def __init__(self, config: Config) -> None:
        """Stores the config and caches the resolved absolute vault root."""
        self._config = config
        self._root = config.vault_path

    @property
    def root(self) -> Path:
        """Resolved absolute vault root (equals ``config.vault_path``)."""
        return self._root

    def schema_md(self) -> str | None:
        """Returns the vault's ``SCHEMA.md`` text.

        Curate passes this to the model as ``system_extra``, so pages are filed to the
        live per-type schema. A bare or unseeded vault has none, which is a valid state,
        and the contract :func:`thoth.llm.validate_file_plan` enforces does not depend
        on it.
        """
        path = self._root / "SCHEMA.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    # ---- seed the vault spine (idempotent provisioning) --------------------------

    def seed(self, *, force: bool = False) -> SeedResult:
        """Writes the packaged vault spine and dashboards, idempotently.

        Every packaged template is written to its path under the root, and the canonical
        empty content folders are created so the structure exists for Obsidian browsing.
        Existing spine files are left untouched unless ``force`` is set, so re-running
        over a live vault never clobbers an edited page. Folder creation is always
        ``exist_ok``.

        Args:
            force: Overwrite existing spine files with the packaged text.

        Returns:
            The template paths written on this run and the ones skipped.
        """
        from thoth.templates import iter_templates

        created: list[str] = []
        skipped: list[str] = []
        for name, text in iter_templates():
            absolute = self.resolve(name)
            if absolute.exists() and not force:
                skipped.append(name)
                continue
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(text, encoding="utf-8")
            created.append(name)

        for folder in SEED_DIRS:
            (self._root / folder).mkdir(parents=True, exist_ok=True)

        return SeedResult(created=tuple(created), skipped=tuple(skipped))

    # ---- path confinement (the security core) -----------------------------------

    def resolve(self, vault_relative_path: str) -> Path:
        """Validates and resolves a vault-relative path to an absolute path in root.

        Rejects the empty string, absolute paths, any ``..`` or ``.`` part, and any
        result landing outside the resolved root, so a symlink pointing out of the vault
        is caught. The path need not exist.

        Args:
            vault_relative_path: A POSIX-style path relative to the vault root.

        Returns:
            The absolute path inside the vault root.

        Raises:
            PathConfinementError: if the path is empty, absolute, carries a ``..`` or
                ``.`` segment, or resolves outside the root.
        """
        if not vault_relative_path:
            raise PathConfinementError("vault path must be a non-empty relative path")
        if PurePosixPath(vault_relative_path).is_absolute():
            raise PathConfinementError(
                f"vault path must be relative, not absolute: {vault_relative_path!r}"
            )
        # Inspect the raw segments. PurePosixPath silently drops '.' parts, so the
        # check must run on the original string, which also catches '' from a '//' run
        for segment in vault_relative_path.split("/"):
            if segment in ("..", ".", ""):
                raise PathConfinementError(
                    f"vault path may not contain {segment!r} segment: "
                    f"{vault_relative_path!r}"
                )

        candidate = self._root / vault_relative_path
        # Resolve follows symlinks in the existing prefix, so a symlinked directory
        # escaping the vault is caught, and normalises the missing tail lexically, so
        # the leaf need not exist yet
        resolved = candidate.resolve()
        resolved_root = self._root.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise PathConfinementError(
                f"vault path escapes the vault root: {vault_relative_path!r}"
            )
        return candidate

    def is_inside(self, vault_relative_path: str) -> bool:
        """Reports whether :meth:`resolve` would accept a path."""
        try:
            self.resolve(vault_relative_path)
        except PathConfinementError:
            return False
        return True

    # ---- slug / folder / type validation (no disk touch) -------------------------

    @staticmethod
    def validate_slug(slug: str) -> str:
        """Returns ``slug`` when it matches the slug grammar.

        Accepts lowercase alphanumeric groups joined by single hyphens, such as
        ``program-motion-controller``. Rejects uppercase, spaces, slashes, leading or
        trailing hyphens, doubled hyphens and the empty string.
        """
        if not SLUG_RE.fullmatch(slug):
            raise SlugError(f"invalid slug {slug!r}: must match {SLUG_RE.pattern}")
        return slug

    @staticmethod
    def validate_asset_filename(name: str) -> str:
        """Returns ``name`` when it matches the asset filename grammar.

        Accepts ``<slug>.<ext>`` with a lowercase slug and extension, and a compound
        extension such as the ``.excalidraw.md`` reconstruction from issue #68. Rejects
        a missing extension, ``..``, a leading dot, uppercase and spaces.
        """
        if not ASSET_SLUG_RE.fullmatch(name):
            raise SlugError(
                f"invalid asset filename {name!r}: must match {ASSET_SLUG_RE.pattern}"
            )
        return name

    @classmethod
    def _asset_rel(cls, asset_filename: str) -> str:
        """Validates an asset filename and returns its ``raw/assets/`` path.

        Raises:
            SlugError: on an invalid asset filename.
        """
        cls.validate_asset_filename(asset_filename)
        return f"raw/assets/{asset_filename}"

    @staticmethod
    def validate_folder_type(folder: str, page_type: str) -> None:
        """Validates that ``page_type`` may live in ``folder``.

        Args:
            folder: A top-level vault folder name.
            page_type: The frontmatter type value.

        Raises:
            SchemaError: when the folder is unknown, or the type is not permitted in
                it per :data:`~thoth.vault.FOLDER_TYPE_CONTRACT`.
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
        """Confines a path, then builds its ``obsidian://`` deep link.

        Confinement including the symlink check runs here, and the percent-encoding is
        delegated to the one canonical builder on :class:`~thoth.config.Config`.

        Raises:
            PathConfinementError: if the path escapes the vault root.
        """
        self.resolve(vault_relative_path)
        return self._config.obsidian_uri(vault_relative_path)

    # ---- read --------------------------------------------------------------------

    def read_page(self, vault_relative_path: str) -> Page:
        """Confines, reads, and splits a page into frontmatter and body.

        Args:
            vault_relative_path: Vault-relative path to a ``.md`` file.

        Returns:
            The parsed page.

        Raises:
            PathConfinementError: if the path escapes the vault root.
            VaultError: if the file does not exist.
        """
        absolute = self.resolve(vault_relative_path)
        if not absolute.is_file():
            raise VaultError(f"page does not exist: {vault_relative_path!r}")
        post = frontmatter.load(absolute)
        return Page(
            path=PurePosixPath(vault_relative_path).as_posix(),
            frontmatter=dict(post.metadata),
            body=post.content,
        )

    def page_exists(self, vault_relative_path: str) -> bool:
        """Reports whether a confined path exists as a file."""
        absolute = self.resolve(vault_relative_path)
        return absolute.is_file()

    def iter_folder_pages(self, folders: tuple[str, ...]) -> Iterator[tuple[str, Path]]:
        """Yields every ``*.md`` page under ``folders``.

        Folders are visited in the order given and pages within one in sorted filename
        order, which is the stable scan order the lexical search passes rank by. Missing
        folders are skipped silently.

        Args:
            folders: Vault-relative folder names to scan, in priority order.

        Yields:
            The vault-relative path and the absolute path to each file.
        """
        for folder in folders:
            directory = self._root / folder
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.md")):
                yield f"{folder}/{entry.name}", entry

    @staticmethod
    def body_sha256(body: str) -> str:
        """Returns the hex SHA-256 of the body text, the ``raw/`` idempotency key."""
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @classmethod
    def stored_body_sha256(cls, body: str) -> str:
        """Returns the ``sha256`` to stamp on a raw page for drift detection.

        The digest must equal what a reader re-derives from disk. ``python-frontmatter``
        normalises the parsed content by dropping the leading blank line and trailing
        whitespace, so the digest is taken over a round trip through the exact
        serialisation :meth:`_write_post` writes. Stamping the raw digest instead would
        make every body ending in a newline, which is the normal extractor case, report
        spurious drift.

        Args:
            body: The raw page body markdown, before redaction.

        Returns:
            The hex SHA-256 of the parse-stable, redacted body.
        """
        rendered = cls._render_page({}, redact_secrets(body))
        return cls.body_sha256(frontmatter.loads(rendered).content)

    @staticmethod
    def bytes_sha256(data: bytes) -> str:
        """Returns the hex SHA-256 of raw bytes, the binary-asset idempotency key."""
        return hashlib.sha256(data).hexdigest()

    def asset_exists(self, asset_filename: str) -> bool:
        """Reports whether ``raw/assets/<asset_filename>`` already exists.

        The filename is validated and confined first, so a malformed or escaping name is
        rejected rather than silently reported absent.

        Args:
            asset_filename: The asset filename to test.

        Returns:
            True when the confined path exists as a file, otherwise False.

        Raises:
            SlugError: on an invalid asset filename.
            PathConfinementError: if the destination escapes the vault root.
        """
        return self.resolve(self._asset_rel(asset_filename)).is_file()

    def asset_sha256(self, asset_filename: str) -> str:
        """Returns the hex SHA-256 of an existing asset's bytes.

        Ingest uses this to decide whether a re-uploaded binary is byte-identical, and
        so an idempotent skip, or a genuine change before calling :meth:`save_asset`.

        Args:
            asset_filename: The asset filename to digest.

        Returns:
            The hex digest of the asset's bytes.

        Raises:
            SlugError: on an invalid asset filename.
            PathConfinementError: if the destination escapes the vault root.
            VaultError: if the asset does not exist.
        """
        rel = self._asset_rel(asset_filename)
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
        """Validates, redacts, stamps and atomically writes a content page.

        Validates the folder-by-type contract, the slug grammar, the required common
        fields and the source value. Secrets are redacted from the body and from string
        frontmatter values. ``created`` is preserved on update and ``updated`` is always
        the run date.

        Args:
            folder: A top-level vault folder.
            slug: The page slug.
            frontmatter_in: Page frontmatter, carrying a valid type and source plus
                the other required common fields.
            body: The page body markdown.
            today: Date to stamp, defaulting to today.

        Returns:
            The vault-relative path written.

        Raises:
            SchemaError: on a folder or type mismatch, a missing required field, a
                bad type, or an invalid source.
            SlugError: on an invalid slug.
        """
        self.validate_slug(slug)
        meta = dict(frontmatter_in)
        page_type = meta.get("type")
        if not isinstance(page_type, str):
            raise SchemaError("frontmatter 'type' must be a string")
        self.validate_folder_type(folder, page_type)
        self._validate_common_fields(meta)
        if page_type != INBOX_TYPE:
            # Every content page carries the personal boolean (ADR 0013), defaulting
            # to work when the caller does not say otherwise
            meta.setdefault("personal", False)

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
        """Writes an immutable ``raw/<subdir>/<slug>.md`` source page.

        Stamps ``ingested`` and the parse-stable redacted digest, and redacts secrets
        from the body and from string frontmatter values.

        Args:
            subdir: A ``raw/`` subdirectory, excluding the binary-only ``assets``.
            slug: The raw page slug.
            frontmatter_in: Raw frontmatter such as ``source_url``. ``ingested`` and
                ``sha256`` are overwritten here.
            body: The raw page body markdown.
            today: Date to stamp as ``ingested``, defaulting to today.

        Returns:
            The vault-relative path written.

        Raises:
            SchemaError: if the subdir is ``assets`` or is not a known raw subdir.
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
        """Moves a downloaded binary into ``raw/assets/<asset_filename>``.

        The filename is validated and confined, and the bytes are moved verbatim rather
        than base64. An existing asset is never overwritten.

        Args:
            tmp_path: Path to the already-downloaded binary.
            asset_filename: The destination filename.

        Returns:
            The vault-relative path written.

        Raises:
            SlugError: on an invalid asset filename.
            PathConfinementError: if the destination escapes the vault root.
            VaultError: if the source is missing or the destination exists.
        """
        rel = self._asset_rel(asset_filename)
        destination = self.resolve(rel)
        if not tmp_path.is_file():
            raise VaultError(f"source asset does not exist: {tmp_path}")
        if destination.exists():
            raise VaultError(f"refusing to overwrite existing asset: {rel!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp_path, destination)
        return rel

    def remove_page(self, vault_relative_path: str) -> bool:
        """Deletes a confined path if it exists, idempotently.

        The path is confined first, so an absolute, ``..`` or symlink-escaping path is
        rejected rather than deleted. Used to drop a superseded ``inbox/`` hold once a
        deferred capture has been curated and the durable pages carry the content.

        Args:
            vault_relative_path: Vault-relative path to remove.

        Returns:
            True when a file was removed, False when nothing was there.

        Raises:
            PathConfinementError: if the path escapes the vault root.
        """
        absolute = self.resolve(vault_relative_path)
        if not absolute.is_file():
            return False
        absolute.unlink()
        return True

    # ---- navigation edits (append-only / idempotent) ----------------------------

    def append_log(self, action: str, subject: str, files: list[str]) -> None:
        """Appends a dated action block and the touched-file list to ``log.md``.

        Args:
            action: The log action, one of :data:`_LOG_ACTIONS`.
            subject: A short human-readable subject.
            files: Vault-relative paths touched by the action.

        Raises:
            SchemaError: if the action is not a known log action.
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

    # ---- internals ---------------------------------------------------------------

    def _validate_common_fields(self, meta: dict[str, object]) -> None:
        """Validates author-supplied common fields, the type and the source.

        ``created`` and ``updated`` are deliberately not required here. The caller
        supplies neither, because :meth:`write_page` stamps them, so requiring them
        pre-stamp would be wrong. An ``inbox`` hold is machinery and has its own field
        set, carrying ``sha256`` instead of ``tags`` (ADR 0013).
        """
        required = (
            _AUTHOR_REQUIRED_INBOX_FIELDS
            if meta.get("type") == INBOX_TYPE
            else _AUTHOR_REQUIRED_FIELDS
        )
        missing = [field for field in required if meta.get(field) in (None, "")]
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
        """Returns the ``created`` value of an existing page."""
        absolute = self.resolve(vault_relative_path)
        if not absolute.is_file():
            return None
        post = frontmatter.load(absolute)
        return post.metadata.get("created")

    @staticmethod
    def _render_page(meta: dict[str, object], body: str) -> str:
        """Serialises already-redacted frontmatter and body to the on-disk text.

        This is the single source of truth for a page's byte layout. The frontmatter
        block uses :func:`yaml.safe_dump` so the assembled key order survives, where
        ``frontmatter.dumps`` would re-sort it. The body gets one trailing newline.

        Args:
            meta: The already-redacted frontmatter mapping.
            body: The already-redacted body markdown.

        Returns:
            The exact text written to disk.
        """
        block = yaml.safe_dump(
            meta,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        stripped = body.rstrip("\n")
        return f"---\n{block}---\n\n{stripped}\n"

    def _write_post(
        self, vault_relative_path: str, meta: dict[str, object], body: str
    ) -> None:
        """Redacts, serialises and atomically writes a page.

        :meth:`_render_page` lays out the bytes. The file is written to a sibling
        ``.tmp`` and atomically replaced, so a crash never leaves a half-written page in
        the vault.
        """
        absolute = self.resolve(vault_relative_path)
        text = self._render_page(_redact_frontmatter(meta), redact_secrets(body))
        absolute.parent.mkdir(parents=True, exist_ok=True)
        tmp = absolute.with_name(absolute.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(absolute)
