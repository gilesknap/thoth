"""The three retrieval passes as pure functions over the injected collaborators.

Each pass is a module-level function taking the vault, and for recall the Hindsight
seam, explicitly. The thin public methods on :class:`thoth.query.QueryEngine` gather the
collaborators and delegate here, and the user-facing contract of each pass is documented
on those methods.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from thoth.hindsight import Hindsight
from thoth.vault import REFERENCE_TYPES, Vault, VaultError

from ._shared import _HIGH_WEIGHT, _LOW_WEIGHT, _MAX_GREP_BYTES, SEARCHED_DIRS

_MD_LINK_RE: re.Pattern[str] = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")
"""Captures a standard markdown ``[text](path.md)`` link target (the OKF form)."""

_WIKILINK_RE: re.Pattern[str] = re.compile(r"\[\[([^\]|#]+)")
"""Captures a legacy ``[[wikilink]]`` target, ignoring any alias or anchor."""


# ---- pass 1: lexical scan over the curated folders ----------------------------------


def _grep(vault: Vault, term: str, *, limit: int = 20) -> list[str]:
    """Lexically scans :data:`SEARCHED_DIRS` for ``term``, ranked by hits."""
    tokens = _tokenize(term)
    if not tokens or limit < 1:
        return []
    patterns = [_token_pattern(token) for token in tokens]
    # Gather in the stable scan order, folder then filename. The sort below is stable,
    # so pages with an identical key keep it and the pre-#96 tie-break survives
    scored: list[tuple[int, int, str]] = []
    for rel, entry in vault.iter_folder_pages(SEARCHED_DIRS):
        # The filename and frontmatter are the high-weight haystack and the body is
        # the low-weight one. _safe_read keeps the leading --- block, so split it off
        raw = _safe_read(entry).lower()
        front, body = _split_frontmatter(raw)
        high_hay = f"{entry.name.lower()}\n{front}"
        matched = 0
        weight = 0
        for pattern in patterns:
            if pattern.search(high_hay):
                matched += 1
                weight += _HIGH_WEIGHT
            elif pattern.search(body):
                matched += 1
                weight += _LOW_WEIGHT
        if matched:
            scored.append((matched, weight, rel))
    # Rank by distinct-token count first, then placement weight, descending, so the
    # count of matched words always dominates
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [rel for _matched, _weight, rel in scored[:limit]]


# ---- pass 2: graph navigation --------------------------------------------------------


def _follow_links(vault: Vault, path: str, *, limit: int = 20) -> list[str]:
    """Resolves a page body's inter-page links to existing vault paths.

    Recognises the OKF standard markdown form and any residual wikilink (issue #189).
    Every target is reduced to its bare slug stem and resolved against the searched
    folders, which works because vault slugs are unique.
    """
    if limit < 1:
        return []
    try:
        page = vault.read_page(path)
    except VaultError:
        return []
    resolved: list[str] = []
    seen: set[str] = set()
    for target in _link_stems(page.body):
        candidate = _target_to_path(vault, target)
        if candidate is None or candidate in seen or candidate == path:
            continue
        seen.add(candidate)
        resolved.append(candidate)
        if len(resolved) >= limit:
            break
    return resolved


def _link_stems(body: str) -> list[str]:
    """Yields the bare slug stem of every internal inter-page link in ``body``.

    Unions the standard markdown and legacy wikilink forms. An external URL is dropped,
    and each surviving target has its alias, anchor, directory, ``.md`` and URL-escapes
    stripped back to the stem.
    """
    raw: list[str] = [match.group(1) for match in _WIKILINK_RE.finditer(body)]
    for match in _MD_LINK_RE.finditer(body):
        target = match.group(1).strip()
        if target and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            raw.append(target)
    stems: list[str] = []
    for target in raw:
        head = target.split("|", 1)[0].split("#", 1)[0]
        stem = PurePosixPath(unquote(head.strip())).stem
        if stem:
            stems.append(stem)
    return stems


# ---- pass 3: semantic recall ---------------------------------------------------------


def _recall_paths(
    hindsight: Hindsight,
    vault: Vault,
    query: str,
    *,
    limit: int = 10,
    types: frozenset[str] | None = REFERENCE_TYPES,
) -> list[str]:
    """Semantic recall via Hindsight, keeping only hits that resolve to real pages."""
    if limit < 1:
        return []
    kept: list[str] = []
    seen: set[str] = set()
    for hit in hindsight.recall(query, limit=limit, types=types):
        path = hit.path
        if path in seen:
            continue
        if not _confined_page_exists(vault, path):
            continue
        seen.add(path)
        kept.append(path)
    return kept


# ---- internals -----------------------------------------------------------------------


def _target_to_path(vault: Vault, target: str) -> str | None:
    """Resolves a link target to an existing vault page path, else ``None``.

    A folder-qualified target is taken verbatim and a bare slug is probed against each
    searched folder in order, with a trailing ``.md`` tolerated. Only confined, existing
    pages are returned, so a target that would escape the vault never resolves.
    """
    cleaned = target.strip().strip("/")
    if not cleaned:
        return None
    if cleaned.endswith(".md"):
        cleaned = cleaned[: -len(".md")]
    if "/" in cleaned:
        candidate = f"{cleaned}.md"
        if _confined_page_exists(vault, candidate):
            return PurePosixPath(candidate).as_posix()
        return None
    for folder in SEARCHED_DIRS:
        candidate = f"{folder}/{cleaned}.md"
        if _confined_page_exists(vault, candidate):
            return candidate
    return None


def _confined_page_exists(vault: Vault, path: str) -> bool:
    """True when ``path`` is vault-confined and exists as a page."""
    return vault.is_inside(path) and vault.page_exists(path)


def _safe_read(absolute_path: Path) -> str:
    """Reads a small text file for grep, returning ``""`` on any read failure."""
    try:
        if absolute_path.stat().st_size > _MAX_GREP_BYTES:
            return ""
        return absolute_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _tokenize(text: str) -> list[str]:
    """Splits a query into lowercase, non-empty whitespace-separated tokens."""
    return [token for token in text.lower().split() if token]


def _token_pattern(token: str) -> re.Pattern[str]:
    """Compiles a case-insensitive, word-boundary matcher for one query token (#96).

    Word boundaries stop the substring noise the old ``token in haystack`` scan
    produced, so "bed" no longer matches "embedded" and "do" no longer matches "window".
    The token is regex-escaped so punctuation in a slug-like token matches literally,
    and each boundary is only asserted when that end of the token is a word character,
    so a token like "c++" still matches at its non-word edge.
    """
    body = re.escape(token)
    left = r"\b" if token[:1].isalnum() or token[:1] == "_" else ""
    right = r"\b" if token[-1:].isalnum() or token[-1:] == "_" else ""
    return re.compile(f"{left}{body}{right}", re.IGNORECASE)


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Splits a page's raw text into its frontmatter and its body (#96 weighting).

    A vault page opens with a ``---`` fence, the YAML, a closing fence, then the body,
    the same shape ``python-frontmatter`` writes. Returning the two separately lets grep
    weight a token hitting the title or summary gloss above one hitting only prose. Text
    with no well-formed block is treated as all body, so a malformed page never crashes
    the scan and simply matches at the lower weight.
    """
    if not raw.startswith("---"):
        return "", raw
    # The closing fence is a line that is exactly "---" after the opening one
    closing = re.search(r"\n---[ \t]*(?:\n|$)", raw)
    if closing is None:
        return "", raw
    return raw[3 : closing.start()], raw[closing.end() :]
