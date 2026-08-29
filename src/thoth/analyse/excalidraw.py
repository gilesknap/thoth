"""Deterministic assembly of the ``.excalidraw.md`` envelope (issue #68)."""

from __future__ import annotations

import json
from typing import Any

# The banner Obsidian-Excalidraw writes at the top of a parsed drawing, reproduced
# verbatim so a thoth-authored file is byte-shaped like a plugin-authored one
_EXCALIDRAW_BANNER = (
    "==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ⚠== "
    "You can decompress Drawing data with the command palette: 'Decompress current "
    "Excalidraw file'. For more info check in plugin settings under 'Saving'"
)


def _excalidraw_markdown(
    elements: list[dict[str, Any]], text_elements: list[dict[str, str]]
) -> str:
    """Assemble the ``.excalidraw.md`` envelope around the built scene elements.

    thoth builds the whole Obsidian-Excalidraw file format deterministically, trusting
    the model only for the node and connector structure: the frontmatter that marks the
    note as a parsed drawing, the plugin's switch-to-Excalidraw banner, a ``## Text
    Elements`` index carrying each label and its ``^id`` anchor for Obsidian search, and
    a ``%%``-commented ``## Drawing`` section holding the scene in a fenced ``json``
    block. The scene is stored uncompressed because the plugin reads both and plain JSON
    keeps the vault canonical as plain text.

    Args:
        elements: The fully-formed Excalidraw element dicts
        text_elements: ``{"id", "text"}`` rows for the text index

    Returns:
        The complete ``.excalidraw.md`` markdown string
    """
    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "thoth",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    scene_json = json.dumps(scene, indent=2)
    text_index = "".join(
        f"{row['text']} ^{row['id']}\n\n"
        for row in text_elements
        if row["text"].strip()
    )
    return (
        "---\n"
        "excalidraw-plugin: parsed\n"
        "tags: [excalidraw]\n"
        "---\n\n"
        f"{_EXCALIDRAW_BANNER}\n\n\n"
        "# Excalidraw Data\n\n"
        "## Text Elements\n"
        f"{text_index}"
        "%%\n"
        "## Drawing\n"
        "```json\n"
        f"{scene_json}\n"
        "```\n"
        "%%\n"
    )
