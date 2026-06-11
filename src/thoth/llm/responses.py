"""Response-shape helpers tolerant of SDK objects and dict-shaped test fakes."""

from __future__ import annotations

import json
import re
from typing import Any

from .client import LLMError, Message


def _field(obj: Any, key: str) -> Any:
    """Read ``key`` from a dict or an attribute-style object (``None`` when absent).

    The real Anthropic SDK returns responses and content blocks as typed objects;
    test fakes use plain dicts. Every response-shape helper here reads both through
    this one accessor.
    """
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _content_list(response: Any) -> list[Any]:
    """Return a response's content blocks (``[]`` when there is no content)."""
    content = _field(response, "content")
    return [] if content is None else content


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
    parts: list[str] = []
    for block in _content_list(response):
        text = _field(block, "text")
        if _field(block, "type") == "text" and isinstance(text, str):
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
    return [_block_as_dict(block) for block in _content_list(response)]


# ---- tool-use response-shape helpers (tolerant of SDK objects and dict fakes) -----


def _tool_use_blocks(response: Any) -> list[Any]:
    """Return the ``tool_use`` content blocks of a response (objects or dicts).

    :func:`extract_text` deliberately ignores ``tool_use`` blocks, so a tool-use caller
    inspects ``response.content`` itself. Tolerant of the real SDK shape (blocks with a
    ``.type`` attribute) and a dict-shaped fake
    (``{'content': [{'type': 'tool_use', ...}]}``).

    Args:
        response: An Anthropic response object or a dict-shaped stand-in.

    Returns:
        The list of ``tool_use`` blocks, in order (possibly empty).
    """
    return [
        block
        for block in _content_list(response)
        if _field(block, "type") == "tool_use"
    ]


def _block_name(block: Any) -> str:
    """Return a ``tool_use`` block's tool ``name`` as a string (``''`` when absent)."""
    name = _field(block, "name")
    return name if isinstance(name, str) else ""


def _block_id(block: Any) -> str:
    """Return a ``tool_use`` block's ``id`` as a string (``''`` when absent).

    The id keys the matching ``tool_result`` block in the next user turn, so it must be
    carried through verbatim (the Messages API rejects a ``tool_result`` whose
    ``tool_use_id`` matches no prior ``tool_use`` block).
    """
    value = _field(block, "id")
    return value if isinstance(value, str) else ""


def _block_input(block: Any) -> dict[str, Any]:
    """Return a ``tool_use`` block's ``input`` map (``{}`` when absent/ill-typed)."""
    value = _field(block, "input")
    return value if isinstance(value, dict) else {}


def extract_tool_use(response: Any, name: str) -> dict[str, Any] | None:
    """Return the input dict of the first ``tool_use`` block named ``name``.

    Built on the tolerant block helpers, so it works against both the real SDK response
    (where ``tool_use.input`` is an already-parsed dict) and a dict-shaped test fake. A
    forced tool call (``tool_choice={"type": "tool", "name": name}``) makes the model
    return a structured ``tool_use.input`` dict the SDK/transport escapes for us, so a
    body with raw newlines / non-breaking spaces can never break JSON parsing.

    Args:
        response: An Anthropic response object or a dict-shaped stand-in.
        name: The tool name to match.

    Returns:
        The matching block's ``input`` dict, or ``None`` when no block matches.
    """
    for block in _tool_use_blocks(response):
        if _block_name(block) == name:
            return _block_input(block)
    return None


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
