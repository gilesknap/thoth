"""The structured :class:`Analysis` result and its parse from the model's JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The coarse image kinds the folded analyse call may report (ADR 0009). Anything the
# model returns outside this set is normalised to "" (unknown), so the ingest pass never
# branches on an unexpected value.
_VALID_KINDS: frozenset[str] = frozenset({"diagram", "document", "photo", "screenshot"})


@dataclass(frozen=True, slots=True)
class Analysis:
    """The structured result of analysing one binary capture.

    Attributes:
        text: The OCR'd / extracted text of the asset (an image's legible text, a PDF's
            body text). Empty when the asset carries no text.
        description: A structured natural-language description of the asset's content
            (what the image shows / what the document is about).
        summary: A short one-line summary suitable for a title / log subject.
        suggested_type: A routing hint -- one of the four content types
            (:data:`thoth.vault.TYPE_ENUMERATION`) -- so a whiteboard photo is routed to
            a knowledge folder rather than defaulting to ``memories/``. ``None`` when
            the model offered no usable hint.
        entities: Named entities the model found (feed the candidate fetch).
        concepts: Named concepts the model found (feed the candidate fetch).
        kind: The coarse image kind the model reported -- one of ``"diagram"``,
            ``"document"``, ``"photo"`` or ``"screenshot"``, ``""`` when unknown. This
            single vision call folds the kind detection in (ADR 0009) rather than paying
            a separate pre-call: the ingest pass branches on it to derive a best-effort
            Excalidraw reconstruction of a hand-drawn ``diagram``, and to ask for a
            faithful structured-markdown transcription of a ``document``.
    """

    text: str = ""
    description: str = ""
    summary: str = ""
    suggested_type: str | None = None
    entities: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    kind: str = ""

    def is_empty(self) -> bool:
        """Return ``True`` when the analysis carries no usable extracted content."""
        return not (self.text.strip() or self.description.strip())

    def body_markdown(self) -> str:
        """Render the analysis as a markdown block for the curated page body.

        The block holds the real extracted meaning -- the description followed by the
        verbatim OCR/extracted text under an ``Extracted text`` heading -- so the
        curated page is searchable on the asset's content, not a blind stub. Returns an
        empty string when there is nothing extracted (the caller then keeps its own
        body).
        """
        parts: list[str] = []
        if self.description.strip():
            parts.append(self.description.strip())
        if self.text.strip():
            parts.append("## Extracted text\n\n" + self.text.strip())
        return "\n\n".join(parts)


def _analysis_from_obj(obj: dict[str, Any]) -> Analysis:
    """Build an :class:`Analysis` from a parsed JSON object (missing keys tolerated)."""
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
    """Coerce a JSON value to a string (empty string for a non-string)."""
    return value if isinstance(value, str) else ""


def _as_str_list(value: object) -> list[str]:
    """Return ``value`` as a list of non-empty strings (empty list otherwise)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _as_kind(value: object) -> str:
    """Normalise a reported image kind to one of the four valid values or ``""``.

    Anything outside :data:`_VALID_KINDS` (a missing key, a typo, an unexpected label)
    collapses to ``""`` so the ingest pass never branches on a surprise value.
    """
    if isinstance(value, str) and value in _VALID_KINDS:
        return value
    return ""
