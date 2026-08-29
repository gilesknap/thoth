"""Link-graph checks 1 (orphans), 2 (broken links), 11 (images), 14 (link style).

Each check is a pure function over the parsed pages handed to it by
:class:`thoth.lint.LintEngine`, whose thin ``check_*`` methods gather the pages and the
asset filename sets and delegate here.
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

# The raw subdirectory holding binary assets (check 11)
_ASSETS_DIR: str = "raw/assets"

# Bases views and Excalidraw drawings have no standard-markdown equivalent, so they
# legitimately stay in ![[...]] form and check 14 exempts them (issue #189)
_EXEMPT_EMBED_SUFFIXES: tuple[str, ...] = (".base", ".excalidraw")


def _check_orphans(pages: list[_Page]) -> list[Finding]:
    """Flags a curated reference page with zero inbound links (check 1)."""
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
    """Flags a link resolving to no page, honouring aliases (check 2).

    Recognises the OKF standard markdown form and any residual ``[[wikilink]]``, since
    the extractor unions both. A target resolves when its bare stem matches a page's
    slug, its full path, or one of its aliases.
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
    """Flags a legacy Obsidian wiki link or embed, since OKF wants markdown (check 14).

    A ``[[wikilink]]`` is non-portable and not the ``[text](path.md)`` form OKF requires
    (issue #189), so each is flagged as style. A wiki image embed is flagged in favour
    of ``![alt](path)``, but Bases and Excalidraw embeds, which have no markdown
    equivalent, are exempt.
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
    """Flags orphan assets, broken embeds and surviving sidecars (check 11)."""
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
    """Returns the set of normalised link targets across ``pages``.

    A self-link is excluded, so a page cannot rescue itself from the orphan check.
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
    """Returns every handle a link may resolve to: slug, path and aliases."""
    resolvable: set[str] = set()
    for page in pages:
        resolvable.add(page.slug)
        resolvable.add(page.path)
        resolvable.add(page.path.removesuffix(".md"))
        resolvable.update(page.aliases)
    return resolvable
