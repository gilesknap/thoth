"""Citation minting, prose composition, and the ``USED:`` selection parse.

These are the answer-side functions: building the harness-only :class:`Citation` for a
confined page, composing the prose (LLM-written or deterministic excerpt), and parsing
the model's trailing ``USED:`` line back to the used citation subset (issue #34). The
thin methods on :class:`thoth.query.QueryEngine` delegate here with the injected
collaborators as explicit parameters.
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
"""Match a *pure* selection line (``USED:`` then only indices or ``none``).

Used to strip any stray selection-only lines from the displayed prose, while leaving a
legitimate prose sentence that merely *begins* with ``USED:`` (followed by words)
untouched. This guards against a misbehaving model that emits more than one ``USED:``
line: only the last one drives the citation subset, but every selection-only line is
removed so none leaks into the Slack answer.
"""


def _build_citation(vault: Vault, path: str) -> Citation:
    """Confine ``path``, read its title, and build the canonical link + wikilink."""
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
    """Compose the prose answer and the *used* citation subset (issue #34).

    With an injected LLM the consulted page bodies are handed to the model as
    indexed context; the model writes natural prose and ends with a ``USED: 1, 3``
    line naming the candidates that directly supported the answer. That line is
    parsed, mapped back to the consulted citations, and stripped from the displayed
    prose; the matching subset is returned. A missing/garbled ``USED:`` line falls
    back to keeping **all** consulted citations (the pre-#34 behaviour). Without an
    LLM a deterministic excerpt of the top consulted page is returned with that
    single page as its citation.

    Args:
        vault: The real, path-confined vault facade.
        llm: The optional injected LLM (``None`` means the deterministic path).
        query: The natural-language query.
        consulted: The harness-built citations for every retrieved candidate page.

    Returns:
        A ``(answer, used_citations)`` pair: the displayed prose (``USED:`` line
        stripped) and the subset of ``consulted`` the answer actually used.
    """
    if llm is not None:
        return _compose_with_llm(vault, llm, query, consulted)
    return _excerpt(vault, consulted[0].path), consulted[:1]


def _compose_with_llm(
    vault: Vault, llm: LLM, query: str, consulted: list[Citation]
) -> tuple[str, list[Citation]]:
    """Hand the indexed candidate pages to the LLM; return prose + the used subset.

    Each candidate is labelled with a 1-based index and its full excerpt is handed
    to the model verbatim (image ``![[embeds]]`` and all, so the model can answer
    questions *about* the attachments). Clean Slack output is the prompt's job, not
    a pre-processor's: the model is told to write natural, concise prose in Slack
    ``mrkdwn`` (``*bold*``/``_italic_``/bullets, never GitHub ``**bold**``),
    referring to pages by title only -- never pasting paths, ``[[wikilinks]]`` or
    ``![[embeds]]``, and never narrating the source list (the harness attaches it,
    so the model must not mention it; issue #63). It ends with a ``USED: <indices>``
    line; that line is parsed back to the consulted citations, stripped from the
    displayed answer, and the used subset returned. A missing/garbled line falls
    back to all citations.
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
    """Return a stripped, length-capped excerpt of a page body (deterministic)."""
    try:
        page = vault.read_page(path)
    except VaultError:
        return ""
    body = page.body.strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "…"


def _split_used(raw: str, consulted: list[Citation]) -> tuple[str, list[Citation]]:
    """Split the model reply into displayed prose + the used citations (issue #34).

    Finds the **last** ``USED: 1, 3`` (or ``USED: none``) line (the prompt promises the
    selection is on the *final* line, with nothing after it), maps its 1-based indices
    back to ``consulted`` citations, and returns the prose with the selection line(s)
    removed plus the matching subset. If the model misbehaves and emits more than one
    selection line, only the last drives the subset, but **every** selection-only line
    is stripped from the prose so none leaks into the displayed answer. A legitimate
    prose sentence that merely *begins* with ``USED:`` (followed by words, not indices)
    is preserved. Robust fallback: a missing/garbled/empty selection keeps **all**
    consulted citations (the pre-#34 behaviour) so a malformed model reply never crashes
    and never silently drops every source. ``USED: none`` yields an empty subset (the
    answer cited nothing), so the renderer shows prose alone.

    Args:
        raw: The model's full text reply (may end with a ``USED:`` line).
        consulted: The candidate citations, in the 1-based order shown to the model.

    Returns:
        A ``(prose, used)`` pair: the answer with the ``USED:`` line stripped, and the
        used citation subset.
    """
    matches = list(_USED_LINE_RE.finditer(raw))
    match = matches[-1] if matches else None
    if match is None:
        return raw.strip(), list(consulted)
    # The last line drives the subset; strip every selection-only line (a stray earlier
    # "USED: 1" must not survive in the prose) while keeping any "USED: <words>" prose.
    prose = _USED_SELECTION_LINE_RE.sub("", raw).strip()
    selection = match.group(1).strip()
    if selection.lower() == "none":
        return prose, []
    indices = [int(tok) for tok in re.findall(r"\d+", selection)]
    if not indices:
        # A garbled selection (no parseable index, not the explicit "none"): keep all.
        return prose, list(consulted)
    used: list[Citation] = []
    seen: set[int] = set()
    for index in indices:
        if 1 <= index <= len(consulted) and index not in seen:
            seen.add(index)
            used.append(consulted[index - 1])
    # Every index out of range -> nothing matched; fall back to all (never drop all).
    if not used:
        return prose, list(consulted)
    return prose, used
