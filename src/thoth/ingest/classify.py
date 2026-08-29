"""Pass 1: the cheap classify call that routes a capture (SPEC Appendix)."""

from __future__ import annotations

from typing import Any

from thoth.analyse import Analysis
from thoth.llm import LLMError, Message, extract_text, parse_json_block
from thoth.vault import TYPE_ENUMERATION, VALID_TYPES, SlugError, Vault

from ._shared import (
    Capture,
    CaptureKind,
    Classification,
    IngestError,
    LLMUnavailableError,
    _IngestorBase,
    logger,
)


class _ClassifyPass(_IngestorBase):
    """The classify pass: one cheap call -> a validated routing decision."""

    # ---- pass 1: classify --------------------------------------------------------

    def classify(
        self,
        capture: Capture,
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> Classification:
        """Runs the cheap classify call and validates its routing output.

        One call returns the type, slug and title plus any named entities and concepts.
        The type and slug are validated through the vault here, so a bad routing
        decision is rejected before any disk is touched.

        A binary capture's analysis (issue #42) folds its extracted content into the
        prompt and unions its hints with the model's terms, so the item routes by
        content rather than landing in the default folder, and the candidate fetch sees
        the analysed terms. A pre-extracted body does the same for a text-bearing
        capture.

        The same bounded excerpt that feeds curate is folded in here, so a clearly
        personal URL routes differently from a technical one rather than being decided
        from the link and title alone (issue #123). An audio transcript is folded in
        even past a noise voice-memo caption, otherwise classify would title and route
        the note blind off the placeholder (issue #129).

        Args:
            capture: The inbound item to classify.
            analysis: Optional analysis of a binary capture.
            extracted_body: Optional pre-extracted body, folded in bounded so routing
                is content-aware.

        Returns:
            The validated classification.

        Raises:
            IngestError: if the output is unparseable, or names an out-of-vocabulary
                type or an invalid slug.
        """
        prompt = self._classify_prompt(
            capture, analysis=analysis, extracted_body=extracted_body
        )
        try:
            response = self._llm.complete([Message(role="user", content=prompt)])
        except Exception as exc:  # noqa: BLE001 - any client failure aborts classify
            # A transport failure is deferrable, since the raw is already durable. The
            # validation failures below stay a plain IngestError, keeping the gate
            raise LLMUnavailableError(f"classify LLM call failed: {exc}") from exc
        obj = self._parse_block(response, "classification")

        page_type = obj.get("type")
        if not isinstance(page_type, str):
            raise IngestError("classification 'type' must be a string")
        slug = obj.get("slug")
        if not isinstance(slug, str):
            raise IngestError("classification 'slug' must be a string")
        try:
            Vault.validate_slug(slug)
        except SlugError as exc:
            raise IngestError(f"classification slug rejected: {exc}") from exc
        if page_type not in VALID_TYPES:
            raise IngestError(
                f"classification type {page_type!r} is not a valid vault type"
            )

        title = obj.get("title")
        if not isinstance(title, str) or not title.strip():
            title = slug.replace("-", " ").title()

        page_type = self._route_by_analysis(page_type, analysis)
        entities = _str_list(obj.get("entities"))
        concepts = _str_list(obj.get("concepts"))
        if analysis is not None:
            entities = _merge_terms(entities, analysis.entities)
            concepts = _merge_terms(concepts, analysis.concepts)
        logger.debug(
            "classify chose: type=%s slug=%s title=%r (analysis_folded=%s, "
            "%d entities, %d concepts)",
            page_type,
            slug,
            title,
            analysis is not None,
            len(entities),
            len(concepts),
        )
        return Classification(
            page_type=page_type,
            slug=slug,
            title=title,
            entities=entities,
            concepts=concepts,
        )

    @staticmethod
    def _route_by_analysis(page_type: str, analysis: Analysis | None) -> str:
        """Promotes a generic memory routing to the analysed content type.

        The blind classifier defaults a binary capture to memory, the only thing it can
        guess from a filename. When the analyse pass extracted real content and
        suggested a knowledge type, honour that hint so the capture routes by its
        content rather than landing in the default folder (issue #42). A model that
        already chose a more specific type is trusted, and an analysis suggesting memory
        never overrides it.
        """
        if analysis is None:
            return page_type
        suggested = analysis.suggested_type
        if (
            page_type == "memory"
            and suggested is not None
            and suggested in VALID_TYPES
            and suggested != "memory"
        ):
            return suggested
        return page_type

    # ---- prompt builders ---------------------------------------------------------

    def _classify_prompt(
        self,
        capture: Capture,
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> str:
        """Builds the cheap classify prompt from the capture.

        The legal type enumeration is derived from the canonical vocabulary (issue #19)
        rather than restated, so a type added to the vault contract reaches the
        classifier automatically and the two cannot diverge. A binary capture's
        analysis, a bounded URL or transcript excerpt, and an audio transcript past any
        voice-memo caption are all folded in, so routing is content-aware and symmetric
        with curate.
        """
        what = self._capture_summary(
            capture,
            analysis=analysis,
            extracted_body=extracted_body,
            is_transcript=self._capture_kind(capture) is CaptureKind.AUDIO,
        )
        type_list = ", ".join(TYPE_ENUMERATION)
        return (
            "Classify this captured item for a personal knowledge vault. Return ONLY a "
            f"JSON object with keys: type (one of {type_list}), slug "
            "(lowercase-hyphen), title, entities (list of names), and concepts (list "
            "of names). Use 'note' for anything written (a concept, comparison, "
            "or query, differentiated by a tag); use 'action' for a todo or an "
            "errand; use 'media' for a to-consume media item (book/film/podcast "
            "to enjoy later).\n\n"
            f"Captured item:\n{what}"
        )

    # ---- shared parse helper -----------------------------------------------------

    @staticmethod
    def _parse_block(response: Any, what: str) -> dict[str, Any]:
        """Extracts text from a response and parses its first JSON object.

        Raises:
            IngestError: if no parseable JSON object is found.
        """
        text = extract_text(response)
        try:
            return parse_json_block(text)
        except LLMError as exc:
            raise IngestError(
                f"could not parse {what} from model output: {exc}"
            ) from exc


def _str_list(value: object) -> list[str]:
    """Returns a value as a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _merge_terms(primary: list[str], extra: list[str]) -> list[str]:
    """Unions two term lists, preserving order and de-duplicating case-insensitively.

    The model's own terms come first, so they drive the candidate fetch order, then any
    analysed terms not already present (issue #42).
    """
    seen = {term.lower() for term in primary}
    merged = list(primary)
    for term in extra:
        if term.lower() not in seen:
            merged.append(term)
            seen.add(term.lower())
    return merged
