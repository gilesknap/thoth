"""Pure markdown extractors for the lint scan, also unit-tested directly.

Link and embed extraction with code-fence suppression, plus the tag-taxonomy parser.
Everything here is a pure function of the text it is given and nothing touches the
vault.

Link style is OKF standard markdown (issue #189). The extractors still recognise the
legacy Obsidian forms too, so the link-graph checks stay correct across a migration and
a stray wiki token from a future capture still counts as a link.

The dedicated wiki extractors feed the style check that flags those legacy tokens. Bases
and Excalidraw embeds have no standard-markdown equivalent and legitimately stay in wiki
form.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote

__all__ = [
    "parse_taxonomy_tags",
    "extract_links",
    "extract_embeds",
    "extract_wiki_links",
    "extract_wiki_embeds",
]

# A standard markdown inline link, the OKF form (#189). The capture group is the raw
# target, possibly escaped or anchored, and the lookbehind excludes image embeds
_MD_LINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")

# A standard markdown image embed, the OKF image form (#189)
_MD_IMAGE_RE: re.Pattern[str] = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# A legacy wikilink token, plain, aliased or anchored. The capture group is the raw
# inner text, which the helpers strip, and the lookbehind excludes embeds
_WIKILINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[\[([^\[\]]+?)\]\]")

# A legacy embed token, distinguished from a wikilink by the leading bang. Still used
# for bases and excalidraw embeds
_EMBED_RE: re.Pattern[str] = re.compile(r"\!\[\[([^\[\]]+?)\]\]")

# Fenced and inline code spans, whose contents must not produce false-positive links or
# embeds
_FENCE_RE: re.Pattern[str] = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

# The schema heading the tag taxonomy bullets live under (SPEC appendix)
_TAXONOMY_HEADING: str = "## Tag Taxonomy"


def _is_internal_link(target: str) -> bool:
    """Reports whether a link target is vault-internal.

    External URLs and pure same-page anchors are excluded, because neither resolves to a
    vault page and so neither belongs in the link graph.
    """
    stripped = target.strip()
    if not stripped or stripped.startswith("#"):
        return False
    # A leading scheme marks an external URL
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", stripped) is None


def _normalise_target(target: str) -> str:
    """Reduces a link target to the bare page handle it resolves to.

    Handles both a standard markdown href, possibly escaped, and a legacy wiki target.
    The alias, anchor, directory, suffix and escapes are all stripped, leaving the bare
    stem. Active vault slugs are unique, so the stem alone is enough to match a page in
    the resolvable set.
    """
    head = target.split("|", 1)[0]
    head = head.split("#", 1)[0]
    name = PurePosixPath(unquote(head.strip())).name
    if name.endswith(".md"):
        name = name[: -len(".md")]
    return name.strip()


def _normalise_embed(target: str) -> str:
    """Reduces an embed target to the bare asset filename, keeping the extension.

    Handles a markdown image href and a legacy wiki embed. The size or anchor suffix,
    directory and escapes are stripped, leaving the filename the image-hygiene check
    matches against the asset folder.
    """
    head = target.split("|", 1)[0]
    head = head.split("#", 1)[0]
    return PurePosixPath(unquote(head.strip())).name


def extract_links(body: str) -> list[str]:
    """Returns the raw target of every internal inter-page link in a body.

    Recognises the OKF standard form and the legacy wiki form, so the link graph stays
    correct across a migration. External URLs and pure anchors are dropped, and the
    caller normalises each target. Links inside code spans are ignored, so code examples
    never produce false positives.

    Args:
        body: The page body markdown.

    Returns:
        The raw target of each link in document order, markdown first then any
        residual wiki links.
    """
    stripped = _FENCE_RE.sub("", body)
    targets = [
        match.group(1).strip()
        for match in _MD_LINK_RE.finditer(stripped)
        if _is_internal_link(match.group(1))
    ]
    targets.extend(match.group(1).strip() for match in _WIKILINK_RE.finditer(stripped))
    return targets


def extract_embeds(body: str) -> list[str]:
    """Returns the filename of every asset embed in a body.

    Recognises the OKF standard image form and the legacy wiki form, still used for
    bases and excalidraw. Suffixes, directories and escapes are stripped to the bare
    filename, and external URLs and code-fenced embeds are ignored.

    Args:
        body: The page body markdown.

    Returns:
        The embedded filenames in document order, markdown first then any residual
        wiki embeds.
    """
    stripped = _FENCE_RE.sub("", body)
    names = [
        _normalise_embed(match.group(1))
        for match in _MD_IMAGE_RE.finditer(stripped)
        if _is_internal_link(match.group(1))
    ]
    names.extend(
        _normalise_embed(match.group(1)) for match in _EMBED_RE.finditer(stripped)
    )
    return names


def extract_wiki_links(body: str) -> list[str]:
    """Returns the raw inner text of every legacy wikilink, for the style check.

    Used only by the OKF link-style check, because a wiki link is non-portable and not
    the standard form OKF requires. Embeds are excluded by their leading bang, and
    code-fenced tokens are ignored.
    """
    stripped = _FENCE_RE.sub("", body)
    return [match.group(1).strip() for match in _WIKILINK_RE.finditer(stripped)]


def extract_wiki_embeds(body: str) -> list[str]:
    """Returns the raw inner text of every legacy embed, for the style check.

    The check flags wiki image embeds but exempts the bases and excalidraw embeds that
    have no standard-markdown equivalent. Code-fenced tokens are ignored.
    """
    stripped = _FENCE_RE.sub("", body)
    return [match.group(1).strip() for match in _EMBED_RE.finditer(stripped)]


def parse_taxonomy_tags(schema_text: str) -> set[str]:
    """Returns the tag set listed under the taxonomy heading in the schema.

    The section lists tags as bullets of a label then comma-separated tags, and this
    collects every tag after the first colon on each bullet, between the heading and the
    next one. A label-less bullet is accepted too.

    Args:
        schema_text: The full schema text.

    Returns:
        The taxonomy tags, empty when the heading is absent.
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
