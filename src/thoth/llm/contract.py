"""The curate file-plan contract rendered from the validator's own constants."""

from __future__ import annotations

from thoth.vault import (
    ACTION_KIND_VOCAB,
    ACTION_STATUS_VOCAB,
    FOLDER_TYPE_CONTRACT,
    MEDIA_TYPE_VOCAB,
    PRIORITY_VOCAB,
    REQUIRED_COMMON_FIELDS,
    VALID_SOURCES,
)

# Valid actions for the file-plan ``log`` block (mirrors thoth.vault.append_log).
_VALID_LOG_ACTIONS: frozenset[str] = frozenset(
    {"ingest", "create", "update", "query", "lint", "archive", "delete", "reindex"}
)
_MIN_WIKILINKS: int = 2


def file_plan_contract_text() -> str:
    """Render the authoritative curate file-plan contract for the curate prompt.

    The curate pass asks the model for a JSON file plan, but historically gave it only
    a one-line "return a file plan (see the file-plan schema)" instruction with the
    schema never actually shown -- so the model guessed the envelope and **every**
    capture was rejected by :func:`validate_file_plan` (empty ``folder``, missing
    ``slug``/``updated``/``wikilinks``, a file path mistaken for ``source``, a malformed
    ``log`` block). This spells out the exact JSON shape and the enums.

    It is rendered from the **same canonical constants the validator enforces**
    (:data:`~thoth.vault.FOLDER_TYPE_CONTRACT`, :data:`~thoth.vault.VALID_SOURCES`,
    :data:`~thoth.vault.REQUIRED_COMMON_FIELDS`, the action ``kind``/``status`` and
    ``priority``/``media_type`` vocabularies, :data:`_VALID_LOG_ACTIONS`,
    :data:`_MIN_WIKILINKS`), so the instructions and :func:`validate_file_plan` cannot
    drift -- a new folder/type/source/vocab-value/log-action flows into the prompt
    automatically. The internal ``inbox`` holding folder is excluded: it is the durable
    pre-LLM hold, never a curate target.

    Returns:
        A multi-line contract string to embed in the curate prompt.
    """
    offered = [folder for folder in FOLDER_TYPE_CONTRACT if folder != "inbox"]
    folder_types = ", ".join(
        f"{folder}->{sorted(types)[0]}"
        for folder, types in FOLDER_TYPE_CONTRACT.items()
        if folder != "inbox"
    )
    sources = ", ".join(sorted(VALID_SOURCES))
    required = ", ".join((*REQUIRED_COMMON_FIELDS, "personal"))
    log_actions = ", ".join(sorted(_VALID_LOG_ACTIONS))
    kinds = ", ".join(ACTION_KIND_VOCAB)
    statuses = ", ".join(ACTION_STATUS_VOCAB)
    priorities = ", ".join(PRIORITY_VOCAB)
    media_types = ", ".join(MEDIA_TYPE_VOCAB)
    return (
        "The file plan you submit MUST be a single object of this exact shape:\n"
        "{\n"
        '  "pages": [ {                         // REQUIRED, at least one page\n'
        '    "action": "create" | "update",\n'
        f'    "folder": one of [{", ".join(offered)}],\n'
        '    "slug": "lowercase-hyphenated",     // a-z 0-9 in single-hyphen groups\n'
        '    "frontmatter": {                     // MUST include ALL of: '
        f"{required}\n"
        '      "title": "...", "type": "<type matching the folder>",\n'
        '      "created": "YYYY-MM-DD", "updated": "YYYY-MM-DD",\n'
        f'      "source": one of [{sources}], "tags": ["..."], '
        '"personal": true|false\n'
        "    },\n"
        f'    "body": "markdown containing at least {_MIN_WIKILINKS} [[wikilinks]]",\n'
        '    "summary": "one crisp line: what this page is about",   // see below\n'
        '    "wikilinks": ["[[a-related-page]]", "[[another-page]]"]   // >= '
        f"{_MIN_WIKILINKS}\n"
        "  } ],\n"
        f'  "log": {{"action": one of [{log_actions}], "subject": "...", '
        '"files": ["folder/slug.md"]}   // optional\n'
        "}\n"
        f"Folder -> required type: {folder_types}.\n"
        "A note carries a tag for its kind (concept/comparison/query).\n"
        '"personal" is true when the item concerns the owner\'s private life (people, '
        "errands, books/films to watch), false for work / technical / general "
        "knowledge.\n"
        'Author a crisp one-line "summary" for EVERY page, including actions; it '
        "becomes the page's canonical one-line gloss in frontmatter.\n"
        f'Action pages additionally require: "kind": one of [{kinds}] and "status": '
        f"one of [{statuses}] (use todo for new items); optionally "
        f'"due_date": "YYYY-MM-DD" and "priority": one of [{priorities}].\n'
        f'When kind is media also set "media_type": one of [{media_types}] and "url" '
        "when known.\n"
        'Memory pages set "memory_date": "YYYY-MM-DD" when the memory happened '
        "(else omit; it falls back to created).\n"
        "Tags are descriptive topic labels only -- never duplicate type, kind, or "
        "personal as a tag.\n"
        '"source" is the capture CHANNEL (one of the list above) -- NEVER a file path '
        "or the raw page path.\n"
        "Use today's date for created/updated. Do not invent folders, types, sources, "
        "or log actions outside the lists above.\n"
        "Emit a section heading ONLY when you have real content to put under it: never "
        "an empty heading, never a placeholder or 'expand later' comment, and never an "
        "HTML comment (<!-- ... -->) standing in for missing content. If the captured "
        "material is thin, a short body with just a one-paragraph summary (plus the "
        "[[wikilinks]]) is correct and complete -- do not scaffold sections you cannot "
        "fill."
    )
