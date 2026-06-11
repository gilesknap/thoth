"""Shared frontmatter field coercions (pure, total) used by summary and lint.

Stdlib-only leaf module holding the tolerant scalar coercions both vault scanners
apply to already-parsed frontmatter metadata. Every helper is total: a malformed
value degrades to the neutral result (``None`` / ``[]`` / ``False``), never raises.
"""

from __future__ import annotations

from datetime import date, datetime

__all__: list[str] = []


def _str_field(value: object) -> str | None:
    """Return ``value`` as a stripped string, or ``None`` when absent/blank.

    A real string is stripped (blank -> ``None``), ``None`` stays ``None``, and any
    other scalar is stringified.
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
    """Coerce a frontmatter date-ish value to a :class:`date`, else ``None``.

    Accepts a real :class:`~datetime.date` or :class:`~datetime.datetime` (YAML often
    parses bare ``YYYY-MM-DD`` to a ``date``), and a string in ``YYYY-MM-DD`` or
    ``YYYY-MM-DD HH:MM`` form (the trailing time is dropped). Any other value, an empty
    string, or an unparseable string yields ``None`` -- a malformed date is treated as
    "no date" and never raises.
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
