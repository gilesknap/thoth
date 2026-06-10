"""Walk a file or directory tree into :class:`~thoth.ingest.Capture` items (issue #80).

This is the thin file/folder primitive underneath the ``thoth capture <path>...``
subcommand: point it at a single file or a directory and it yields one
:class:`~thoth.ingest.Capture` per eligible file, ready to feed through the existing
:meth:`thoth.ingest.Ingestor.ingest` pipeline. A Markdown/text file becomes a ``text``
capture (its bytes ARE the body, per the issue #57 upload path); an image/PDF/audio file
becomes a ``path`` capture the server reads; every capture carries ``source="import"``.

The walk is deliberately conservative for a vault import:

* **Machinery is skipped.** The ``.obsidian/``, ``.git/`` and ``_bases/`` directories
  and thoth's own spine files (``index.md`` / ``SCHEMA.md`` / ``log.md``) are never
  captured, so re-importing thoth's *own* vault does not re-ingest dashboards or log.
* **Unknown extensions are skipped, not guessed.** Unlike the Slack/MCP upload path --
  which defaults an unrecognised binary to an image (a phone photo is the common case)
  -- a bulk import skips a file whose extension is not a known text/image/PDF/audio
  kind, so a stray binary in the tree never triggers a surprise (paid) analyse call.
  Each skip is logged at debug so a ``--dry-run`` operator can see what was passed over.
* **Globs filter on the relative path.** ``include``/``exclude`` are :mod:`fnmatch`
  patterns matched against each file's path *relative to the walk root* (so
  ``drafts/*`` excludes a subtree and ``*.md`` includes only Markdown); ``--limit`` caps
  the total number of captures yielded across all roots.

Only the standard library, the shared :mod:`thoth.filetypes` extension sets, plus a
single deferred import of :data:`thoth.ingest.Capture` (inside the generator body, so
the heavy ``thoth.ingest`` module is not pulled in merely by importing this module) are
used, honouring the package's import-safety contract.
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

    Each entry in ``paths`` is a single file (yields at most one capture) or a directory
    (recursively walked in sorted order for a deterministic import). Markdown/text files
    become ``text`` captures (bytes read as the body, decoded with ``errors="replace"``
    so a stray non-UTF-8 byte never aborts the walk); image/PDF/audio files become
    ``path`` captures the ingest server reads. Every capture carries
    ``source="import"`` and the original ``filename``.

    Machinery directories (``.obsidian``/``.git``/``_bases``) and the spine files
    (``index.md``/``SCHEMA.md``/``log.md``) are always skipped; a file whose extension
    is not a known text/image/PDF/audio kind is skipped (logged at debug). ``include``
    and ``exclude`` are :mod:`fnmatch` globs matched against each file's path relative
    to its walk root; ``limit`` caps the total number of captures yielded.

    Args:
        paths: One or more files/directories to walk.
        include: If non-empty, only files whose relative path matches one of these globs
            are captured.
        exclude: Files whose relative path matches one of these globs are skipped (in
            addition to the always-skipped machinery/spine).
        limit: Stop after yielding this many captures (``None`` = no cap).

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

    A single file yields itself (relative path = its name). A directory is walked
    recursively in sorted order, pruning the machinery directories and the spine files
    so a thoth vault re-import never touches its own dashboards/log. ``relative`` is the
    POSIX path relative to ``root`` (the directory itself, or the file's parent), so the
    include/exclude globs match on a stable, separator-normalised key.
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

    ``exclude`` wins over ``include``: a path matching any exclude glob is dropped even
    if it also matches an include glob. An empty ``include`` means "include everything
    not excluded".
    """
    if any(fnmatch(relative, pattern) for pattern in exclude):
        return False
    if include and not any(fnmatch(relative, pattern) for pattern in include):
        return False
    return True


def _build_capture(file_path: Path, capture_cls: type[Capture]) -> Capture | None:
    """Build the :class:`~thoth.ingest.Capture` for ``file_path`` by its extension.

    A text/Markdown file is read inline as the capture ``text`` (decoded with
    ``errors="replace"`` so a stray byte never aborts the walk); an image/PDF/audio file
    becomes a ``path`` capture the ingest server reads itself. Returns ``None`` for an
    unrecognised extension (the caller skips it) so a bulk import never guesses a binary
    kind and triggers a surprise analyse spend.
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
