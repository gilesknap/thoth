"""The three retrieval passes as pure functions over the injected collaborators.

Each pass is a module-level function taking the :class:`~thoth.vault.Vault` (and, for
recall, the :class:`~thoth.hindsight.Hindsight` seam) explicitly; the thin public
methods on :class:`thoth.query.QueryEngine` gather the collaborators and delegate
here. The user-facing contract of each pass is documented on those methods.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from thoth.hindsight import Hindsight
from thoth.vault import REFERENCE_TYPES, Vault, VaultError

from ._shared import _HIGH_WEIGHT, _LOW_WEIGHT, _MAX_GREP_BYTES, SEARCHED_DIRS

_WIKILINK_RE: re.Pattern[str] = re.compile(r"\[\[([^\]|#]+)")
"""Capture an Obsidian ``[[wikilink]]`` target (ignoring ``|alias`` / ``#anchor``)."""


# ---- pass 1: lexical scan over the curated folders ----------------------------------


def _grep(vault: Vault, term: str, *, limit: int = 20) -> list[str]:
    """Lexically scan :data:`SEARCHED_DIRS` ``*.md`` for ``term``, ranked by hits."""
    tokens = _tokenize(term)
    if not tokens or limit < 1:
        return []
    patterns = [_token_pattern(token) for token in tokens]
    # Gather every matching page with its ranking key in the existing stable scan
    # order (folder order, then filename order). The sort below is stable, so pages
    # with an identical key keep this order -- preserving the pre-#96 tie-break.
    scored: list[tuple[int, int, str]] = []
    for rel, entry in vault.iter_folder_pages(SEARCHED_DIRS):
        # The filename and the page's frontmatter are the high-weight haystack;
        # the body is the low-weight one. _safe_read returns the raw text with
        # the leading "---" frontmatter block intact (#72), which we split off.
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
    # Rank by distinct-token count first, then placement weight; stable, so equal
    # keys keep their scan order. (matched, weight) descending = best page first.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [rel for _matched, _weight, rel in scored[:limit]]


# ---- pass 2: graph navigation --------------------------------------------------------


def _follow_wikilinks(vault: Vault, path: str, *, limit: int = 20) -> list[str]:
    """Resolve the ``[[wikilinks]]`` in a page body to existing vault paths."""
    if limit < 1:
        return []
    try:
        page = vault.read_page(path)
    except VaultError:
        return []
    resolved: list[str] = []
    seen: set[str] = set()
    for match in _WIKILINK_RE.finditer(page.body):
        target = match.group(1).strip()
        if not target:
            continue
        candidate = _target_to_path(vault, target)
        if candidate is None or candidate in seen or candidate == path:
            continue
        seen.add(candidate)
        resolved.append(candidate)
        if len(resolved) >= limit:
            break
    return resolved


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
    """Resolve a wikilink/catalog target to an existing vault page path or ``None``.

    Accepts a folder-qualified target (``people/jane-doe``) verbatim, and a bare
    slug (``program-motion-controller``) by probing each searched folder in order.
    A trailing ``.md`` is tolerated. Only confined, existing pages are returned, so
    a target that would escape the vault never resolves.
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
    """Return ``True`` when ``path`` is vault-confined and exists as a page."""
    return vault.is_inside(path) and vault.page_exists(path)


def _safe_read(absolute_path: Path) -> str:
    """Read a small text file for grep, returning ``""`` on any read failure."""
    try:
        if absolute_path.stat().st_size > _MAX_GREP_BYTES:
            return ""
        return absolute_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _tokenize(text: str) -> list[str]:
    """Split a query into lowercase, non-empty whitespace-separated tokens."""
    return [token for token in text.lower().split() if token]


def _token_pattern(token: str) -> re.Pattern[str]:
    """Compile a case-insensitive, word-boundary matcher for one query token (#96).

    Word boundaries (``\\b<token>\\b``) stop the substring noise the old ``token in
    haystack`` scan produced: ``"bed"`` no longer matches ``embedded`` and ``"do"`` no
    longer matches ``window``/``document``. The token is regex-escaped so punctuation in
    a slug-like token (``"drive-control-module"``) matches literally, and a leading or
    trailing word boundary is only asserted when the token *starts*/*ends* with a word
    character (so a token like ``"c++"`` still matches at its non-word edge).
    """
    body = re.escape(token)
    left = r"\b" if token[:1].isalnum() or token[:1] == "_" else ""
    right = r"\b" if token[-1:].isalnum() or token[-1:] == "_" else ""
    return re.compile(f"{left}{body}{right}", re.IGNORECASE)


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split a page's raw text into its YAML frontmatter and its body (#96 weighting).

    A vault page opens with a ``---`` fence, the YAML frontmatter, a closing ``---``
    fence, then the body (the same shape ``python-frontmatter`` writes). This returns
    ``(frontmatter, body)`` so grep can weight a token hitting the title/summary gloss
    above one hitting only prose. When the text has no well-formed frontmatter block the
    whole thing is treated as body (empty frontmatter), so a malformed or fence-less
    page never crashes the scan and simply matches at the lower body weight.
    """
    if not raw.startswith("---"):
        return "", raw
    # Find the closing fence: a line that is exactly "---" after the opening one.
    closing = re.search(r"\n---[ \t]*(?:\n|$)", raw)
    if closing is None:
        return "", raw
    return raw[3 : closing.start()], raw[closing.end() :]
