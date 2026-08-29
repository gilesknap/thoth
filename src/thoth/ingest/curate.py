"""Passes 3-4: candidate fetch, the curate file-plan call and the as-is path."""

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

# Folders scanned by the read-only create-vs-update candidate search (reference layer).
_CANDIDATE_DIRS: tuple[str, ...] = ("entities", "notes", "memories")

# How many curate LLM attempts before giving up: one initial call plus one corrective
# retry that feeds the validation errors back to the model. A model that returns a
# slightly malformed plan (the failure mode that left the vault empty) is recovered
# rather than aborting the whole capture; a persistently invalid plan still raises so
# the validation gate is preserved.
_CURATE_ATTEMPTS: int = 2

# The forced tool the curate pass uses to return its file plan. Making the model emit
# the plan as a structured ``tool_use.input`` dict (rather than hand-serialised JSON
# free text) means the SDK/transport handles all escaping -- a body with raw newlines,
# tabs, **bold** or U+00A0 non-breaking spaces can never break JSON parsing (issue
# #110, where ~55 of ~140 holds aborted on "Unterminated string" raw_decode failures).
# The schema is deliberately PERMISSIVE (only ``pages`` required): tool-use guarantees
# valid JSON, NOT a valid plan, so :func:`validate_file_plan` stays the real gate and
# the repair loop still feeds validation problems back. The shape mirrors
# :func:`thoth.llm.file_plan_contract_text` and ``_check_page``.
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

# The forced-tool directive so curate ALWAYS returns its plan via the tool.
_SUBMIT_FILE_PLAN_CHOICE: dict[str, Any] = {
    "type": "tool",
    "name": "submit_file_plan",
}


class _CuratePass(_IngestorBase):
    """Passes 3-4: candidate fetch plus the validated curate or as-is file path."""

    # ---- pass 3: fetch candidates ------------------------------------------------

    def fetch_candidates(self, cls: Classification) -> list[str]:
        """Find, read-only, the existing pages that the curate pass may update.

        Runs :meth:`search_vault` for each named entity and concept and returns the
        de-duplicated, order-preserving list of candidate vault paths.

        Args:
            cls: The validated classification carrying the named terms.

        Returns:
            Vault-relative paths of existing curated pages that match a named term.
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
        """Run the curate call, validate the file-plan, and write every page.

        A second LLM call returns a file-plan, which
        :func:`thoth.llm.validate_file_plan` validates by reusing the same vault
        validators. Each page is then written through
        :meth:`thoth.vault.Vault.write_page`, which re-validates the folder, type and
        slug contract and confines the path. A plan that tries to escape the vault root,
        or violates the contract, is rejected and nothing is written for the offending
        page.

        Given ``analysis``, a binary capture (issue #42), the extracted text and
        description reach the model, so the curated page **body holds the real meaning**
        of the asset and cross-links it, instead of a blind stub around the asset embed.

        ``extracted_body`` plays the same role for a *text-bearing* capture whose body
        was extracted before classify, an audio transcript or a URL article's markdown.
        That body lives in ``raw/``, but the model can read only the prompt, not files.
        Without it an audio capture reached the model as a bare ``File: clip.m4a`` line
        and was filed as a content-free "no transcript yet" stub, even though whisper
        had transcribed it. It is inlined only when the capture has no inline ``text``,
        which is already shown, so a plain-text capture is never duplicated.

        Args:
            capture: The inbound item, for context.
            cls: The validated classification.
            raw: The raw-capture result, whose path and embeds the model is offered.
            candidates: Existing candidate page paths.
            analysis: Optional content analysis of a binary image or PDF capture.
            extracted_body: Optional pre-extracted text body, an audio transcript or
                a URL article's markdown, inlined so curate sees the real content.

        Returns:
            The validated file-plan object, carrying a private list of written page
            paths under ``"_written"``.

        Raises:
            IngestError: when the model output is unparseable, the plan fails
                validation, or a vault write rejects a page.
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
                # Transport/availability failure -> deferrable (raw is already durable).
                raise LLMUnavailableError(f"curate LLM call failed: {exc}") from exc
            try:
                plan = self._parse_and_validate_plan(response)
            except IngestError as exc:
                # A parse/validation failure is recoverable: feed the exact problems
                # back to the model once before giving up. The last attempt re-raises,
                # so a persistently invalid plan still aborts (validation gate kept).
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
        # Unreachable: the loop either returns a written plan or re-raises on the last
        # attempt, but keep a definite terminator for the type checker.
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
        """File one page with the original body verbatim, skipping the curate LLM call.

        The low-touch import mode (ADR 0010). The cheap classify call has already chosen
        the routing, the ``type``, ``slug`` and ``title``, so this writes ONE page into
        that type's content folder with the **original body verbatim** and a minimal
        derived frontmatter of ``title``, ``type``, ``source``, ``tags`` and
        ``personal``, plus a ``status`` default on an actionable action or media page.
        There is no second curate LLM call, no reshaping, no wikilink or dedup merge,
        and no summary synthesis. Any saved asset is embedded and any analysed OCR text
        appended, the same enrichment the curated path applies, so a binary import stays
        searchable on its content.

        Returns a file-plan-shaped dict, with ``_written`` and a single ``pages`` entry,
        so the shared navigation and report tail in :meth:`ingest` treats it like a
        curate plan. The page itself is written here through the confined
        :meth:`thoth.vault.Vault.write_page`, which re-validates the folder, type and
        slug contract.

        Args:
            capture: The inbound item, whose ``text`` and ``source`` are the body and
                provenance.
            cls: The validated classification, supplying folder routing, slug and title.
            raw: The raw-capture result, whose asset embeds are appended.
            extracted_body: A pre-extracted text body, a URL article or audio
                transcript, used as the page body when the capture has no inline
                ``text``.

        Returns:
            A file-plan-shaped dict whose ``_written`` lists the single filed page path.

        Raises:
            IngestError: when the classification routes to an unknown type or folder, or
                the vault rejects the write.
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
            # No curate pass runs, so stamp the universal default the contract expects
            # (write_page would default personal anyway).
            "personal": False,
        }
        # Actionable types (action, media) require a status; stamp the open default
        # (ADR 0015 retired the kind facet, so no kind is set).
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
        """Build the verbatim page body for an as-is import (no model reshaping).

        Prefers the inline ``text``, the Markdown or text upload case where the body IS
        the file, then a pre-extracted body from a URL article or audio transcript, then
        a stub naming the kept asset for a binary with no text. The saved-asset embeds
        are appended relative to ``folder``, so the binary renders in Obsidian.
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
        """Read the curate tool-use plan and validate it against the file-plan contract.

        The curate pass FORCES the ``submit_file_plan`` tool, so the plan arrives as a
        structured ``tool_use.input`` dict whose escaping the SDK handles, ending the
        invalid-JSON aborts (issue #110). A response with no such tool call counts as a
        parse failure, which the repair loop recovers. A ``pages`` value the model
        stochastically JSON-encoded as a STRING, a known slip despite the array schema,
        is unwrapped deterministically here rather than burning the corrective retry.
        ``validate_file_plan`` stays the authoritative gate, because tool use guarantees
        valid JSON, not a valid plan.

        Raises:
            IngestError: when the model did not call the tool, or the plan fails
                validation. The message names every offending field, so :meth:`curate`
                can feed it back to the model on the corrective retry.
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
        """Scan the curated folders, read-only, for ``query`` in filenames and bodies.

        A case-insensitive lexical scan over ``*.md`` in the curated layer,
        :data:`_CANDIDATE_DIRS`. No LLM and no network, just a disk read. It decides
        whether a named term already has a page to update.

        Args:
            query: The term to search for.
            limit: The maximum number of paths to return.

        Returns:
            Up to ``limit`` vault-relative paths whose filename or body contains the
            term, order-preserving and de-duplicated.
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
        """Write one validated file-plan page through the confined vault helper.

        ``write_page`` re-validates the folder, type and slug contract and confines the
        path, so a plan that slipped a bad folder or an escaping slug past the schema
        check is still rejected here. A reference page's per-plan ``summary`` (issue
        #72) routes into its frontmatter, the canonical rebuildable one-line gloss that
        replaced the old ``index.md`` catalog (ADR 0008) and that :meth:`thoth.query`
        grep then absorbs transparently. For a binary capture the asset's analysed OCR
        text is ensured present in the body (issue #42), so the page is searchable on
        the real content even when the model's body did not transcribe it.
        """
        folder = page["folder"]
        slug = page["slug"]
        frontmatter = dict(page["frontmatter"])
        frontmatter.setdefault("source", source)
        self._apply_summary(frontmatter, page)
        body = page["body"]
        body = self._append_embeds(body, raw, folder=folder)
        body = self._ensure_analysis_text(body, raw, analysis)
        # Page reuse vs create (issue #125): a plan ``action`` of "update", or a slug
        # already on disk, means this capture merges into an existing page rather than
        # creating a new one -- the signal that explains a screenshot folding into an
        # existing note.
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
        """Route a content page's per-plan ``summary`` into its frontmatter (#72).

        The curate plan carries a one-line ``summary`` per page. For every content page
        in :data:`~thoth.vault.SUMMARY_TYPES`, all four types including ``action`` since
        ADR 0013, it is the canonical rebuildable gloss, written into frontmatter as
        ``summary:`` so :meth:`thoth.query.QueryEngine.grep`, which scans the whole file
        including frontmatter, finds it and the Bases dashboards can show a Summary
        column. The page therefore owns its gloss instead of an ``index.md`` catalog
        (ADR 0008). A blank or whitespace summary, an ``inbox`` hold, and a page that
        already carries its own ``summary`` frontmatter are left untouched.
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
        """Append the analysed extracted text to an asset-bearing page when absent.

        Only a page carrying the saved asset gets the extracted text, so a multi-page
        plan does not duplicate the transcript onto unrelated pages. The text is
        appended only when the model's body does not already contain it, since the model
        may have transcribed it itself, so there is no double-paste.
        """
        if analysis is None or not raw.asset_paths or not analysis.text.strip():
            return body
        ocr = analysis.text.strip()
        if ocr in body:
            return body
        return body.rstrip("\n") + "\n\n## Extracted text\n\n" + ocr

    @staticmethod
    def _append_embeds(body: str, raw: RawCaptureResult, *, folder: str) -> str:
        """Append standard markdown image embeds for saved assets not already in body.

        Each saved asset is embedded with the OKF ``![](relative/path)`` form (issue
        #189), the path computed relative to the page's ``folder``, so a ``memories/``
        page embeds ``../raw/assets/photo.png``, and URL-escaped. An Excalidraw drawing,
        stored as ``<slug>.excalidraw.md`` on disk, is the one exception. Only the
        Obsidian ``![[<slug>.excalidraw]]`` wiki embed renders the drawing, the plugin
        keying on that basename (issue #68), and there is no standard-markdown
        equivalent, so it keeps the wiki form and a model-written
        ``![[<slug>.excalidraw.md]]`` is normalised to it first. An embed already in the
        model's body is not duplicated.
        """
        additions: list[str] = []
        for asset_rel in raw.asset_paths:
            name = PurePosixPath(asset_rel).name
            embed_name = _embed_name(name)
            if embed_name != name:
                # Excalidraw drawing: keep the Obsidian wiki embed (only form that
                # renders the drawing); normalise a model-written .md variant first.
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
        """Build the curate-call prompt: the file-plan contract, classification and raw.

        The model returns the plan by CALLING the ``submit_file_plan`` tool, forced
        through ``tool_choice``, so the plan is a structured ``tool_use.input`` dict the
        SDK escapes and it can never break JSON parsing (issue #110). The exact field
        and enum contract is embedded verbatim from
        :func:`thoth.llm.file_plan_contract_text`, rendered from the same constants the
        validator enforces, so the model knows the shape the tool input must satisfy,
        and ``validate_file_plan`` remains the gate. A binary capture's analysis is
        included (issue #42), so the curated body holds the asset's real extracted
        content, and ``extracted_body`` does the same for an audio transcript or URL
        article body, which the model cannot read off the raw page path.
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
    """Map an asset filename to the name Obsidian must embed to *render* it (issue #68).

    For an Excalidraw drawing stored as ``<slug>.excalidraw.md``, the trailing ``.md``
    must be dropped. Obsidian's basename for that file is ``<slug>.excalidraw``, and the
    Excalidraw plugin renders the *drawing* only for an ``![[<slug>.excalidraw]]``
    embed. ``![[<slug>.excalidraw.md]]`` embeds the markdown note instead, showing the
    raw scene JSON, which was the issue #68 live-verify failure. Every other asset
    embeds by its bare filename unchanged.
    """
    if asset_filename.endswith(".excalidraw.md"):
        return asset_filename[: -len(".md")]
    return asset_filename


def _relative_asset_href(asset_rel: str, folder: str) -> str:
    """Build a page-relative, URL-escaped href for a vault-relative asset path (#189).

    Content folders are one level deep, so an asset at ``raw/assets/photo.png`` becomes
    ``../raw/assets/photo.png`` from any content page. A space or other unsafe character
    is percent-encoded, so the markdown ``![](...)`` link is well-formed.
    """
    rel = posixpath.relpath(asset_rel, folder) if folder else asset_rel
    return quote(rel, safe="/")


def _curate_repair_prompt(problems: str) -> str:
    """Build the corrective retry prompt that feeds the problems back to the model.

    Sent as the follow-up user turn after a rejected plan, where the prior assistant
    turn carries the model's ``submit_file_plan`` tool call, so the model sees exactly
    what failed and fixes it rather than the capture aborting. The problem string may be
    a :func:`validate_file_plan` message OR "curate did not call submit_file_plan tool",
    meaning the model failed to call the tool at all, so the wording stays generic and
    references the tool either way.
    """
    return (
        "Your previous submit_file_plan call was REJECTED -- the following problems "
        f"were found:\n{problems}\n\n"
        "Call submit_file_plan again with a corrected plan that fixes EVERY problem "
        "above and matches the required shape exactly."
    )


def _curate_repair_turn(response: Any, problems: str) -> Message:
    """Build the user turn that feeds curate problems back for the corrective retry.

    The shape depends on how the prior assistant turn ended (issue #110 reviewer fix):

    * When the assistant CALLED ``submit_file_plan``, the normal forced-tool path where
      the plan merely failed :func:`validate_file_plan`, the Messages API requires the
      next user turn to OPEN with a ``tool_result`` block keyed to that ``tool_use``
      block's id. A plain-text turn after a ``tool_use`` block is a 400, reporting
      "tool_use ids were found without tool_result blocks immediately after", so the
      turn leads with ``tool_result(tool_use_id, repair_text, is_error=True)``.
    * When the assistant did NOT call the tool, the "did not call submit_file_plan"
      case, its turn is plain text, so a plain-text user follow-up is valid and no
      ``tool_result`` is owed.

    Args:
        response: The rejected curate response, whose assistant turn was just echoed.
        problems: The validation and parse problems to feed back.

    Returns:
        A user :class:`Message` whose content is API-valid for the echoed assistant
        turn.
    """
    text = _curate_repair_prompt(problems)
    for block in _tool_use_blocks(response):
        if _block_name(block) == "submit_file_plan":
            return Message(
                role="user",
                content=[tool_result_block(_block_id(block), text, is_error=True)],
            )
    return Message(role="user", content=text)
