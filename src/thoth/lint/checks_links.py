"""Link-graph checks: orphan pages (1), broken wikilinks (2), image hygiene (11).

Each check is a pure function over the parsed pages handed to it by
:class:`thoth.lint.LintEngine`; the engine's thin ``check_*`` methods gather the
pages (and asset filename sets) and delegate here.
"""

from __future__ import annotations

from .model import Finding, Severity, _finding, _Page
from .parse import _normalise_target, extract_embeds, extract_wikilinks

# The raw subdirectory holding binary assets (image-hygiene check 11).
_ASSETS_DIR: str = "raw/assets"


def _check_orphans(pages: list[_Page]) -> list[Finding]:
    """Flag curated reference pages with zero inbound wikilinks (check 1)."""
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
                "reference page has no inbound [[wikilinks]]",
            )
        )
    return findings


def _check_broken_wikilinks(pages: list[_Page]) -> list[Finding]:
    """Flag ``[[target]]`` links resolving to no page, honouring aliases (check 2)."""
    resolvable = _resolvable_targets(pages)
    findings: list[Finding] = []
    for page in pages:
        for target in extract_wikilinks(page.body):
            if _normalise_target(target) in resolvable:
                continue
            findings.append(
                _finding(
                    2,
                    "broken-wikilink",
                    Severity.BROKEN,
                    page.path,
                    f"wikilink [[{target}]] resolves to no page",
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
                        f"embeds missing asset ![[{embed}]]",
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
    """Return the set of normalised wikilink targets across ``pages``.

    Self-links (a page linking to its own slug) are excluded so a page cannot rescue
    itself from the orphan check.
    """
    inbound: set[str] = set()
    for page in pages:
        for target in extract_wikilinks(page.body):
            normalised = _normalise_target(target)
            if normalised == page.slug or normalised == page.path.removesuffix(".md"):
                continue
            inbound.add(normalised)
    return inbound


def _resolvable_targets(pages: list[_Page]) -> set[str]:
    """Return every handle a wikilink may resolve to: slug, path, and aliases."""
    resolvable: set[str] = set()
    for page in pages:
        resolvable.add(page.slug)
        resolvable.add(page.path)
        resolvable.add(page.path.removesuffix(".md"))
        resolvable.update(page.aliases)
    return resolvable
