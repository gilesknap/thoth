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
        """Run the cheap classify call and validate its routing output.

        One LLM call returns a JSON object with ``type``/``slug``/``title`` plus any
        named entities/concepts. The ``type`` and ``slug`` are validated through
        :class:`~thoth.vault.Vault` here, so a bad routing decision is rejected before
        any disk is touched.

        When ``analysis`` is supplied (a binary capture the analyse pass enriched, issue
        #42), the OCR'd/extracted content is folded into the prompt **and** the model's
        named entities/concepts are unioned with the analysis hints, so the item is
        routed *by its content* -- a whiteboard photo lands in ``notes/``, not the
        ``memories/`` default -- and the candidate fetch sees the analysed terms.

        ``extracted_body`` does the same for a *text-bearing* capture whose body was
        extracted before classify (a URL article's markdown / an audio transcript): the
        same bounded lead excerpt that already feeds curate (head-truncated to
        :data:`_URL_EXCERPT_CHARS`) is folded into the classify prompt too, so routing
        is **content-aware** -- a clearly-personal URL routes differently from a
        technical one, instead of being decided from the link + title alone (issue
        #123). classify stays on Sonnet here (the Haiku move is issue #79).

        For an **audio** capture the transcript is folded in even when a (noise)
        Slack voice-memo caption sits in ``capture.text`` -- otherwise classify would
        title and route the note blind off the "Listen to voice note" placeholder
        (issue #129); see :meth:`_capture_summary`'s ``is_transcript`` bypass, mirrored
        from curate.

        Args:
            capture: The inbound item to classify.
            analysis: Optional content analysis of a binary capture (image/PDF).
            extracted_body: Optional pre-extracted text body (URL article markdown /
                audio transcript) folded in -- bounded -- so routing is content-aware.

        Returns:
            The validated :class:`Classification`.

        Raises:
            IngestError: if the model output is unparseable or names an
                out-of-vocabulary type or an invalid slug.
        """
        prompt = self._classify_prompt(
            capture, analysis=analysis, extracted_body=extracted_body
        )
        try:
            response = self._llm.complete([Message(role="user", content=prompt)])
        except Exception as exc:  # noqa: BLE001 - any client failure aborts classify
            # A transport/availability failure -> deferrable (raw is already durable);
            # validation failures below stay a plain IngestError (abort, gate kept).
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
        """Promote a generic ``memory`` routing to the analysed content type.

        The blind classifier defaults a binary capture to ``memory`` (the only thing it
        can guess from a filename). When the analyse pass extracted real content and
        suggested a knowledge type (``entity``/``note``/``action``), honour that hint so
        the capture is routed by its content rather than landing in ``memories/`` by
        default (issue #42). A model that already chose a non-``memory`` type is
        trusted; an analysis suggesting ``memory`` (a personal snapshot) never overrides
        a more specific model choice.
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
        """Build the cheap classify-call prompt from the capture.

        The legal ``type`` enumeration is derived from
        :data:`thoth.vault.TYPE_ENUMERATION` (the canonical vocabulary, issue #19),
        not restated here, so a type added to the vault contract is offered to the
        classifier automatically and the two cannot diverge. A binary capture's analysis
        (issue #42) is folded in so the model classifies by the asset's real content;
        ``extracted_body`` folds in the same bounded URL/transcript excerpt that feeds
        curate so routing is content-aware (issue #123); for an audio capture the
        transcript is folded in even past a Slack voice-memo caption (``is_transcript``
        bypass), so a voice note is titled/routed by the spoken content (issue #129),
        symmetric with curate.
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
            "of names). Use 'note' for anything written (a concept, comparison, or "
            "query, differentiated by a tag); use 'action' for a todo or a to-consume "
            "item (a media item is an action tagged 'media').\n\n"
            f"Captured item:\n{what}"
        )

    # ---- shared parse helper -----------------------------------------------------

    @staticmethod
    def _parse_block(response: Any, what: str) -> dict[str, Any]:
        """Extract text from a response and parse its first JSON object.

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
    """Return ``value`` as a list of non-empty strings (empty list otherwise)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _merge_terms(primary: list[str], extra: list[str]) -> list[str]:
    """Union two term lists, order-preserving and case-insensitively de-duplicated.

    The model's own classify terms come first (so they drive the candidate fetch order),
    then any analysed entities/concepts not already present (issue #42).
    """
    seen = {term.lower() for term in primary}
    merged = list(primary)
    for term in extra:
        if term.lower() not in seen:
            merged.append(term)
            seen.add(term.lower())
    return merged
