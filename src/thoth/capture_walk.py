"""Walk a file or directory tree into :class:`~thoth.ingest.Capture` items (issue #80).

This is the thin file and folder primitive underneath the ``thoth capture <path>...``
subcommand. Point it at a single file or a directory and it yields one
:class:`~thoth.ingest.Capture` per eligible file, ready for the existing
:meth:`thoth.ingest.Ingestor.ingest` pipeline. A Markdown or text file becomes a
``text`` capture, its bytes being the body as on the issue #57 upload path, an image,
PDF or audio file becomes a ``path`` capture the server reads, and every capture carries
``source="import"``.

The walk is deliberately conservative for a vault import:

* **Machinery is skipped.** The ``.obsidian/``, ``.git/`` and ``_bases/`` directories
  and thoth's own spine files (``index.md``, ``SCHEMA.md``, ``log.md``) are never
  captured, so re-importing thoth's *own* vault re-ingests neither dashboards nor log. *
  **Unknown extensions are skipped, not guessed.** The Slack and MCP upload path
  defaults an unrecognised binary to an image, a phone photo being the common case, but
  a bulk import instead skips a file whose extension is not a known text, image, PDF or
  audio kind, so a stray binary never triggers a surprise paid analyse call. Each skip
  is logged at debug, so a ``--dry-run`` operator sees what was passed over. * **Globs
  filter on the relative path.** ``include`` and ``exclude`` are :mod:`fnmatch` patterns
  matched against each file's path *relative to the walk root*, so ``drafts/*`` excludes
  a subtree and ``*.md`` includes only Markdown, and ``--limit`` caps the total captures
  yielded across all roots.

This module uses only the standard library, the shared :mod:`thoth.filetypes` extension
sets and one deferred import of :data:`thoth.ingest.Capture`, placed inside the
generator body so importing this module does not pull in the heavy ``thoth.ingest``,
honouring the package's import-safety contract.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from thoth.filetypes import AUDIO_EXTS as _AUDIO_EXTS
from thoth.filetypes import IMAGE_EXTS as _IMAGE_EXTS
from thoth.filetypes import TEXT_EXTS as _TEXT_EXTS

if TYPE_CHECKING:
    from thoth.ingest import Capture

__all__ = ["walk_captures"]

logger = logging.getLogger(__name__)

# The frontmatter ``source`` value stamped on every imported page (added to
# :data:`thoth.vault.VALID_SOURCES`). Kept here so the walker is self-contained.
IMPORT_SOURCE: str = "import"

# Directory names that are vault machinery, never content: the Obsidian config, the git
# metadata, and the Bases dashboard sources. A directory with one of these names (at any
# depth) is pruned from the walk entirely.
_SKIP_DIRS: frozenset[str] = frozenset({".obsidian", ".git", "_bases"})

# thoth's own spine files: a static Home dashboard, the schema, and the activity log.
# These are managed by ``thoth init`` / the pipeline, not captured as content, so
# importing a thoth vault never re-ingests them.
_SPINE_FILES: frozenset[str] = frozenset({"index.md", "SCHEMA.md", "log.md"})

# A bulk import is conservative: a file whose extension is in none of the known sets is
# skipped (logged), rather than defaulting to an image like the Slack/MCP upload path
# does -- so a stray binary never triggers a surprise analyse spend (#80).
_PDF_EXTS: frozenset[str] = frozenset({"pdf"})


def walk_captures(
    paths: Sequence[Path],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    limit: int | None = None,
) -> Iterator[Capture]:
    """Yield one :class:`~thoth.ingest.Capture` per eligible file under ``paths``.

    Each entry in ``paths`` is a single file, yielding at most one capture, or a
    directory, walked recursively in sorted order for a deterministic import. A Markdown
    or text file becomes a ``text`` capture whose bytes are read as the body and decoded
    with ``errors="replace"``, so a stray non-UTF-8 byte never aborts the walk, while an
    image, PDF or audio file becomes a ``path`` capture the ingest server reads. Every
    capture carries ``source="import"`` and the original ``filename``.

    The module docstring gives the skip and glob rules this applies.

    Args:
        paths: One or more files or directories to walk.
        include: When non-empty, captures only files whose relative path matches one of
            these globs.
        exclude: Skips files whose relative path matches one of these globs, on top of
            the always-skipped machinery and spine.
        limit: Stop after yielding this many captures. ``None`` means no cap.

    Yields:
        A :class:`~thoth.ingest.Capture` for each eligible file, in walk order.
    """
    from thoth.ingest import Capture

    emitted = 0
    for root in paths:
        for file_path, relative in _iter_files(root):
            if limit is not None and emitted >= limit:
                return
            if not _passes_globs(relative, include=include, exclude=exclude):
                logger.debug("capture walk: skip %s (glob filter)", relative)
                continue
            capture = _build_capture(file_path, Capture)
            if capture is None:
                logger.debug("capture walk: skip %s (unknown kind)", relative)
                continue
            emitted += 1
            yield capture


def _iter_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(file_path, relative_path)`` for each file under ``root`` in walk order.

    A single file yields itself, its relative path being its name. A directory is walked
    recursively in sorted order, pruning the machinery directories and the spine files,
    so a thoth vault re-import never touches its own dashboards or log. ``relative`` is
    the POSIX path relative to ``root``, the directory itself or the file's parent, so
    the include and exclude globs match on a stable, separator-normalised key.
    """
    if root.is_file():
        if root.name not in _SPINE_FILES:
            yield root, root.name
        return
    if not root.is_dir():
        logger.debug("capture walk: skip %s (not a file or directory)", root)
        return
    for file_path in _walk_dir(root):
        if file_path.name in _SPINE_FILES:
            continue
        relative = file_path.relative_to(root).as_posix()
        yield file_path, relative


def _walk_dir(directory: Path) -> Iterator[Path]:
    """Recursively yield files under ``directory`` (sorted), pruning machinery dirs."""
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.is_dir():
            if entry.name in _SKIP_DIRS:
                continue
            yield from _walk_dir(entry)
        elif entry.is_file():
            yield entry


def _passes_globs(
    relative: str, *, include: Sequence[str], exclude: Sequence[str]
) -> bool:
    """Return whether ``relative`` survives the include/exclude glob filters.

    ``exclude`` wins over ``include``, so a path matching any exclude glob is dropped
    even when it also matches an include glob. An empty ``include`` means "include
    everything not excluded".
    """
    if any(fnmatch(relative, pattern) for pattern in exclude):
        return False
    if include and not any(fnmatch(relative, pattern) for pattern in include):
        return False
    return True


def _build_capture(file_path: Path, capture_cls: type[Capture]) -> Capture | None:
    """Build the :class:`~thoth.ingest.Capture` for ``file_path`` by its extension.

    A text or Markdown file is read inline as the capture ``text``, decoded with
    ``errors="replace"`` so a stray byte never aborts the walk, while an image, PDF or
    audio file becomes a ``path`` capture the ingest server reads itself. Returns
    ``None`` for an unrecognised extension, which the caller skips, so a bulk import
    never guesses a binary kind and triggers a surprise analyse spend.
    """
    ext = file_path.suffix.lstrip(".").lower()
    if ext in _TEXT_EXTS:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        return capture_cls(text=text, source=IMPORT_SOURCE, filename=file_path.name)
    if ext in _IMAGE_EXTS or ext in _AUDIO_EXTS or ext in _PDF_EXTS:
        return capture_cls(
            path=file_path, source=IMPORT_SOURCE, filename=file_path.name
        )
    return None
