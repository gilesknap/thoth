"""Shared file-extension vocabularies for capture-kind detection.

Stdlib-only leaf module: the single source for the extension sets the ingest
pipeline, the bulk-import walker and the Slack upload path all classify by.
Extensions are listed lowercase with no dot.
"""

from __future__ import annotations

__all__ = ["AUDIO_EXTS", "IMAGE_EXTS", "TEXT_EXTS"]

IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
"""Extensions that select an image capture (analysed server-side, issue #84)."""

AUDIO_EXTS: frozenset[str] = frozenset({"mp3", "wav", "m4a", "ogg", "flac"})
"""Extensions that select an audio capture (transcribed server-side)."""

TEXT_EXTS: frozenset[str] = frozenset(
    {"md", "txt", "csv", "json", "org", "yaml", "yml", "log", "rst", "tsv"}
)
"""Plain-text uploads (markdown/notes/data dumps) whose bytes ARE the text body: read
the file rather than misclassifying it as an image binary and dropping its text
(issue #57). Checked before the image default in ``thoth.ingest._ext_kind``."""
