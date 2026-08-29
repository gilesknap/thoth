"""Link-graph checks 1, 2, 11 and 14: orphans, broken links, images and style.

Check 1 is orphan pages, check 2 broken links, check 11 image hygiene and check 14
OKF link style. Each is a pure function over the parsed pages that
:class:`thoth.lint.LintEngine` hands it, the engine's thin ``check_*`` methods
gathering the pages and asset filename sets and delegating here.
"""

from __future__ import annotations

from .model import Finding, Severity, _finding, _Page
from .parse import (
    _normalise_embed,
    _normalise_target,
    extract_embeds,
    extract_links,
    extract_wiki_embeds,
    extract_wiki_links,
)

# The raw subdirectory holding binary assets (image-hygiene check 11).
_ASSETS_DIR: str = "raw/assets"

# Wiki embeds that legitimately stay in ``![[...]]`` form: Obsidian Bases views and
# Excalidraw drawings have no standard-markdown equivalent (issue #189), so the OKF
# link-style check (14) exempts them.
_EXEMPT_EMBED_SUFFIXES: tuple[str, ...] = (".base", ".excalidraw")


def _check_orphans(pages: list[_Page]) -> list[Finding]:
    """Flag curated reference pages with zero inbound links (check 1)."""
    inbound = _inbound_targets(pages)
    findings: list[Finding] = []
    for page in pages:
        handles = {page.slug, *page.aliases}
        if handles & inbound:
            continue
        findings.append(
            _finding(
                1,
                "orphan",
                Severity.ORPHAN,
                page.path,
                "reference page has no inbound links",
            )
        )
    return findings


def _check_broken_links(pages: list[_Page]) -> list[Finding]:
    """Flag ``[text](path.md)`` links resolving to no page, honouring aliases (check 2).

    Recognises both the OKF standard markdown link form and any residual
    ``[[wikilink]]``, since the extractor unions both. A target resolves when its bare
    stem matches a page's slug, its full path, or one of its aliases.
    """
    resolvable = _resolvable_targets(pages)
    findings: list[Finding] = []
    for page in pages:
        for target in extract_links(page.body):
            if _normalise_target(target) in resolvable:
                continue
            findings.append(
                _finding(
                    2,
                    "broken-link",
                    Severity.BROKEN,
                    page.path,
                    f"link [{target}] resolves to no page",
                )
            )
    return findings


def _check_link_style(pages: list[_Page]) -> list[Finding]:
    """Flag a legacy Obsidian wiki link or embed, since OKF wants markdown (check 14).

    A ``[[wikilink]]`` is non-portable and not the ``[text](path.md)`` form OKF requires
    (issue #189), so each is flagged ``Severity.STYLE``. A wiki *image* embed such as
    ``![[photo.png]]`` is likewise flagged in favour of ``![alt](path)``, but a Bases
    (``.base``) or Excalidraw (``.excalidraw``) embed is exempt, having no markdown
    equivalent.
    """
    findings: list[Finding] = []
    for page in pages:
        for target in extract_wiki_links(page.body):
            findings.append(
                _finding(
                    14,
                    "wiki-link",
                    Severity.STYLE,
                    page.path,
                    f"Obsidian wiki link [[{target}]] -- use a standard markdown "
                    "[text](path.md) link (OKF)",
                )
            )
        for target in extract_wiki_embeds(page.body):
            if _normalise_embed(target).endswith(_EXEMPT_EMBED_SUFFIXES):
                continue
            findings.append(
                _finding(
                    14,
                    "wiki-embed",
                    Severity.STYLE,
                    page.path,
                    f"Obsidian wiki embed ![[{target}]] -- use a standard markdown "
                    "![alt](path) embed (OKF)",
                )
            )
    return findings


def _check_image_hygiene(
    pages: list[_Page], *, assets: set[str], sidecars: set[str]
) -> list[Finding]:
    """Flag orphan assets, broken embeds and surviving sidecars (check 11)."""
    embedded: set[str] = set()
    findings: list[Finding] = []
    for page in pages:
        for embed in extract_embeds(page.body):
            embedded.add(embed)
            if embed not in assets:
                findings.append(
                    _finding(
                        11,
                        "broken-embed",
                        Severity.BROKEN,
                        page.path,
                        f"embeds missing asset {embed!r}",
                    )
                )
    for asset in sorted(assets):
        if asset not in embedded:
            findings.append(
                _finding(
                    11,
                    "orphan-binary",
                    Severity.BROKEN,
                    f"{_ASSETS_DIR}/{asset}",
                    "binary asset is embedded by no page",
                )
            )
    for sidecar in sorted(sidecars):
        findings.append(
            _finding(
                11,
                "asset-sidecar",
                Severity.BROKEN,
                f"{_ASSETS_DIR}/{sidecar}",
                "legacy per-image sidecar; merge into its owning page",
            )
        )
    return findings


def _inbound_targets(pages: list[_Page]) -> set[str]:
    """Return the set of normalised link targets across ``pages``.

    A self-link, where a page links to its own slug, is excluded, so a page cannot
    rescue itself from the orphan check.
    """
    inbound: set[str] = set()
    for page in pages:
        for target in extract_links(page.body):
            normalised = _normalise_target(target)
            if normalised == page.slug or normalised == page.path.removesuffix(".md"):
                continue
            inbound.add(normalised)
    return inbound


def _resolvable_targets(pages: list[_Page]) -> set[str]:
    """Return every handle a link may resolve to: the slug, path and aliases."""
    resolvable: set[str] = set()
    for page in pages:
        resolvable.add(page.slug)
        resolvable.add(page.path)
        resolvable.add(page.path.removesuffix(".md"))
        resolvable.update(page.aliases)
    return resolvable
