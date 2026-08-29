"""Passes 3-4: candidate fetch, the curate file-plan call, and the as-is path."""

from __future__ import annotations

import json
import posixpath
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from thoth.analyse import Analysis
from thoth.llm import (
    Message,
    SchemaValidationError,
    _block_id,
    _block_name,
    _tool_use_blocks,
    assistant_blocks_message,
    extract_tool_use,
    file_plan_contract_text,
    tool_result_block,
    validate_file_plan,
)
from thoth.vault import SUMMARY_TYPES, SchemaError, SlugError, VaultError

from ._shared import (
    _TYPE_FOLDER,
    Capture,
    CaptureKind,
    Classification,
    IngestError,
    LLMUnavailableError,
    RawCaptureResult,
    _IngestorBase,
    logger,
)

# Folders scanned by the read-only create-vs-update candidate search
_CANDIDATE_DIRS: tuple[str, ...] = ("entities", "notes", "memories")

# One initial call plus one corrective retry that feeds the validation errors back. A
# slightly malformed plan was the failure mode that left the vault empty, so recover it
# rather than aborting the capture. A persistently invalid plan still raises
_CURATE_ATTEMPTS: int = 2

# The forced tool curate uses to return its file plan. A structured tool_use.input dict
# lets the SDK handle all escaping, so raw newlines, tabs, bold or non-breaking spaces
# can never break JSON parsing. Issue #110 saw ~55 of ~140 holds abort on
# "Unterminated string". The schema is deliberately permissive, only pages required,
# because tool-use guarantees valid JSON and not a valid plan, so validate_file_plan
# stays the real gate
_SUBMIT_FILE_PLAN_TOOL: dict[str, Any] = {
    "name": "submit_file_plan",
    "description": "Submit the file plan for the captured item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "description": "One or more pages to create or update.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "update"],
                        },
                        "folder": {"type": "string"},
                        "slug": {
                            "type": "string",
                            "description": "lowercase-hyphenated",
                        },
                        "frontmatter": {
                            "type": "object",
                            "description": (
                                "title, type, created, updated, source, tags, "
                                "personal (+ status on action/media pages)"
                            ),
                            "additionalProperties": True,
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "markdown body with >= 2 standard links "
                                "[text](folder/slug.md)"
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": "one-line gloss (every page)",
                        },
                    },
                    "required": ["action", "folder", "slug", "frontmatter", "body"],
                },
            },
            "log": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "subject": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["pages"],
    },
}

# Forced-tool directive so curate always returns its plan via the tool
_SUBMIT_FILE_PLAN_CHOICE: dict[str, Any] = {
    "type": "tool",
    "name": "submit_file_plan",
}


class _CuratePass(_IngestorBase):
    """Passes 3-4: candidate fetch plus the validated curate / as-is file path."""

    # ---- pass 3: fetch candidates ------------------------------------------------

    def fetch_candidates(self, cls: Classification) -> list[str]:
        """Finds existing pages the curate pass may update, read-only.

        Args:
            cls: Validated classification carrying the named terms.

        Returns:
            De-duplicated vault paths matching a named term, in order.
        """
        seen: list[str] = []
        for term in (*cls.entities, *cls.concepts, cls.title):
            for path in self.search_vault(term):
                if path not in seen:
                    seen.append(path)
        return seen

    # ---- pass 4: curate ----------------------------------------------------------

    def curate(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        candidates: list[str],
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> dict[str, Any]:
        """Runs the curate call, validates the file plan, and writes every page.

        Each page goes through :meth:`thoth.vault.Vault.write_page`, which re-validates
        the folder, type and slug contract and confines the path. A plan escaping the
        vault root is rejected and nothing is written for that page.

        The model cannot read files, only the prompt, so a binary capture's analysis
        (issue #42) and any pre-extracted body are inlined here. Without that an audio
        capture arrived as a bare ``File: clip.m4a`` line and filed a content-free stub
        even though whisper had transcribed it.

        Args:
            capture: The inbound item, for context.
            cls: The validated classification.
            raw: Raw-capture result, whose path and embeds are offered to the model.
            candidates: Existing candidate page paths.
            analysis: Optional analysis of a binary capture.
            extracted_body: Optional pre-extracted body, inlined only when the
                capture has no inline text so nothing is duplicated.

        Returns:
            The validated file plan, with written page paths under ``_written``.

        Raises:
            IngestError: if the output is unparseable, the plan fails validation, or
                a vault write rejects a page.
        """
        prompt = self._curate_prompt(
            capture,
            cls,
            raw,
            candidates,
            analysis=analysis,
            extracted_body=extracted_body,
        )
        messages: list[Message] = [Message(role="user", content=prompt)]
        problems = ""
        for attempt in range(_CURATE_ATTEMPTS):
            try:
                response = self._llm.complete(
                    messages,
                    system_extra=self._schema_md,
                    tools=[_SUBMIT_FILE_PLAN_TOOL],
                    tool_choice=_SUBMIT_FILE_PLAN_CHOICE,
                )
            except Exception as exc:  # noqa: BLE001 - any client failure aborts curate
                # Transport failure is deferrable, raw is already durable
                raise LLMUnavailableError(f"curate LLM call failed: {exc}") from exc
            try:
                plan = self._parse_and_validate_plan(response)
            except IngestError as exc:
                # A parse or validation failure is recoverable, so feed the exact
                # problems back once before giving up. The last attempt re-raises, so a
                # persistently invalid plan still aborts
                problems = str(exc)
                if attempt + 1 >= _CURATE_ATTEMPTS:
                    raise
                messages = [
                    Message(role="user", content=prompt),
                    assistant_blocks_message(response),
                    _curate_repair_turn(response, problems),
                ]
                continue

            written: list[str] = []
            pages = plan.get("pages")
            assert isinstance(pages, list)  # guaranteed by validate_file_plan
            for page in pages:
                written.append(
                    self._write_planned_page(
                        page, capture.source, raw, analysis=analysis
                    )
                )
            plan["_written"] = written
            return plan
        # Unreachable, the loop returns or re-raises on the last attempt, but the type
        # checker wants a definite terminator
        raise IngestError(f"file plan rejected after retries: {problems}")

    # ---- pass 4 (alternative): file as-is, no curate (issue #80, ADR 0010) -------

    def _file_as_is(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        *,
        extracted_body: str | None = None,
    ) -> dict[str, Any]:
        """Files one page with the original body verbatim, skipping the curate call.

        This is the low-touch import mode (ADR 0010). Classify has already chosen the
        routing, so one page is written with the original body and a minimal derived
        frontmatter. There is no second LLM call, no reshaping, no dedup merge and no
        summary synthesis.

        Assets are still embedded and analysed OCR text still appended, so a binary
        import remains searchable on its content.

        The return is file-plan shaped so the shared report tail in :meth:`ingest`
        treats it like a curate plan.

        Args:
            capture: The inbound item, supplying body and provenance.
            cls: Validated classification, supplying folder, slug and title.
            raw: Raw-capture result, whose asset embeds are appended.
            extracted_body: Pre-extracted body used when the capture has no inline
                text.

        Returns:
            A file-plan shaped dict whose ``_written`` holds the one filed path.

        Raises:
            IngestError: if the type has no content folder or the vault rejects the
                write.
        """
        folder = _TYPE_FOLDER.get(cls.page_type)
        if folder is None:
            raise IngestError(
                f"as-is import: classification type {cls.page_type!r} has no content "
                "folder"
            )
        body = self._as_is_body(capture, raw, extracted_body, folder=folder)
        frontmatter: dict[str, Any] = {
            "title": cls.title,
            "type": cls.page_type,
            "source": capture.source,
            "tags": [],
            # No curate pass runs, so stamp the default the contract expects
            "personal": False,
        }
        # Actionable types require a status. ADR 0015 retired the kind facet
        if cls.page_type in ("action", "media"):
            frontmatter["status"] = "todo"
        try:
            rel = self._vault.write_page(folder, cls.slug, frontmatter, body)
        except (SchemaError, SlugError, VaultError) as exc:
            raise IngestError(
                f"as-is import rejected page {folder}/{cls.slug}: {exc}"
            ) from exc
        return {
            "_written": [rel],
            "pages": [{"frontmatter": dict(frontmatter)}],
            "log": {"subject": cls.title},
        }

    def _as_is_body(
        self,
        capture: Capture,
        raw: RawCaptureResult,
        extracted_body: str | None,
        *,
        folder: str,
    ) -> str:
        """Builds the verbatim page body for an as-is import, with no reshaping.

        Prefers the inline text, where the body is the file, then a pre-extracted body,
        then a stub for a binary with no text. Asset embeds are appended so the binary
        renders in Obsidian.
        """
        if capture.text is not None:
            body = capture.text
        elif extracted_body and extracted_body.strip():
            body = extracted_body
        elif raw.asset_paths:
            body = ""
        else:
            body = "_Imported with no extractable text._"
        return self._append_embeds(body, raw, folder=folder)

    def _parse_and_validate_plan(self, response: Any) -> dict[str, Any]:
        """Reads the curate tool-use plan and validates it against the contract.

        A missing tool call is treated as a parse failure, so the repair loop can
        recover it. A ``pages`` value the model JSON-encoded as a string is a known slip
        despite the array schema, so it is unwrapped here rather than burning the
        corrective retry.

        Raises:
            IngestError: if the tool was not called or the plan fails validation. The
                message names every offending field so :meth:`curate` can feed it back.
        """
        plan = extract_tool_use(response, "submit_file_plan")
        if plan is None:
            raise IngestError("curate did not call submit_file_plan tool")
        pages = plan.get("pages")
        if isinstance(pages, str):
            try:
                decoded = json.loads(pages)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                plan["pages"] = decoded
                logger.info("curate: unwrapped string-encoded 'pages' array")
        try:
            validate_file_plan(plan)
        except SchemaValidationError as exc:
            raise IngestError(f"file plan rejected: {exc}") from exc
        return plan

    # ---- read-only create-vs-update helper --------------------------------------

    def search_vault(self, query: str, *, limit: int = 10) -> list[str]:
        """Scans the curated folders for ``query`` in filenames and bodies, read-only.

        A case-insensitive lexical scan with no LLM and no network, used to decide
        whether a named term already has a page to update.

        Args:
            query: The term to search for.
            limit: Maximum paths to return.

        Returns:
            Up to ``limit`` matching vault paths, de-duplicated and in order.
        """
        needle = query.strip().lower()
        hits: list[str] = []
        if not needle:
            return hits
        for rel, md_path in self._vault.iter_folder_pages(_CANDIDATE_DIRS):
            if rel in hits:
                continue
            haystack = md_path.name.lower()
            try:
                haystack += "\n" + md_path.read_text(encoding="utf-8").lower()
            except OSError:
                pass
            if needle in haystack:
                hits.append(rel)
                if len(hits) >= limit:
                    return hits
        return hits

    # ---- internals: curate -------------------------------------------------------

    def _write_planned_page(
        self,
        page: dict[str, Any],
        source: str,
        raw: RawCaptureResult,
        *,
        analysis: Analysis | None = None,
    ) -> str:
        """Writes one validated file-plan page through the confined vault helper.

        ``write_page`` re-validates the contract and confines the path, so a bad folder
        or an escaping slug that slipped past the schema check is still rejected here. A
        page's ``summary`` is routed into frontmatter (issue #72) as the canonical gloss
        that replaced the old ``index.md`` catalog (ADR 0008). A binary capture's OCR
        text is ensured present so the page is searchable on the real content even when
        the model's body did not transcribe it (issue #42).
        """
        folder = page["folder"]
        slug = page["slug"]
        frontmatter = dict(page["frontmatter"])
        frontmatter.setdefault("source", source)
        self._apply_summary(frontmatter, page)
        body = page["body"]
        body = self._append_embeds(body, raw, folder=folder)
        body = self._ensure_analysis_text(body, raw, analysis)
        # Page reuse vs create (issue #125). An update action, or a slug already on
        # disk, means this capture merges into an existing page. This is the signal that
        # explains a screenshot folding into an existing note
        existed = self._vault.page_exists(f"{folder}/{slug}.md")
        logger.debug(
            "write page: %s/%s action=%s (%s by slug)",
            folder,
            slug,
            page.get("action", "?"),
            "updating existing" if existed else "creating new",
        )
        try:
            return self._vault.write_page(folder, slug, frontmatter, body)
        except (SchemaError, SlugError, VaultError) as exc:
            raise IngestError(
                f"vault rejected planned page {folder}/{slug}: {exc}"
            ) from exc

    @staticmethod
    def _apply_summary(frontmatter: dict[str, Any], page: dict[str, Any]) -> None:
        """Routes a content page's per-plan ``summary`` into its frontmatter (#72).

        The gloss is canonical and rebuildable, so the page owns it instead of an
        ``index.md`` catalog (ADR 0008). Grep scans frontmatter too, and the Bases
        dashboards can then show a Summary column. A blank summary, an ``inbox`` hold,
        or a page already carrying its own summary is left untouched.
        """
        if "summary" in frontmatter:
            return
        page_type = frontmatter.get("type")
        if not isinstance(page_type, str) or page_type not in SUMMARY_TYPES:
            return
        summary = page.get("summary")
        if isinstance(summary, str) and summary.strip():
            frontmatter["summary"] = summary.strip()

    @staticmethod
    def _ensure_analysis_text(
        body: str, raw: RawCaptureResult, analysis: Analysis | None
    ) -> str:
        """Appends the analysed OCR text to an asset-bearing page when it is absent.

        Only pages carrying the saved asset get the text, so a multi-page plan cannot
        duplicate the transcript onto unrelated pages. The model may have transcribed it
        already, so nothing is appended when the body already contains it.
        """
        if analysis is None or not raw.asset_paths or not analysis.text.strip():
            return body
        ocr = analysis.text.strip()
        if ocr in body:
            return body
        return body.rstrip("\n") + "\n\n## Extracted text\n\n" + ocr

    @staticmethod
    def _append_embeds(body: str, raw: RawCaptureResult, *, folder: str) -> str:
        """Appends markdown image embeds for saved assets not already in the body.

        Assets use the OKF ``![](relative/path)`` form (issue #189), computed relative
        to the page folder and URL-escaped.

        An Excalidraw drawing is the one exception. Only the Obsidian
        ``![[<slug>.excalidraw]]`` wiki embed renders the drawing, because the plugin
        keys on that basename (issue #68), and there is no standard-markdown equivalent.
        A model-written ``.md`` variant is normalised to it first.
        """
        additions: list[str] = []
        for asset_rel in raw.asset_paths:
            name = PurePosixPath(asset_rel).name
            embed_name = _embed_name(name)
            if embed_name != name:
                # Excalidraw keeps the wiki embed, the only form that renders the
                # drawing. Normalise a model-written .md variant first
                body = body.replace(f"![[{name}]]", f"![[{embed_name}]]")
                embed = f"![[{embed_name}]]"
                present = embed in body
            else:
                href = _relative_asset_href(asset_rel, folder)
                embed = f"![]({href})"
                present = name in body or href in body
            if present or embed in additions:
                continue
            additions.append(embed)
        if not additions:
            return body
        return body.rstrip("\n") + "\n\n" + "\n".join(additions)

    def _curate_prompt(
        self,
        capture: Capture,
        cls: Classification,
        raw: RawCaptureResult,
        candidates: list[str],
        *,
        analysis: Analysis | None = None,
        extracted_body: str | None = None,
    ) -> str:
        """Builds the curate prompt from the contract, classification and raw capture.

        The field and enum contract is embedded verbatim from
        :func:`thoth.llm.file_plan_contract_text`, rendered from the same constants the
        validator enforces, so the model knows the shape the tool input must satisfy.
        ``validate_file_plan`` remains the gate. A binary capture's analysis (issue #42)
        and any extracted body are included, because the model cannot read the raw page
        off disk.
        """
        candidate_block = "\n".join(f"- {path}" for path in candidates) or "(none)"
        raw_block = raw.raw_path or "(no raw page)"
        asset_block = ", ".join(raw.asset_paths) or "(none)"
        summary = self._capture_summary(
            capture,
            analysis=analysis,
            extracted_body=extracted_body,
            is_transcript=self._capture_kind(capture) is CaptureKind.AUDIO,
        )
        return (
            "Given the SCHEMA (in the system prompt) and the captured item below, file "
            "it into the vault by CALLING the submit_file_plan tool with the file "
            "plan.\n\n"
            f"Today's date is {self._today_iso()} (Europe/London) -- use it to resolve "
            "any relative deadline in the captured text into a concrete due_date.\n\n"
            f"{file_plan_contract_text()}\n\n"
            f"Classification: type={cls.page_type} slug={cls.slug} title={cls.title}\n"
            f"Raw source page: {raw_block}\n"
            "Saved assets (embed each inline with a markdown image, e.g. "
            f"![](../raw/assets/NAME)): {asset_block}\n"
            f"Existing candidate pages to maybe update:\n{candidate_block}\n\n"
            f"Captured item:\n{summary}"
        )


def _embed_name(asset_filename: str) -> str:
    """Maps an asset filename to the name Obsidian must embed to render it (#68).

    An Excalidraw drawing is stored as ``<slug>.excalidraw.md`` but Obsidian's basename
    for it is ``<slug>.excalidraw``, and the plugin only renders the drawing for that
    form. Embedding the ``.md`` name shows the raw scene JSON instead, which was the
    issue #68 live-verify failure.
    """
    if asset_filename.endswith(".excalidraw.md"):
        return asset_filename[: -len(".md")]
    return asset_filename


def _relative_asset_href(asset_rel: str, folder: str) -> str:
    """Builds a page-relative, URL-escaped href for a vault asset path (#189).

    Content folders are one level deep, so ``raw/assets/photo.png`` becomes
    ``../raw/assets/photo.png`` from any content page. Unsafe characters are
    percent-encoded so the markdown link is well-formed.
    """
    rel = posixpath.relpath(asset_rel, folder) if folder else asset_rel
    return quote(rel, safe="/")


def _curate_repair_prompt(problems: str) -> str:
    """Builds the corrective retry prompt that feeds the problems back to the model.

    Sent as the follow-up user turn after a rejected plan, so the model sees exactly
    what failed and fixes it rather than the capture aborting. The problems may come
    from ``validate_file_plan`` or from the model failing to call the tool at all, so
    the wording stays generic and references the tool either way.
    """
    return (
        "Your previous submit_file_plan call was REJECTED -- the following problems "
        f"were found:\n{problems}\n\n"
        "Call submit_file_plan again with a corrected plan that fixes EVERY problem "
        "above and matches the required shape exactly."
    )


def _curate_repair_turn(response: Any, problems: str) -> Message:
    """Builds the user turn that feeds curate problems back for the retry.

    The shape depends on how the prior assistant turn ended (issue #110):

    * If the assistant **called** ``submit_file_plan``, the normal forced-tool path
      where the plan merely failed :func:`validate_file_plan`, the Messages API requires
      the next user turn to open with a ``tool_result`` keyed to that ``tool_use``
      block's id. A plain-text turn there is a 400, so this leads with an error
      ``tool_result``.
    * If the assistant **did not** call the tool, its turn is plain text, so a
      plain-text follow-up is valid and no ``tool_result`` is owed.

    Args:
        response: The rejected curate response, just echoed as the assistant turn.
        problems: The validation or parse problems to feed back.

    Returns:
        A user message whose content is API-valid for the echoed turn.
    """
    text = _curate_repair_prompt(problems)
    for block in _tool_use_blocks(response):
        if _block_name(block) == "submit_file_plan":
            return Message(
                role="user",
                content=[tool_result_block(_block_id(block), text, is_error=True)],
            )
    return Message(role="user", content=text)
