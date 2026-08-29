"""Citation minting, prose composition, and the ``USED:`` selection parse.

The answer-side functions: building the harness-only citation for a confined page,
composing the prose either from the model or as a deterministic excerpt, and parsing the
model's trailing selection line back to the used subset (issue #34). The thin engine
methods delegate here with the injected collaborators passed explicitly.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from thoth.llm import LLM, Message, extract_text
from thoth.vault import Vault, VaultError

from ._shared import _EXCERPT_CHARS, Citation

_USED_LINE_RE: re.Pattern[str] = re.compile(
    r"^USED:\s*(.*)$", re.IGNORECASE | re.MULTILINE
)
"""Match the model's trailing ``USED: 1, 3`` (or ``USED: none``) selection line."""

_USED_SELECTION_LINE_RE: re.Pattern[str] = re.compile(
    r"^USED:[ \t]*(?:none|[\d,\s]*)$", re.IGNORECASE | re.MULTILINE
)
"""Matches a pure selection line, carrying only indices or "none".

Used to strip stray selection-only lines from the displayed prose, while leaving a
legitimate sentence that merely begins with the marker untouched.

This guards a misbehaving model that emits more than one selection line: only the last
drives the subset, but every selection-only line is removed so none leaks into the
answer.
"""


def _build_citation(vault: Vault, path: str) -> Citation:
    """Confines a path, reads its title, and builds the link and wikilink."""
    obsidian_uri = vault.obsidian_uri(path)
    slug = PurePosixPath(path).stem
    page = vault.read_page(path)
    title_value = page.frontmatter.get("title")
    title = title_value if isinstance(title_value, str) and title_value else slug
    summary_value = page.frontmatter.get("summary")
    snippet = summary_value.strip() if isinstance(summary_value, str) else ""
    return Citation(
        path=PurePosixPath(path).as_posix(),
        title=title,
        obsidian_uri=obsidian_uri,
        wikilink=f"[[{slug}]]",
        snippet=snippet,
    )


def _compose(
    vault: Vault, llm: LLM | None, query: str, consulted: list[Citation]
) -> tuple[str, list[Citation]]:
    """Composes the prose answer and the used citation subset (issue #34).

    With an LLM the consulted bodies are handed over as indexed context. The model
    writes prose and ends with a selection line naming the candidates that supported the
    answer, which is parsed back to citations and stripped from the display. A garbled
    line falls back to keeping every consulted citation.

    Without an LLM a deterministic excerpt of the top page is returned, citing that page
    alone.

    Args:
        vault: The path-confined vault facade.
        llm: The injected LLM, or None for the deterministic path.
        query: The natural-language query.
        consulted: Harness-built citations for every retrieved candidate.

    Returns:
        The displayed prose with the selection line stripped, and the subset the
        answer actually used.
    """
    if llm is not None:
        return _compose_with_llm(vault, llm, query, consulted)
    return _excerpt(vault, consulted[0].path), consulted[:1]


def _compose_with_llm(
    vault: Vault, llm: LLM, query: str, consulted: list[Citation]
) -> tuple[str, list[Citation]]:
    """Hands the indexed candidates to the LLM and returns prose and the used subset.

    Each candidate is labelled with a 1-based index and its excerpt goes over verbatim,
    embeds and all, so the model can answer questions about the attachments. Clean Slack
    output is the prompt's job rather than a pre-processor's.

    The model is told to write concise Slack markup, refer to pages by title only, never
    paste paths or embeds, and never narrate the source list, since the harness attaches
    it (issue #63).

    It ends with a selection line, which is parsed back to citations, stripped from the
    answer, and returned as the used subset. A garbled line falls back to all citations.
    """
    context_parts: list[str] = []
    for index, citation in enumerate(consulted, start=1):
        body = _excerpt(vault, citation.path, limit=2000)
        context_parts.append(f"[{index}] ## {citation.title} ({citation.path})\n{body}")
    context = "\n\n".join(context_parts)
    prompt = (
        "Answer the question using only the numbered vault pages below.\n\n"
        "Write a natural, concise answer in your own words. Format it as Slack "
        "mrkdwn: *bold* (single asterisks), _italic_ (single underscores) and "
        "lines starting with a bullet for lists -- never GitHub-style **bold** or "
        "Markdown # headings. Refer to pages by their title; do not paste file "
        "paths, [[wikilinks]] or ![[embeds]], and do not mention or list the "
        "sources -- just answer the question.\n\n"
        "On the final line, list the page numbers that directly support your "
        "answer as `USED: 1, 3` (comma-separated), or `USED: none` if no page "
        "applies. Put nothing after that line.\n\n"
        f"Question: {query}\n\nVault pages:\n{context}"
    )
    response = llm.complete([Message(role="user", content=prompt)])
    raw = extract_text(response).strip()
    return _split_used(raw, consulted)


def _excerpt(vault: Vault, path: str, *, limit: int = _EXCERPT_CHARS) -> str:
    """Returns a stripped, length-capped excerpt of a page body, deterministically."""
    try:
        page = vault.read_page(path)
    except VaultError:
        return ""
    body = page.body.strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "…"


def _split_used(raw: str, consulted: list[Citation]) -> tuple[str, list[Citation]]:
    """Splits the model reply into displayed prose and the used citations (#34).

    Finds the last selection line, since the prompt promises it comes last with nothing
    after it, and maps its 1-based indices back to citations. If the model emits more
    than one, only the last drives the subset but every selection-only line is stripped,
    so none leaks into the answer.

    A sentence that merely begins with the marker followed by words is preserved.

    The fallback is deliberately generous: a garbled or empty selection keeps every
    consulted citation, so a malformed reply never crashes and never silently drops all
    the sources. An explicit "none" yields an empty subset, so the renderer shows prose
    alone.

    Args:
        raw: The model's full text reply.
        consulted: The candidate citations, in the order shown to the model.

    Returns:
        The answer with the selection line stripped, and the used subset.
    """
    matches = list(_USED_LINE_RE.finditer(raw))
    match = matches[-1] if matches else None
    if match is None:
        return raw.strip(), list(consulted)
    # The last line drives the subset. Strip every selection-only line, so a stray
    # earlier one cannot survive in the prose, while keeping any real sentence
    prose = _USED_SELECTION_LINE_RE.sub("", raw).strip()
    selection = match.group(1).strip()
    if selection.lower() == "none":
        return prose, []
    indices = [int(tok) for tok in re.findall(r"\d+", selection)]
    if not indices:
        # A garbled selection with no parseable index, and not an explicit none, so
        # keep every citation
        return prose, list(consulted)
    used: list[Citation] = []
    seen: set[int] = set()
    for index in indices:
        if 1 <= index <= len(consulted) and index not in seen:
            seen.add(index)
            used.append(consulted[index - 1])
    # Every index was out of range, so nothing matched. Fall back to all rather than
    # dropping every source
    if not used:
        return prose, list(consulted)
    return prose, used
