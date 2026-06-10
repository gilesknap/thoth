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
    _LOG_ACTIONS,
    ASSET_SLUG_RE,
    FOLDER_TYPE_CONTRACT,
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

    # ---- seed the vault spine (idempotent provisioning) --------------------------

    def seed(self, *, force: bool = False) -> SeedResult:
        """Write the packaged vault spine + dashboards into this vault (idempotent).

        Writes every packaged template (``index.md``, ``SCHEMA.md``, ``log.md``, and
        ``_bases/*.base``) to its path under the vault root and creates the canonical
        empty content folders (:data:`~thoth.vault.SEED_DIRS`: ``entities/``,
        ``notes/``, ``memories/``, ``actions/``, ``inbox/`` and the ``raw/`` subdirs)
        so the structure exists for Obsidian browsing. Existing spine files are left
        untouched unless ``force`` is set, so re-running over a live vault never
        clobbers an edited spine page; the empty-folder creation is always
        ``exist_ok``.

        Args:
            force: Overwrite existing spine/dashboard files with the packaged text.

        Returns:
            A :class:`SeedResult` splitting the vault-relative template paths into the
            ones ``created`` on this run and the ones ``skipped`` (already present and
            ``force`` not set).
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
        if PurePosixPath(vault_relative_path).is_absolute():
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

        candidate = self._root / vault_relative_path
        # Path.resolve follows symlinks in the existing prefix (so a symlinked
        # directory that escapes the vault is caught) and normalises the non-existent
        # tail lexically, so the leaf need not exist yet.
        resolved = candidate.resolve()
        resolved_root = self._root.resolve()
        if not resolved.is_relative_to(resolved_root):
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
        """Return ``slug`` if it matches the slug grammar, else raise SlugError.

        Accepts lowercase alphanumeric groups joined by single hyphens (for example
        ``program-motion-controller``, per :data:`~thoth.vault.SLUG_RE`); rejects
        uppercase, spaces, slashes, leading or trailing hyphens, doubled hyphens, and
        the empty string.
        """
        if not SLUG_RE.fullmatch(slug):
            raise SlugError(f"invalid slug {slug!r}: must match {SLUG_RE.pattern}")
        return slug

    @staticmethod
    def validate_asset_filename(name: str) -> str:
        """Return ``name`` if it matches the asset grammar, else raise SlugError.

        Accepts ``<slug>.<ext>`` with a lowercase slug and lowercase extension (for
        example ``motor-control-diagram-e4a408.png``, per
        :data:`~thoth.vault.ASSET_SLUG_RE`), as well as a compound extension (for
        example ``motor-control-diagram-e4a408.excalidraw.md``, the editable
        Excalidraw reconstruction from issue #68); rejects a missing extension, ``..``,
        a leading dot, uppercase, and spaces.
        """
        if not ASSET_SLUG_RE.fullmatch(name):
            raise SlugError(
                f"invalid asset filename {name!r}: must match {ASSET_SLUG_RE.pattern}"
            )
        return name

    @classmethod
    def _asset_rel(cls, asset_filename: str) -> str:
        """Validate an asset filename and return its ``raw/assets/`` relative path.

        Raises:
            SlugError: on an invalid asset filename.
        """
        cls.validate_asset_filename(asset_filename)
        return f"raw/assets/{asset_filename}"

    @staticmethod
    def validate_folder_type(folder: str, page_type: str) -> None:
        """Validate that ``page_type`` may live in ``folder``.

        Args:
            folder: A top-level vault folder name (key of
                :data:`~thoth.vault.FOLDER_TYPE_CONTRACT`).
            page_type: The frontmatter ``type`` value.

        Raises:
            SchemaError: if ``folder`` is not a known folder, or ``page_type`` is not
                permitted in that folder per :data:`~thoth.vault.FOLDER_TYPE_CONTRACT`.
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
        post = frontmatter.load(absolute)
        return Page(
            path=PurePosixPath(vault_relative_path).as_posix(),
            frontmatter=dict(post.metadata),
            body=post.content,
        )

    def page_exists(self, vault_relative_path: str) -> bool:
        """Return ``True`` if a confined ``vault_relative_path`` exists as a file."""
        absolute = self.resolve(vault_relative_path)
        return absolute.is_file()

    def iter_folder_pages(self, folders: tuple[str, ...]) -> Iterator[tuple[str, Path]]:
        """Yield ``(rel, absolute)`` for every ``*.md`` page under ``folders``.

        Folders are visited in the given order and pages within a folder in sorted
        filename order -- the stable scan order the lexical search passes rank by.
        Missing folders are skipped silently.

        Args:
            folders: Vault-relative folder names to scan, in priority order.

        Yields:
            ``(rel, absolute)`` pairs: the vault-relative ``folder/name.md`` path and
            the absolute :class:`~pathlib.Path` to the file.
        """
        for folder in folders:
            directory = self._root / folder
            if not directory.is_dir():
                continue
            for entry in sorted(directory.glob("*.md")):
                yield f"{folder}/{entry.name}", entry

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
        return self.resolve(self._asset_rel(asset_filename)).is_file()

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
        """Validate, redact, stamp, and atomically write a curated/life-admin page.

        Validates the folder-by-type contract, the slug grammar, the required common
        fields, and the ``source`` value; redacts secrets from the body and string
        frontmatter values; stamps ``created`` (preserved on update) and ``updated``
        (always the run date); then writes ``<folder>/<slug>.md`` atomically.

        Args:
            folder: A top-level vault folder (key of
                :data:`~thoth.vault.FOLDER_TYPE_CONTRACT`).
            slug: The page slug (validated by :meth:`validate_slug`).
            frontmatter_in: The page frontmatter; must contain a valid ``type`` and
                ``source`` and the other :data:`~thoth.vault.REQUIRED_COMMON_FIELDS`.
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
            subdir: A ``raw/`` subdirectory in :data:`~thoth.vault.RAW_SUBDIRS`,
                excluding ``assets`` (which is binary-only -- use :meth:`save_asset`).
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
        post = frontmatter.load(absolute)
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
                :func:`~thoth.vault.redact._redact_frontmatter`).
            body: The redacted body markdown (already passed through
                :func:`~thoth.vault.redact_secrets`).

        Returns:
            The exact text written to disk for this page.
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
