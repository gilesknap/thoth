"""Shared frontmatter field coercions, pure and total, used by summary and lint.

This module is a leaf that imports only the standard library. It holds the tolerant
scalar coercions that both vault scanners apply to already-parsed frontmatter metadata.
Every helper is total. A malformed value degrades to the neutral result, which is
``None``, ``[]`` or ``False``, and never raises.
"""

from __future__ import annotations

from datetime import date, datetime

__all__: list[str] = []


def _str_field(value: object) -> str | None:
    """Return ``value`` as a stripped string, or ``None`` when it is absent or blank.

    The function strips a real string and returns ``None`` when nothing is left. It
    returns ``None`` for ``None``. It converts any other scalar with :func:`str`.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if value is None:
        return None
    return str(value)


def _page_tags(meta: dict[str, object]) -> list[str]:
    """Return a page's ``tags`` frontmatter as a list of trimmed strings."""
    raw = meta.get("tags")
    if isinstance(raw, list):
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _is_truthy(value: object) -> bool:
    """Return ``True`` for boolean ``True`` or a truthy string (true / yes / 1)."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _parse_date(value: object) -> date | None:
    """Coerce a frontmatter date value to a :class:`date`, or return ``None``.

    The function accepts a real :class:`~datetime.date` or
    :class:`~datetime.datetime`, because YAML often parses a bare ``YYYY-MM-DD`` to a
    ``date``. It also accepts a string in ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM`` form,
    and drops the trailing time. Any other value, an empty string or an unparseable
    string yields ``None``. The function treats a malformed date as "no date" and never
    raises.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        head = text.split()[0]
        try:
            return date.fromisoformat(head)
        except ValueError:
            return None
    return None
