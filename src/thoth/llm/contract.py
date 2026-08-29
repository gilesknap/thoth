"""The curate file-plan contract rendered from the validator's own constants."""

from __future__ import annotations

from thoth.vault import (
    ACTION_STATUS_VOCAB,
    FOLDER_TYPE_CONTRACT,
    MEDIA_TYPE_VOCAB,
    PRIORITY_VOCAB,
    REQUIRED_COMMON_FIELDS,
    VALID_SOURCES,
)

# Valid actions for the file-plan log block, mirroring thoth.vault.append_log
_VALID_LOG_ACTIONS: frozenset[str] = frozenset(
    {"ingest", "create", "update", "query", "lint", "archive", "delete", "reindex"}
)
# Minimum outbound links per curated page, enforced by the prompt and the lint orphan
# and broken-link checks rather than the file-plan validator (issue #189)
_MIN_LINKS: int = 2


def file_plan_contract_text() -> str:
    """Renders the authoritative curate file-plan contract for the curate prompt.

    The curate pass asks the model for a JSON file plan, but historically gave it only a
    one-line "return a file plan (see the file-plan schema)" instruction with the schema
    never shown, so the model guessed the envelope and every capture was rejected by
    :func:`validate_file_plan`. This spells out the exact JSON shape and the enums.

    It renders from the same canonical constants the validator enforces, so the
    instructions and :func:`validate_file_plan` cannot drift and a new folder, type,
    source, vocabulary value or log action flows into the prompt automatically. The
    internal ``inbox`` holding folder is excluded, because it is the durable pre-LLM
    hold and never a curate target.

    Returns:
        A multi-line contract string to embed in the curate prompt
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
        f'    "body": "markdown with >= {_MIN_LINKS} standard links '
        '[text](folder/slug.md)",\n'
        '    "summary": "one crisp line: what this page is about"   // see below\n'
        "  } ],\n"
        f'  "log": {{"action": one of [{log_actions}], "subject": "...", '
        '"files": ["folder/slug.md"]}   // optional\n'
        "}\n"
        f"Folder -> required type: {folder_types}.\n"
        f"Link to related pages with at least {_MIN_LINKS} standard markdown links "
        "`[Title](folder/slug.md)` using the target page's vault-relative path -- NOT "
        "Obsidian `[[wikilinks]]`. Embed an image with `![](relative/path)`.\n"
        "A note carries a tag for its sub-kind (concept/comparison/query).\n"
        '"personal" keys off the SUBJECT, not whether it is a chore: true when the '
        "item concerns the owner's private life (home, family, friends, hobbies, "
        "personal admin, books/films to watch), false for work / technical / "
        "professional / general knowledge. A task being an errand does not make it "
        "personal -- a work errand (e.g. booking a meeting room) is personal: false.\n"
        'Author a crisp one-line "summary" for EVERY page, including actions and '
        "media; it becomes the page's canonical one-line gloss in frontmatter.\n"
        f'Action and media pages additionally require: "status": one of [{statuses}] '
        '(use todo for new items); optionally "due_date": "YYYY-MM-DD" and '
        f'"priority": one of [{priorities}].\n'
        "When the captured text states a deadline in RELATIVE or natural language "
        '("monday", "tomorrow", "next week", "end of the month", "in 3 days"), '
        'resolve it to a concrete "due_date" (YYYY-MM-DD) relative to TODAY\'S DATE '
        "given in the prompt -- a bare weekday means its NEXT upcoming occurrence. "
        "Do not leave such a deadline unset, and never guess a date when the text "
        "gives no deadline (omit due_date instead).\n"
        'Use type "media" (folder media/) for a to-consume book/film/podcast/etc; a '
        f'media page also sets "media_type": one of [{media_types}] and "url" when '
        "known.\n"
        'Memory pages set "memory_date": "YYYY-MM-DD" when the memory happened '
        "(else omit; it falls back to created).\n"
        "Tags are descriptive topic labels only -- never duplicate type or "
        "personal as a tag.\n"
        '"source" is the capture CHANNEL (one of the list above) -- NEVER a file path '
        "or the raw page path.\n"
        "Use today's date for created/updated. Do not invent folders, types, sources, "
        "or log actions outside the lists above.\n"
        "Emit a section heading ONLY when you have real content to put under it: never "
        "an empty heading, never a placeholder or 'expand later' comment, and never an "
        "HTML comment (<!-- ... -->) standing in for missing content. If the captured "
        "material is thin, a short body with just a one-paragraph summary (plus the "
        "standard markdown links) is correct and complete -- do not scaffold sections "
        "you cannot fill."
    )
