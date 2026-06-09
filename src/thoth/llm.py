"""Anthropic client wrapper, the PKM persona, and the file-plan / answer schemas.

This module owns three framework-independent things the ingest and query phases
build on:

* the verbatim **PKM Agent Persona** system-prompt string (:data:`PERSONA`), lifted
  from the SPEC Appendix, which makes the vault canonical, makes Hindsight a derived
  index, and bakes in the ``obsidian://`` retrieval format and a concise tone;
* helpers that assemble the ``messages.create`` keyword arguments with prompt caching
  (a stable :data:`PERSONA` prefix carrying a ``cache_control`` breakpoint), plus a
  thin injectable :class:`LLM` wrapper around the Anthropic SDK; and
* the JSON schemas (:data:`FILE_PLAN_SCHEMA`, :data:`ANSWER_SCHEMA`) the harness holds
  model output to, with validators (:func:`validate_file_plan` /
  :func:`validate_answer`).

The ``anthropic`` SDK is imported **lazily**, only inside :func:`make_client`, so that
importing :mod:`thoth.llm` (for example at pytest collection or by a tool that only
needs the schemas) never requires the package to be installed. The client is also
**injectable** so tests substitute a fake exposing ``.messages.create(**kwargs)``.

The file-plan validator deliberately reuses the *same* validators that
:mod:`thoth.vault` enforces at disk-write time (:meth:`thoth.vault.Vault.validate_slug`,
:meth:`thoth.vault.Vault.validate_folder_type`, and the
:data:`thoth.vault.REQUIRED_COMMON_FIELDS` / :data:`thoth.vault.VALID_TYPES` /
:data:`thoth.vault.VALID_SOURCES` enums), so a plan that validates here is guaranteed to
pass :meth:`thoth.vault.Vault.write_page` without a second divergent ruleset. The
``obsidian://`` links returned to users are always built by the harness from validated
paths (never fabricated by the model); the persona only *tells* the model the format.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from thoth.budget import KIND_ANTHROPIC, BudgetGuardLike
from thoth.config import Config
from thoth.vault import (
    FOLDER_TYPE_CONTRACT,
    INDEX_SECTIONS,
    LOG_ACTIONS,
    REQUIRED_COMMON_FIELDS,
    VALID_SOURCES,
    SchemaError,
    SlugError,
    Vault,
)

__all__ = [
    "ANSWER_SCHEMA",
    "DATED_MODEL_FALLBACK",
    "DEFAULT_MAX_TOKENS",
    "FILE_PLAN_SCHEMA",
    "file_plan_contract_text",
    "PERSONA",
    "AnthropicLike",
    "LLM",
    "LLMError",
    "Message",
    "SchemaValidationError",
    "assistant_blocks_message",
    "build_create_kwargs",
    "build_system_blocks",
    "extract_text",
    "make_client",
    "parse_json_block",
    "response_content_blocks",
    "tool_result_block",
    "user_blocks_message",
    "validate_answer",
    "validate_file_plan",
]


PERSONA: str = """# PKM Agent Persona

You are a Personal Knowledge Management assistant — a second brain for one user
(Giles, Europe/London). You capture knowledge into a canonical Obsidian vault,
retrieve it with structural + semantic search, and always point the user back to
the real note in their own Obsidian.

## Source of truth
- The **Obsidian vault** (markdown files + binary assets in `raw/assets/`) is the
  ONLY canonical store. It is a git repo, two-way synced with the user's workstation.
- **Hindsight is a rebuildable index over the vault**, not a store. If it drifts,
  it gets reindexed from the vault.
- The small transient state DB is **working memory only** — never the knowledge base.
  Do NOT treat ingested content as "saved" because it is in a session or in Hindsight;
  it is saved only when it is a committed vault file.

## Capturing content (throw-it-and-forget)
1. Detect type: URL, markdown note, code, idea, quote, image, PDF, TODO/Action,
   media-to-consume, memory.
2. Pull the vault first (pull --rebase).
3. Immutable sources (uploaded articles/papers/transcripts/images) go to `raw/`
   (images to `raw/assets/`). Write the curated, cross-linked page in the right
   layer (entities / concepts / comparisons / queries) per SCHEMA.md.
4. Life-admin items (Actions/TODOs, media backlog, memories) are wiki pages with a
   frontmatter `type:` — never a rival folder tree. Set due/recurrence/priority on
   Actions from natural language.
5. Embed images inline with Obsidian wiki-embeds; the curated page describes AND
   embeds the asset. Never store base64. Never write a separate descriptive sidecar.
6. Auto-tag and cross-link. Never ask the user to file or tag.
7. Retain the page into Hindsight, attaching its vault path (reference=<path> if
   supported, else a `SOURCE: <path>` sentinel line + path tag); probe with recall
   that the page path comes back (auto_retain is off, so this is the only thing
   indexing the page). Append to `log.md`; then commit+push.
8. Confirm in 1–2 lines: what it is, where it landed, the tags applied.

## Retrieving content
1. Navigate structurally first (folders, `index.md`, wikilinks, Bases views), then
   use Hindsight semantic recall over CURATED pages to find by meaning.
2. Answer concisely from the vault, then ALWAYS offer the source:
   `obsidian://open?vault=pkm-vault&file=<url-encoded vault-relative path>`
   plus the plain vault-relative path and a `[[wikilink]]`.
3. Slack: render as mrkdwn `<url|title>`. MCP: markdown `[title](url)` + raw path +
   wikilink (the host may not make the custom scheme clickable).
4. Offer a Slack file upload ONLY if the user asks or clearly can't reach Obsidian.

## Proactive summaries (cron, Europe/London)
- Daily 07:00 and weekly Mon 07:00 to the user's Slack DM, composed FROM THE VAULT:
  due/overdue Actions, deadlines in the next 3 days, recent ingests, media-backlog
  nudges, emerging themes, review-flagged items. Use wikilinks as handles.

## Tone
- Concise. Acknowledge captures in 1–2 lines. Give retrieval results with their
  source links and nothing extra. You are an efficient, reliable tool, not a
  conversationalist. Prefer clean state — no cruft, no commented-out leftovers.

## Timezone: Europe/London (GMT/BST)
"""
"""The PKM Agent Persona system-prompt string (verbatim from the SPEC Appendix).

It is the stable, cacheable prefix for every appliance Claude call. It encodes the
load-bearing invariants the later phases rely on: the vault is canonical, Hindsight is
a rebuildable index, the ``obsidian://open`` link template, the ``Europe/London``
timezone, and the concise tone.
"""

DATED_MODEL_FALLBACK: str = "claude-sonnet-4-20250514"
"""Proven dated Anthropic model id, used as a fallback when a bare alias 404s."""

DEFAULT_MAX_TOKENS: int = 4096
"""Default ``max_tokens`` for a ``messages.create`` call."""


# --- JSON schemas the harness validates model output against -----------------
# These are documentation-grade plain dicts. The authoritative *checks* live in the
# validators below, which reuse thoth.vault so the rules cannot diverge from disk.

FILE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pages"],
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "action",
                    "folder",
                    "slug",
                    "frontmatter",
                    "body",
                    "wikilinks",
                ],
                "properties": {
                    "action": {"enum": ["create", "update"]},
                    "folder": {"type": "string"},
                    "slug": {"type": "string"},
                    "frontmatter": {"type": "object"},
                    "body": {"type": "string"},
                    "wikilinks": {"type": "array", "minItems": 2},
                    "embeds": {"type": "array"},
                    "confidence": {"enum": ["high", "medium", "low"]},
                },
            },
        },
        "index_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["section", "wikilink", "summary"],
            },
        },
        "log": {
            "type": "object",
            "required": ["action", "subject", "files"],
        },
    },
}
"""Shape of the curate-pass file-plan the appliance model returns (see SPEC §6).

Each ``pages[*]`` entry maps 1:1 onto :meth:`thoth.vault.Vault.write_page`
(``action``/``folder``/``slug``/``frontmatter``/``body``), carries ``>= 2`` outbound
``wikilinks`` and optional ``embeds``; ``index_entries`` feed
:meth:`thoth.vault.Vault.append_index` and ``log`` feeds
:meth:`thoth.vault.Vault.append_log`. Authoritative validation is
:func:`validate_file_plan`.
"""

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer", "page_paths", "used_web", "web_sources"],
    "properties": {
        "answer": {"type": "string"},
        "page_paths": {"type": "array", "items": {"type": "string"}},
        "used_web": {"type": "boolean"},
        "web_sources": {"type": "array", "items": {"type": "string"}},
    },
}
"""Shape of a blended-answer object (see SPEC §7.1); see :func:`validate_answer`.

``answer`` is the prose; ``page_paths`` are vault-relative pages cited; ``used_web``
flags whether the web was consulted; ``web_sources`` lists the cited URLs.
"""

_MIN_WIKILINKS: int = 2


def file_plan_contract_text() -> str:
    """Render the authoritative curate file-plan contract for the curate prompt.

    The curate pass asks the model for a JSON file plan, but historically gave it only
    a one-line "return a file plan (see the file-plan schema)" instruction with the
    schema never actually shown -- so the model guessed the envelope and **every**
    capture was rejected by :func:`validate_file_plan` (empty ``folder``, missing
    ``slug``/``updated``/``wikilinks``, a file path mistaken for ``source``, malformed
    ``index_entries``/``log``). This spells out the exact JSON shape and the enums.

    It is rendered from the **same canonical constants the validator enforces**
    (:data:`~thoth.vault.FOLDER_TYPE_CONTRACT`, :data:`~thoth.vault.VALID_SOURCES`,
    :data:`~thoth.vault.REQUIRED_COMMON_FIELDS`, :data:`_VALID_LOG_ACTIONS`,
    :data:`_MIN_WIKILINKS`), so the instructions and :func:`validate_file_plan` cannot
    drift -- a new folder/type/source/log-action flows into the prompt automatically.
    The internal ``inbox`` holding folder is excluded: it is the durable pre-LLM hold,
    never a curate target.

    Returns:
        A multi-line contract string to embed in the curate prompt.
    """
    offered = [folder for folder in FOLDER_TYPE_CONTRACT if folder != "inbox"]
    folder_types = ", ".join(
        f"{folder}->{sorted(types)[0]}"
        for folder, types in FOLDER_TYPE_CONTRACT.items()
        if folder != "inbox"
    )
    sources = ", ".join(sorted(VALID_SOURCES))
    required = ", ".join(REQUIRED_COMMON_FIELDS)
    log_actions = ", ".join(sorted(LOG_ACTIONS))
    sections = ", ".join(sorted(INDEX_SECTIONS))
    return (
        "Return ONLY a single JSON object (no prose, no commentary) of this exact "
        "shape:\n"
        "{\n"
        '  "pages": [ {                         // REQUIRED, at least one page\n'
        '    "action": "create" | "update",\n'
        f'    "folder": one of [{", ".join(offered)}],\n'
        '    "slug": "lowercase-hyphenated",     // a-z 0-9 in single-hyphen groups\n'
        '    "frontmatter": {                     // MUST include ALL of: '
        f"{required}\n"
        '      "title": "...", "type": "<type matching the folder>",\n'
        '      "created": "YYYY-MM-DD", "updated": "YYYY-MM-DD",\n'
        f'      "source": one of [{sources}], "tags": ["..."]\n'
        "    },\n"
        f'    "body": "markdown containing at least {_MIN_WIKILINKS} [[wikilinks]]",\n'
        '    "wikilinks": ["[[a-related-page]]", "[[another-page]]"]   // >= '
        f"{_MIN_WIKILINKS}\n"
        "  } ],\n"
        '  "index_entries": [ {"section": "<catalog section>", '
        '"wikilink": "[[slug]]", "summary": "..."} ],   // optional\n'
        f'  "log": {{"action": one of [{log_actions}], "subject": "...", '
        '"files": ["folder/slug.md"]}   // optional\n'
        "}\n"
        f"Folder -> required type: {folder_types}.\n"
        f'index_entries "section" (if you include any) MUST be one of: {sections}; '
        "omit index_entries entirely if none fit.\n"
        '"source" is the capture CHANNEL (one of the list above) -- NEVER a file path '
        "or the raw page path.\n"
        "Use today's date for created/updated. Do not invent folders, types, sources, "
        "log actions, or index sections outside the lists above."
    )


class LLMError(Exception):
    """Base error for LLM wrapper problems."""


class SchemaValidationError(LLMError):
    """Raised when model JSON output fails schema validation.

    The message lists every violation found so a caller (or a retry prompt) can see
    all problems at once rather than one at a time.
    """


class AnthropicLike(Protocol):
    """Structural type for the slice of the Anthropic SDK this module uses.

    Anything exposing a ``messages`` attribute whose ``.create(**kwargs)`` returns a
    response satisfies it, so tests can inject a tiny fake without the real SDK.
    """

    @property
    def messages(self) -> Any:
        """The messages namespace exposing ``create(**kwargs)``."""
        ...


@dataclass(frozen=True, slots=True)
class Message:
    """One chat turn handed to the model.

    ``content`` is either a plain string (an ordinary text turn) or a list of native
    Anthropic content blocks (a ``text`` / ``tool_use`` / ``tool_result`` block list,
    the last keyed by ``tool_use_id``). The structured form is what
    a multi-turn tool-use conversation requires: the assistant turn after a
    ``stop_reason='tool_use'`` response must echo the model's ``tool_use`` block(s) and
    the following user turn must carry a ``tool_result`` block per ``tool_use_id`` (the
    real Messages API rejects a tool-use exchange flattened to text). The block list is
    passed through to ``messages.create`` verbatim by :func:`build_create_kwargs`.
    """

    role: str
    """Either ``'user'`` or ``'assistant'``."""
    content: str | list[dict[str, Any]]
    """The turn's text, or a list of native Anthropic content blocks."""


def build_system_blocks(extra: str | None = None) -> list[dict[str, Any]]:
    """Return the Anthropic ``system`` parameter with a prompt-cache breakpoint.

    The first block is :data:`PERSONA` as a text block carrying
    ``cache_control={'type': 'ephemeral'}`` so the stable prefix is cached across
    calls. When ``extra`` is given (for example the SCHEMA.md text), it is appended as
    a second, uncached text block.

    Args:
        extra: Optional extra system text to append uncached after the persona.

    Returns:
        A list of Anthropic system content blocks.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": PERSONA,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if extra is not None:
        blocks.append({"type": "text", "text": extra})
    return blocks


def build_create_kwargs(
    config: Config,
    messages: Sequence[Message],
    *,
    system_extra: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Assemble the keyword arguments for ``messages.create``.

    Pure and side-effect-free: no network and no SDK import. The model defaults to
    ``config.anthropic_model`` unless ``model`` overrides it; the system parameter is
    :func:`build_system_blocks` (persona + optional ``system_extra``); the messages are
    rendered to ``[{'role': ..., 'content': ...}, ...]``; ``tools`` is included only
    when provided.

    Args:
        config: The frozen runtime config supplying the default model id.
        messages: The conversation turns to send.
        system_extra: Optional uncached extra system text.
        max_tokens: Maximum tokens to generate.
        tools: Optional tool definitions to pass through.
        model: Optional model id overriding ``config.anthropic_model``.

    Returns:
        A kwargs dict suitable for ``client.messages.create(**kwargs)``.
    """
    kwargs: dict[str, Any] = {
        "model": model if model is not None else config.anthropic_model,
        "max_tokens": max_tokens,
        "system": build_system_blocks(system_extra),
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    if tools is not None:
        kwargs["tools"] = tools
    return kwargs


def make_client(config: Config) -> AnthropicLike:
    """Lazily import ``anthropic`` and build an authenticated client.

    The ``anthropic`` import happens **only** inside this function, so merely importing
    :mod:`thoth.llm` (or constructing an :class:`LLM`) never needs the package. The API
    key is read via ``config.require_anthropic()``, which raises
    :class:`thoth.config.ConfigError` *before* the import is attempted when no key is
    set.

    Args:
        config: The frozen runtime config carrying the Anthropic API key.

    Returns:
        An :class:`AnthropicLike` client (a real ``anthropic.Anthropic`` instance).
    """
    api_key = config.require_anthropic()
    from anthropic import Anthropic

    return Anthropic(api_key=api_key)


class LLM:
    """Thin wrapper holding a :class:`~thoth.config.Config` plus an Anthropic client.

    The client is injectable for tests; when omitted it is created lazily via
    :func:`make_client` on first use, so constructing an :class:`LLM` never imports the
    ``anthropic`` package.
    """

    def __init__(
        self,
        config: Config,
        client: AnthropicLike | None = None,
        *,
        guard: BudgetGuardLike | None = None,
    ) -> None:
        """Store the config, an optional pre-built client, and an optional budget guard.

        Args:
            config: The frozen runtime config.
            client: An optional injected client; created lazily on first
                :meth:`complete` when ``None``.
            guard: An optional daily-spend guard (:class:`thoth.budget.BudgetGuard`);
                when wired, :meth:`complete` charges one Anthropic call against the
                daily budget *before* the request and raises
                :class:`thoth.budget.BudgetExceededError` once the cap is reached.
                ``None`` (the default) disables the cap, so existing callers are
                unaffected.
        """
        self._config = config
        self._client = client
        self._guard = guard

    @property
    def config(self) -> Config:
        """The frozen runtime config this wrapper was built with."""
        return self._config

    @property
    def client(self) -> AnthropicLike:
        """The Anthropic client, created lazily via :func:`make_client` on first use."""
        if self._client is None:
            self._client = make_client(self._config)
        return self._client

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_extra: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> Any:
        """Call ``client.messages.create`` with assembled kwargs; return the response.

        Args:
            messages: The conversation turns to send.
            system_extra: Optional uncached extra system text.
            max_tokens: Maximum tokens to generate.
            tools: Optional tool definitions to pass through.
            model: Optional model id overriding ``config.anthropic_model`` (e.g. a
                cheaper Haiku for the Slack intent gate).

        Returns:
            The raw response object returned by the client.

        Raises:
            thoth.budget.BudgetExceededError: when a budget guard is wired and the daily
                call cap has been reached (raised before the request, so nothing is
                spent; the ingest passes treat it as a deferral).
        """
        if self._guard is not None:
            # Charge before the request so a cap-reached day defers rather than spends;
            # every attempt (including retries) counts against the cap (issue #16).
            self._guard.charge(KIND_ANTHROPIC)
        kwargs = build_create_kwargs(
            self._config,
            messages,
            system_extra=system_extra,
            max_tokens=max_tokens,
            tools=tools,
            model=model,
        )
        return self.client.messages.create(**kwargs)


def extract_text(response: Any) -> str:
    """Concatenate the text from an Anthropic response's content blocks.

    Tolerant of both the real SDK response (objects whose blocks have ``.type`` and
    ``.text`` attributes) and a fake shaped ``{'content': [{'type': 'text', 'text':
    ...}]}``. Non-text blocks (for example ``tool_use``) are ignored.

    Args:
        response: An Anthropic response object or a dict-shaped stand-in.

    Returns:
        The concatenated text of every ``text`` block, in order.
    """
    content = (
        response.get("content")
        if isinstance(response, dict)
        else getattr(response, "content", None)
    )
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            text = block.get("text")
        else:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _block_as_dict(block: Any) -> dict[str, Any]:
    """Normalise one content block (an SDK object or a dict) to a plain ``dict``.

    The real Anthropic SDK returns content blocks as typed objects; a test fake returns
    plain dicts. To echo the assistant's ``tool_use`` block(s) back into the next
    request verbatim (so the ``tool_use_id`` keys line up with the ``tool_result``
    blocks the harness sends), each block is reduced to the JSON-able dict the Messages
    API expects. An object is converted via its ``.model_dump()`` (Pydantic v2, what the
    SDK uses) or, failing that, by reading the documented attributes for the block type.

    Args:
        block: A content block (typed SDK object or a dict-shaped stand-in).

    Returns:
        A plain ``dict`` suitable to place back into a ``messages`` content list.
    """
    if isinstance(block, dict):
        return dict(block)
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    block_type = getattr(block, "type", None)
    out: dict[str, Any] = {"type": block_type}
    if block_type == "text":
        out["text"] = getattr(block, "text", "")
    elif block_type == "tool_use":
        out["id"] = getattr(block, "id", None)
        out["name"] = getattr(block, "name", None)
        out["input"] = getattr(block, "input", {})
    return out


def response_content_blocks(response: Any) -> list[dict[str, Any]]:
    """Return a response's content blocks as plain dicts, ready to re-send.

    This is the structured counterpart of :func:`extract_text`: where that flattens a
    response to its text, this preserves *every* block (text and ``tool_use`` alike) so
    an assistant turn can be echoed verbatim into the next ``messages.create`` call. The
    ``tool_use`` blocks must round-trip with their ``id`` intact so the following
    ``tool_result`` blocks can reference them (the API requires the match).

    Args:
        response: An Anthropic response object or a dict-shaped stand-in.

    Returns:
        The content blocks as plain dicts, in order (empty when there is no content).
    """
    content = (
        response.get("content")
        if isinstance(response, dict)
        else getattr(response, "content", None)
    )
    if content is None:
        return []
    return [_block_as_dict(block) for block in content]


def assistant_blocks_message(response: Any) -> Message:
    """Wrap a response's content blocks as an assistant :class:`Message` to re-send.

    The returned message carries the native blocks (including any ``tool_use`` blocks),
    so appending it to the running transcript reproduces the assistant turn exactly --
    the precondition the Messages API places on the turn that *precedes* a user
    ``tool_result`` turn.

    Args:
        response: The Anthropic response whose assistant turn is being echoed.

    Returns:
        A :class:`Message` with ``role='assistant'`` and structured-block content.
    """
    return Message(role="assistant", content=response_content_blocks(response))


def tool_result_block(
    tool_use_id: str, content: str, *, is_error: bool = False
) -> dict[str, Any]:
    """Build one ``tool_result`` content block keyed to a prior ``tool_use`` block.

    The Messages API requires the user turn after a ``stop_reason='tool_use'`` response
    to contain a ``tool_result`` block whose ``tool_use_id`` matches the originating
    ``tool_use`` block; ``is_error=True`` marks a tool failure the model should recover
    from (an SSRF/extract rejection, an unknown tool, a bad argument).

    Args:
        tool_use_id: The ``id`` of the ``tool_use`` block this result answers.
        content: The textual tool output (or error message).
        is_error: Whether the tool failed (sets the API ``is_error`` flag).

    Returns:
        A ``tool_result`` content-block dict.
    """
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


def user_blocks_message(blocks: list[dict[str, Any]]) -> Message:
    """Wrap a list of content blocks (typically ``tool_result``) as a user turn.

    Args:
        blocks: The content blocks to send as one user turn.

    Returns:
        A :class:`Message` with ``role='user'`` and structured-block content.
    """
    return Message(role="user", content=blocks)


# Matches a ```json ... ``` (or bare ``` ... ```) fenced block; group 1 is the body.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_json_block(text: str) -> dict[str, Any]:
    """Extract and parse the first JSON object from model text.

    Strips a ```` ```json ```` (or bare ```` ``` ````) fence if present, otherwise
    parses from the first ``{`` to the matching end of the string. The decoded value
    must be a JSON object (``dict``).

    Args:
        text: The model's text output.

    Returns:
        The decoded JSON object.

    Raises:
        LLMError: if no JSON object is found or the JSON is invalid.
    """
    candidate: str | None = None
    fence = _FENCE_RE.search(text)
    if fence is not None:
        candidate = fence.group(1).strip()
    else:
        start = text.find("{")
        if start != -1:
            candidate = text[start:].strip()
    if not candidate:
        raise LLMError("no JSON object found in model output")
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON in model output: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError(f"expected a JSON object but parsed a {type(obj).__name__}")
    return obj


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

    wikilinks = page.get("wikilinks")
    if not isinstance(wikilinks, list):
        problems.append(f"{where}: 'wikilinks' must be a list")
    elif len(wikilinks) < _MIN_WIKILINKS:
        problems.append(
            f"{where}: needs >= {_MIN_WIKILINKS} wikilinks, got {len(wikilinks)}"
        )

    _check_frontmatter(page.get("frontmatter"), folder, where, problems)


def validate_file_plan(obj: dict[str, Any]) -> None:
    """Validate a file-plan against :data:`FILE_PLAN_SCHEMA` and the vault contract.

    Reuses :mod:`thoth.vault`'s validators so a passing plan is guaranteed to survive
    :meth:`thoth.vault.Vault.write_page`. Each ``pages[*]`` entry is checked for a known
    ``action``, a valid ``slug``, an allowed ``folder`` x ``type`` pairing, the required
    common frontmatter fields, a valid ``source``, and ``>= 2`` ``wikilinks``. Any
    ``index_entries`` and ``log`` block are shape-checked too.

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

    entries = obj.get("index_entries")
    if entries is not None:
        if not isinstance(entries, list):
            problems.append("'index_entries' must be a list")
        else:
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    problems.append(f"index_entries[{idx}]: must be an object")
                    continue
                for field in ("section", "wikilink", "summary"):
                    if field not in entry:
                        problems.append(f"index_entries[{idx}]: missing '{field}'")

    log = obj.get("log")
    if log is not None:
        if not isinstance(log, dict):
            problems.append("'log' must be an object")
        else:
            for field in ("action", "subject", "files"):
                if field not in log:
                    problems.append(f"log: missing '{field}'")
            log_action = log.get("action")
            if log_action is not None and log_action not in LOG_ACTIONS:
                problems.append(
                    f"log: invalid action {log_action!r} (allowed: "
                    f"{', '.join(sorted(LOG_ACTIONS))})"
                )
            files = log.get("files")
            if files is not None and not isinstance(files, list):
                problems.append("log: 'files' must be a list")

    if problems:
        raise SchemaValidationError(
            "file plan failed validation: " + "; ".join(problems)
        )


def validate_answer(obj: dict[str, Any]) -> None:
    """Validate a blended-answer object against :data:`ANSWER_SCHEMA`.

    Args:
        obj: The decoded answer object.

    Raises:
        SchemaValidationError: listing every problem found.
    """
    problems: list[str] = []

    answer = obj.get("answer")
    if not isinstance(answer, str):
        problems.append("'answer' must be a string")
    elif not answer.strip():
        problems.append("'answer' must not be empty")

    page_paths = obj.get("page_paths")
    if not isinstance(page_paths, list):
        problems.append("'page_paths' must be a list")
    elif not all(isinstance(p, str) for p in page_paths):
        problems.append("'page_paths' must be a list of strings")

    used_web = obj.get("used_web")
    if not isinstance(used_web, bool):
        problems.append("'used_web' must be a boolean")

    web_sources = obj.get("web_sources")
    if not isinstance(web_sources, list):
        problems.append("'web_sources' must be a list")
    elif not all(isinstance(s, str) for s in web_sources):
        problems.append("'web_sources' must be a list of strings")

    if problems:
        raise SchemaValidationError("answer failed validation: " + "; ".join(problems))
