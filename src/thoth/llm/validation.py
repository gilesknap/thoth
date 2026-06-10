"""The file-plan validator, reusing :mod:`thoth.vault`'s disk-write validators."""

from __future__ import annotations

from typing import Any

from thoth.vault import (
    REQUIRED_COMMON_FIELDS,
    VALID_SOURCES,
    SchemaError,
    SlugError,
    Vault,
)

from .client import SchemaValidationError
from .contract import _MIN_WIKILINKS, _VALID_LOG_ACTIONS


def _check_frontmatter(
    frontmatter: object, folder: str, where: str, problems: list[str]
) -> None:
    """Validate one page's frontmatter against the common contract and folder x type."""
    if not isinstance(frontmatter, dict):
        problems.append(f"{where}: 'frontmatter' must be an object")
        return
    for field in REQUIRED_COMMON_FIELDS:
        if field not in frontmatter:
            problems.append(f"{where}: missing required frontmatter field '{field}'")
    page_type = frontmatter.get("type")
    if isinstance(page_type, str):
        try:
            Vault.validate_folder_type(folder, page_type)
        except SchemaError as exc:
            problems.append(f"{where}: {exc}")
    elif "type" in frontmatter:
        problems.append(f"{where}: frontmatter 'type' must be a string")
    source = frontmatter.get("source")
    if source is not None and source not in VALID_SOURCES:
        problems.append(
            f"{where}: invalid source {source!r} (allowed: "
            f"{', '.join(sorted(VALID_SOURCES))})"
        )


def _check_page(page: object, idx: int, problems: list[str]) -> None:
    """Validate a single file-plan ``pages[idx]`` entry, appending any problems."""
    where = f"pages[{idx}]"
    if not isinstance(page, dict):
        problems.append(f"{where}: must be an object")
        return

    action = page.get("action")
    if action not in ("create", "update"):
        problems.append(
            f"{where}: 'action' must be 'create' or 'update', got {action!r}"
        )

    folder = page.get("folder")
    if not isinstance(folder, str) or not folder:
        problems.append(f"{where}: 'folder' must be a non-empty string")
        folder = ""

    slug = page.get("slug")
    if not isinstance(slug, str):
        problems.append(f"{where}: 'slug' must be a string")
    else:
        try:
            Vault.validate_slug(slug)
        except SlugError as exc:
            problems.append(f"{where}: {exc}")

    if not isinstance(page.get("body"), str):
        problems.append(f"{where}: 'body' must be a string")

    summary = page.get("summary")
    if summary is not None and not isinstance(summary, str):
        problems.append(f"{where}: 'summary' must be a string")

    wikilinks = page.get("wikilinks")
    if not isinstance(wikilinks, list):
        problems.append(f"{where}: 'wikilinks' must be a list")
    elif len(wikilinks) < _MIN_WIKILINKS:
        problems.append(
            f"{where}: needs >= {_MIN_WIKILINKS} wikilinks, got {len(wikilinks)}"
        )

    _check_frontmatter(page.get("frontmatter"), folder, where, problems)


def validate_file_plan(obj: dict[str, Any]) -> None:
    """Validate a file-plan against the vault contract.

    Reuses :mod:`thoth.vault`'s validators so a passing plan is guaranteed to survive
    :meth:`thoth.vault.Vault.write_page`. Each ``pages[*]`` entry is checked for a known
    ``action``, a valid ``slug``, an allowed ``folder`` x ``type`` pairing, the required
    common frontmatter fields, a valid ``source``, a string ``summary`` when present,
    and ``>= 2`` ``wikilinks``. Any ``log`` block is shape-checked too.

    Args:
        obj: The decoded file-plan object.

    Raises:
        SchemaValidationError: listing every problem found; the message names the
            offending field(s).
    """
    problems: list[str] = []

    pages = obj.get("pages")
    if not isinstance(pages, list):
        problems.append("'pages' must be a list")
    elif not pages:
        problems.append("'pages' must not be empty")
    else:
        for idx, page in enumerate(pages):
            _check_page(page, idx, problems)

    log = obj.get("log")
    if log is not None:
        if not isinstance(log, dict):
            problems.append("'log' must be an object")
        else:
            for field in ("action", "subject", "files"):
                if field not in log:
                    problems.append(f"log: missing '{field}'")
            log_action = log.get("action")
            if log_action is not None and log_action not in _VALID_LOG_ACTIONS:
                problems.append(
                    f"log: invalid action {log_action!r} (allowed: "
                    f"{', '.join(sorted(_VALID_LOG_ACTIONS))})"
                )
            files = log.get("files")
            if files is not None and not isinstance(files, list):
                problems.append("log: 'files' must be a list")

    if problems:
        raise SchemaValidationError(
            "file plan failed validation: " + "; ".join(problems)
        )
