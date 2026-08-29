"""Shared file-extension vocabularies for capture-kind detection.

This module is a leaf that imports only the standard library. It holds the one set of
extensions that the ingest pipeline, the bulk-import walker and the Slack upload path
all classify by. Each extension is lowercase and carries no dot.
"""

from __future__ import annotations

__all__ = ["AUDIO_EXTS", "IMAGE_EXTS", "TEXT_EXTS"]

IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
"""Extensions that select an image capture. The server analyses it (issue #84)."""

AUDIO_EXTS: frozenset[str] = frozenset({"mp3", "wav", "m4a", "ogg", "flac"})
"""Extensions that select an audio capture. The server transcribes it."""

TEXT_EXTS: frozenset[str] = frozenset(
    {"md", "txt", "csv", "json", "org", "yaml", "yml", "log", "rst", "tsv"}
)
"""Extensions for a plain-text upload, such as markdown, a note or a data dump, whose
bytes are the text body.

``thoth.ingest._ext_kind`` checks this set before it falls back to the image default.
Without the check the pipeline classifies the upload as an image binary and drops its
text (issue #57)."""
