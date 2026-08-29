"""Walks a file or directory tree into capture items (issue #80).

The thin primitive underneath ``thoth capture <path>...``. Point it at a file or a
directory and it yields one capture per eligible file, ready for the existing pipeline.
A text file becomes a text capture whose bytes are the body, a binary becomes a path
capture the server reads, and every capture carries the import source. The walk is
deliberately conservative for a vault import:

* **Machinery is skipped.** The Obsidian, git and Bases directories, and thoth's own
  spine files, are never captured, so re-importing thoth's own vault does not re-ingest
  its dashboards or its log.
* **Unknown extensions are skipped, not guessed.** The Slack upload path defaults an
  unrecognised binary to an image, because a phone photo is the common case, but a bulk
  import skips it so a stray binary never triggers a surprise paid analyse call. Each
  skip is logged at debug, so a dry run shows what was passed over.
* **Globs filter on the relative path.** Include and exclude match each file's path
  relative to its walk root, and the limit caps the total yielded across all roots.

Only the standard library, the shared extension sets, and a deferred import of the
capture type are used, honouring the package's import-safety contract.
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

# The source value stamped on every imported page, kept here so the walker is
# self-contained
IMPORT_SOURCE: str = "import"

# Directory names that are vault machinery rather than content. One of these at any
# depth is pruned from the walk entirely
_SKIP_DIRS: frozenset[str] = frozenset({".obsidian", ".git", "_bases"})

# thoth's own spine files, managed by init and the pipeline rather than captured as
# content, so importing a thoth vault never re-ingests them
_SPINE_FILES: frozenset[str] = frozenset({"index.md", "SCHEMA.md", "log.md"})

# A bulk import is conservative: an extension in none of the known sets is skipped and
# logged rather than defaulting to an image, so a stray binary never triggers a surprise
# analyse spend (#80)
_PDF_EXTS: frozenset[str] = frozenset({"pdf"})


def walk_captures(
    paths: Sequence[Path],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    limit: int | None = None,
) -> Iterator[Capture]:
    """Yields one capture per eligible file under the given paths.

    Each entry is a single file, yielding at most one capture, or a directory walked
    recursively in sorted order for a deterministic import. A text file becomes a text
    capture, decoded replacing bad bytes so a stray non-UTF-8 byte never aborts the
    walk.

    A binary becomes a path capture the server reads. Every capture carries the import
    source and the original filename.

    Machinery directories and spine files are always skipped, and a file whose extension
    is not a known kind is skipped and logged.

    Args:
        paths: One or more files or directories to walk.
        include: When non-empty, only relative paths matching one of these globs are
            captured.
        exclude: Relative paths matching one of these globs are skipped, on top of the
            always-skipped machinery.
        limit: Stop after this many captures, or None for no cap.

    Yields:
        A capture for each eligible file, in walk order.
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
    """Yields the absolute and relative path of each file under a root.

    A single file yields itself. A directory is walked recursively in sorted order,
    pruning the machinery directories and spine files so a vault re-import never touches
    its own dashboards or log. The relative path uses POSIX separators, so the globs
    match on a stable normalised key.
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
    """Recursively yields the files under a directory, sorted, pruning machinery."""
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
    """Reports whether a relative path survives the include and exclude globs.

    Exclude wins, so a path matching any exclude glob is dropped even when it also
    matches an include. An empty include means everything not excluded.
    """
    if any(fnmatch(relative, pattern) for pattern in exclude):
        return False
    if include and not any(fnmatch(relative, pattern) for pattern in include):
        return False
    return True


def _build_capture(file_path: Path, capture_cls: type[Capture]) -> Capture | None:
    """Builds the capture for one file, chosen by its extension.

    A text file is read inline as the capture body, decoded replacing bad bytes so a
    stray byte never aborts the walk. A binary becomes a path capture the server reads
    itself. An unrecognised extension returns None and the caller skips it, so a bulk
    import never guesses a binary kind and triggers a surprise analyse spend.
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
