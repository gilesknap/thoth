"""The structured :class:`Analysis` result and its parse from the model's JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The coarse image kinds the folded analyse call may report (ADR 0009). Anything else
# normalises to "" so the ingest pass never branches on an unexpected value
_VALID_KINDS: frozenset[str] = frozenset({"diagram", "document", "photo", "screenshot"})


@dataclass(frozen=True, slots=True)
class Analysis:
    """The structured result of analysing one binary capture.

    Attributes:
        text: The extracted text of the asset, empty when it carries none
        description: A natural-language description of what the asset shows
        summary: A short one-line summary, usable as a title or log subject
        suggested_type: A routing hint, one of the four content types, so a whiteboard
            photo lands in a knowledge folder rather than ``memories/``. ``None`` when
            the model offered no usable hint
        entities: Named entities the model found, feeding the candidate fetch
        concepts: Named concepts the model found, feeding the candidate fetch
        kind: The coarse image kind, one of ``diagram``, ``document``, ``photo`` or
            ``screenshot``, and ``""`` when unknown. The single vision call folds this
            detection in rather than paying a separate pre-call (ADR 0009), and ingest
            branches on it to reconstruct a hand-drawn diagram in Excalidraw and to ask
            for a structured-markdown transcription of a document
    """

    text: str = ""
    description: str = ""
    summary: str = ""
    suggested_type: str | None = None
    entities: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    kind: str = ""

    def is_empty(self) -> bool:
        """True when the analysis carries no usable extracted content."""
        return not (self.text.strip() or self.description.strip())

    def body_markdown(self) -> str:
        """Renders the analysis as a markdown block for the curated page body.

        The block holds the real extracted meaning, the description followed by the
        verbatim text under an ``Extracted text`` heading, so the curated page is
        searchable on the asset's content rather than a blind stub. Returns an empty
        string when nothing was extracted, and the caller then keeps its own body.
        """
        parts: list[str] = []
        if self.description.strip():
            parts.append(self.description.strip())
        if self.text.strip():
            parts.append("## Extracted text\n\n" + self.text.strip())
        return "\n\n".join(parts)


def _analysis_from_obj(obj: dict[str, Any]) -> Analysis:
    """Builds an :class:`Analysis` from parsed JSON, tolerating missing keys."""
    suggested = obj.get("suggested_type")
    return Analysis(
        text=_as_str(obj.get("text")),
        description=_as_str(obj.get("description")),
        summary=_as_str(obj.get("summary")),
        suggested_type=suggested if isinstance(suggested, str) and suggested else None,
        entities=_as_str_list(obj.get("entities")),
        concepts=_as_str_list(obj.get("concepts")),
        kind=_as_kind(obj.get("kind")),
    )


def _as_str(value: object) -> str:
    """Coerces a JSON value to a string, empty for anything that is not one."""
    return value if isinstance(value, str) else ""


def _as_str_list(value: object) -> list[str]:
    """Returns ``value`` as a list of non-empty strings, empty for anything else."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _as_kind(value: object) -> str:
    """Normalises a reported image kind to one of the four valid values or ``""``.

    Anything outside :data:`_VALID_KINDS`, whether a missing key, a typo or an
    unexpected label, collapses to ``""`` so the ingest pass never branches on a
    surprise value.
    """
    if isinstance(value, str) and value in _VALID_KINDS:
        return value
    return ""
