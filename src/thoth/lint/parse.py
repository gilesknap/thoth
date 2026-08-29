"""Pure markdown extractors for the lint scan (also unit-tested directly).

Inter-page link and embed token extraction, with code-fence suppression, plus the
``SCHEMA.md`` tag-taxonomy parser. Everything here is a pure function of the text it
is given, and nothing touches the vault.

Link style is OKF standard markdown (issue #189), so a page links with
``[text](path.md)`` and embeds an image with ``![alt](path)``. The extractors here
still recognise the legacy Obsidian ``[[wikilink]]`` and ``![[embed]]`` forms too, so
the link-graph checks stay correct during and after a migration, and a stray wiki
token a future capture emits still counts as a link. The dedicated
:func:`extract_wiki_links` and :func:`extract_wiki_embeds` feed the OKF style check
that flags those legacy tokens. An Obsidian Bases (``.base``) or Excalidraw
(``.excalidraw``) embed has no standard-markdown equivalent and legitimately stays in
``![[...]]`` form.
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

# A standard markdown inline link ``[text](target)`` (the OKF form, #189). The capture
# group is the raw target (a relative ``path.md``, maybe ``%20``-escaped or with a
# ``#anchor``). The negative lookbehind excludes image embeds ``![alt](target)``.
_MD_LINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")

# A standard markdown image embed ``![alt](target)`` (the OKF image form, #189).
_MD_IMAGE_RE: re.Pattern[str] = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# A legacy Obsidian wikilink token: [[target]], [[target|alias]], or [[target#heading]].
# The capture group is the raw inner text; helpers strip the alias / anchor. The
# negative lookbehind excludes ![[embeds]].
_WIKILINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[\[([^\[\]]+?)\]\]")

# A legacy Obsidian embed token: ![[asset.ext]] (the leading '!' distinguishes it from a
# wikilink). Still used for Bases (.base) and Excalidraw (.excalidraw) embeds.
_EMBED_RE: re.Pattern[str] = re.compile(r"\!\[\[([^\[\]]+?)\]\]")

# Fenced code spans (``` ... ``` or ~~~ ... ~~~) and inline code (`...`); their contents
# must not produce false-positive links/embeds (SPEC: code-fenced false positives).
_FENCE_RE: re.Pattern[str] = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

# The SCHEMA.md heading under which the tag taxonomy bullets live (SPEC Appendix).
_TAXONOMY_HEADING: str = "## Tag Taxonomy"


def _is_internal_link(target: str) -> bool:
    """Return ``True`` for a vault-internal link target.

    Excludes an external URL, such as ``https://...``, ``mailto:`` or the
    ``obsidian://`` scheme, and a pure same-page ``#section`` anchor. Neither resolves
    to a vault page, so neither belongs in the broken-link and orphan graph.
    """
    stripped = target.strip()
    if not stripped or stripped.startswith("#"):
        return False
    # A leading "scheme:" (http:, https:, mailto:, ...) marks an external URL.
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", stripped) is None


def _normalise_target(target: str) -> str:
    """Reduce a link target to the bare page handle (slug stem) it resolves to.

    Handles both a standard markdown href, such as ``../entities/jane.md#bio`` and
    possibly ``%20``-escaped, and a legacy wiki target such as ``dir/jane|Jane#bio``.
    The ``|alias``, the ``#anchor``, the directory, the ``.md`` suffix and the
    URL-escapes are all stripped, leaving the bare filename stem. Active vault slugs are
    unique, so that stem is enough to match a page's slug in the resolvable set.
    """
    head = target.split("|", 1)[0]
    head = head.split("#", 1)[0]
    name = PurePosixPath(unquote(head.strip())).name
    if name.endswith(".md"):
        name = name[: -len(".md")]
    return name.strip()


def _normalise_embed(target: str) -> str:
    """Reduce an embed target to the bare asset filename (extension kept).

    Handles a markdown image href such as ``../raw/assets/photo%20one.png`` and a legacy
    wiki embed such as ``photo.png|320``. The ``|size`` or ``#anchor`` suffix, the
    directory and the URL-escapes are stripped, leaving the filename the image-hygiene
    check matches against ``raw/assets/``.
    """
    head = target.split("|", 1)[0]
    head = head.split("#", 1)[0]
    return PurePosixPath(unquote(head.strip())).name


def extract_links(body: str) -> list[str]:
    """Return the raw target of every internal inter-page link in ``body``.

    Recognises the OKF standard markdown form ``[text](path.md)`` and the legacy
    Obsidian ``[[wikilink]]`` form, so the link graph stays correct across a migration.
    An external URL and a pure ``#anchor`` link are dropped, and the caller normalises
    each target with :func:`_normalise_target`. A link inside a fenced or inline code
    span is ignored, so a code example never produces a false positive.

    Args:
        body: The page body markdown.

    Returns:
        The raw target text of each link, in document order, markdown links first and
        then any residual wiki links.
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
    """Return the filename of every asset embed in ``body``.

    Recognises the OKF standard markdown image form ``![alt](path)`` and the legacy
    Obsidian ``![[asset.ext]]`` form, which Bases and Excalidraw embeds still use. The
    ``|size`` or ``#anchor`` suffix, the directory and the URL-escapes are stripped to
    the bare filename. An external image URL, and an embed inside a code span, are
    ignored.

    Args:
        body: The page body markdown.

    Returns:
        The embedded filenames, in document order, markdown images first and then any
        residual wiki embeds.
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
    """Return the raw inner text of every legacy ``[[wikilink]]``, for the OKF check.

    Only the OKF link-style check uses it, because a wiki link is non-portable and not
    the standard markdown form OKF requires. An ``![[embed]]`` is excluded by its
    leading ``!``, and a code-fenced token is ignored.
    """
    stripped = _FENCE_RE.sub("", body)
    return [match.group(1).strip() for match in _WIKILINK_RE.finditer(stripped)]


def extract_wiki_embeds(body: str) -> list[str]:
    """Return the raw inner text of every legacy ``![[embed]]``, for the OKF check.

    The OKF link-style check uses it. That check flags a wiki *image* embed but exempts
    the Bases (``.base``) and Excalidraw (``.excalidraw``) embeds with no
    standard-markdown equivalent. A code-fenced token is ignored.
    """
    stripped = _FENCE_RE.sub("", body)
    return [match.group(1).strip() for match in _EMBED_RE.finditer(stripped)]


def parse_taxonomy_tags(schema_text: str) -> set[str]:
    """Return the tag set listed under ``## Tag Taxonomy`` in ``SCHEMA.md``.

    The taxonomy section (SPEC Appendix) lists tags as bullet lines of the form ``-
    <label>: tag-a, tag-b, tag-c``. This collects every comma-separated tag after the
    first colon on each bullet, between the ``## Tag Taxonomy`` heading and the next
    ``##`` heading. A label-less bullet such as ``- tag-a, tag-b`` is accepted too, and
    the result is an empty set when the heading is absent.

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
