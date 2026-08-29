"""Shared file-extension vocabularies for capture-kind detection.

A standard-library-only leaf module, and the single source for the extension sets that
the ingest pipeline, the bulk-import walker and the Slack upload path classify by. List
every extension in lowercase with no dot.
"""

from __future__ import annotations

__all__ = ["AUDIO_EXTS", "IMAGE_EXTS", "TEXT_EXTS"]

IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
"""Extensions that select an image capture, analysed server-side (issue #84)."""

AUDIO_EXTS: frozenset[str] = frozenset({"mp3", "wav", "m4a", "ogg", "flac"})
"""Extensions that select an audio capture, transcribed server-side."""

TEXT_EXTS: frozenset[str] = frozenset(
    {"md", "txt", "csv", "json", "org", "yaml", "yml", "log", "rst", "tsv"}
)
"""Plain-text uploads whose bytes ARE the text body: markdown, a note, a data dump. Read
the file, rather than misclassify it as an image binary and drop its text (issue #57).
``thoth.ingest._ext_kind`` checks this set before the image default."""
