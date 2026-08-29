"""Response-shape helpers tolerant of SDK objects and dict-shaped test fakes."""

from __future__ import annotations

import json
import re
from typing import Any

from .client import LLMError, Message


def _field(obj: Any, key: str) -> Any:
    """Reads a key from a dict or an attribute-style object.

    The real SDK returns responses and blocks as typed objects while test fakes use
    plain dicts, so every helper here reads both through this one accessor.
    """
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _content_list(response: Any) -> list[Any]:
    """Returns a response's content blocks, or an empty list when there are none."""
    content = _field(response, "content")
    return [] if content is None else content


def extract_text(response: Any) -> str:
    """Concatenates the text from a response's content blocks.

    Tolerant of both the real SDK response and a dict-shaped fake. Non-text blocks such
    as ``tool_use`` are ignored.

    Args:
        response: A response object or a dict-shaped stand-in.

    Returns:
        The concatenated text of every text block, in order.
    """
    parts: list[str] = []
    for block in _content_list(response):
        text = _field(block, "text")
        if _field(block, "type") == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _block_as_dict(block: Any) -> dict[str, Any]:
    """Normalises one content block to a plain dict.

    Echoing the assistant's tool_use blocks back verbatim keeps the ids lined up with
    the tool_result blocks the harness sends, so each block is reduced to the JSON-able
    dict the Messages API expects. An object is converted through its Pydantic dump, or
    failing that by reading the documented attributes for its block type.

    Args:
        block: A typed SDK block or a dict-shaped stand-in.

    Returns:
        A plain dict ready to place back into a content list.
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
    """Returns a response's content blocks as plain dicts, ready to re-send.

    The structured counterpart of :func:`extract_text`. Where that flattens a response
    to its text, this preserves every block so an assistant turn can be echoed verbatim
    into the next call. The tool_use blocks must round-trip with their ids intact,
    because the API requires the following tool_result blocks to match them.

    Args:
        response: A response object or a dict-shaped stand-in.

    Returns:
        The blocks as plain dicts, in order, and empty when there is no content.
    """
    return [_block_as_dict(block) for block in _content_list(response)]


# ---- tool-use response-shape helpers (tolerant of SDK objects and dict fakes) -----


def _tool_use_blocks(response: Any) -> list[Any]:
    """Returns the tool_use content blocks of a response.

    :func:`extract_text` deliberately ignores these, so a tool-use caller inspects the
    content itself. Tolerant of both the real SDK shape and a dict-shaped fake.

    Args:
        response: A response object or a dict-shaped stand-in.

    Returns:
        The tool_use blocks, in order and possibly empty.
    """
    return [
        block
        for block in _content_list(response)
        if _field(block, "type") == "tool_use"
    ]


def _block_name(block: Any) -> str:
    """Returns a tool_use block's tool name, or "" when absent."""
    name = _field(block, "name")
    return name if isinstance(name, str) else ""


def _block_id(block: Any) -> str:
    """Returns a tool_use block's id, or "" when absent.

    The id keys the matching tool_result block in the next user turn, so it must be
    carried through verbatim. The Messages API rejects a result whose id matches no
    prior tool_use block.
    """
    value = _field(block, "id")
    return value if isinstance(value, str) else ""


def _block_input(block: Any) -> dict[str, Any]:
    """Returns a tool_use block's input map, or {} when absent or ill-typed."""
    value = _field(block, "input")
    return value if isinstance(value, dict) else {}


def extract_tool_use(response: Any, name: str) -> dict[str, Any] | None:
    """Returns the input dict of the first tool_use block with a given name.

    Built on the tolerant block helpers, so it works against both the real SDK response
    and a dict-shaped fake. A forced tool call makes the model return a structured input
    dict the transport escapes, so a body with raw newlines or non-breaking spaces can
    never break JSON parsing.

    Args:
        response: A response object or a dict-shaped stand-in.
        name: The tool name to match.

    Returns:
        The matching block's input dict, or None when no block matches.
    """
    for block in _tool_use_blocks(response):
        if _block_name(block) == name:
            return _block_input(block)
    return None


def assistant_blocks_message(response: Any) -> Message:
    """Wraps a response's content blocks as an assistant message to re-send.

    The message carries the native blocks, including any tool_use, so appending it to
    the transcript reproduces the assistant turn exactly. That is the precondition the
    Messages API places on the turn preceding a user tool_result turn.

    Args:
        response: The response whose assistant turn is being echoed.

    Returns:
        An assistant message with structured-block content.
    """
    return Message(role="assistant", content=response_content_blocks(response))


def tool_result_block(
    tool_use_id: str, content: str, *, is_error: bool = False
) -> dict[str, Any]:
    """Builds one tool_result block keyed to a prior tool_use block.

    The Messages API requires the user turn after a tool-use response to contain a
    result whose id matches the originating block.

    Args:
        tool_use_id: The id of the block this result answers.
        content: The textual tool output, or an error message.
        is_error: Marks a tool failure the model should recover from, such as an
            SSRF rejection, an unknown tool, or a bad argument.

    Returns:
        The tool_result content block.
    """
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


# Matches a json or bare fenced block, where group 1 is the body
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_json_block(text: str) -> dict[str, Any]:
    """Extracts and parses the first JSON object from model text.

    Strips a fence when present, otherwise parses from the first brace. The decoded
    value must be an object rather than any other JSON type.

    Args:
        text: The model's text output.

    Returns:
        The decoded JSON object.

    Raises:
        LLMError: if no object is found or the JSON is invalid.
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
