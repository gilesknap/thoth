"""Pure markdown extractors for the lint scan (also unit-tested directly).

Wikilink / embed token extraction (with code-fence suppression) and the
``SCHEMA.md`` tag-taxonomy parser. Everything here is a pure function of the
text it is given; nothing touches the vault.
"""

from __future__ import annotations

import re

__all__ = [
    "parse_taxonomy_tags",
    "extract_wikilinks",
    "extract_embeds",
]

# A wikilink token: [[target]], [[target|alias]], or [[target#heading]]. The capture
# group is the raw inner text; the helper strips the alias / anchor to the bare target.
_WIKILINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[\[([^\[\]]+?)\]\]")

# An embed token: ![[asset.ext]] (the leading '!' distinguishes it from a wikilink).
_EMBED_RE: re.Pattern[str] = re.compile(r"\!\[\[([^\[\]]+?)\]\]")

# Fenced code spans (``` ... ``` or ~~~ ... ~~~) and inline code (`...`); their contents
# must not produce false-positive wikilinks/embeds (SPEC: code-fenced false positives).
_FENCE_RE: re.Pattern[str] = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

# The SCHEMA.md heading under which the tag taxonomy bullets live (SPEC Appendix).
_TAXONOMY_HEADING: str = "## Tag Taxonomy"


def _normalise_target(target: str) -> str:
    """Strip the ``|alias`` and ``#anchor`` parts and trim a wikilink target."""
    head = target.split("|", 1)[0]
    head = head.split("#", 1)[0]
    return head.strip()


def extract_wikilinks(body: str) -> list[str]:
    """Return the bare targets of every ``[[wikilink]]`` in ``body``.

    Recognises ``[[target]]``, ``[[target|alias]]`` and ``[[target#heading]]``; the
    alias and anchor portions are *not* stripped here (the caller normalises). An
    ``![[embed]]`` is *not* a wikilink (the leading ``!`` is excluded). Links inside
    fenced or inline code spans are ignored so code examples never produce false
    positives.

    Args:
        body: The page body markdown.

    Returns:
        The raw inner text of each wikilink, in document order.
    """
    stripped = _FENCE_RE.sub("", body)
    return [match.group(1).strip() for match in _WIKILINK_RE.finditer(stripped)]


def extract_embeds(body: str) -> list[str]:
    """Return the filenames of every ``![[asset.ext]]`` embed in ``body``.

    Only embeds (the ``![[...]]`` form, marked by the leading ``!``) are returned; plain
    ``[[wikilinks]]`` are ignored. Any ``|alias`` / ``#anchor`` suffix is stripped.
    Embeds inside fenced or inline code spans are ignored.

    Args:
        body: The page body markdown.

    Returns:
        The embedded filenames, in document order.
    """
    stripped = _FENCE_RE.sub("", body)
    return [_normalise_target(match.group(1)) for match in _EMBED_RE.finditer(stripped)]


def parse_taxonomy_tags(schema_text: str) -> set[str]:
    """Return the tag set listed under ``## Tag Taxonomy`` in ``SCHEMA.md``.

    The taxonomy section (SPEC Appendix) lists tags as bullet lines of the form
    ``- <label>: tag-a, tag-b, tag-c``; this collects every comma-separated tag after
    the first colon on each bullet, between the ``## Tag Taxonomy`` heading and the next
    ``##`` heading. A label-less bullet (``- tag-a, tag-b``) is also accepted. The
    result is an empty set if the heading is absent.

    Args:
        schema_text: The full ``SCHEMA.md`` text.

    Returns:
        The set of taxonomy tag strings.
    """
    lines = schema_text.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if line.strip() == _TAXONOMY_HEADING
        )
    except StopIteration:
        return set()
    tags: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        bullet = stripped[2:]
        payload = bullet.split(":", 1)[1] if ":" in bullet else bullet
        for token in payload.split(","):
            tag = token.strip()
            if tag:
                tags.add(tag)
    return tags
