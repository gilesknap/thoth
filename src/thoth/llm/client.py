"""The injectable Anthropic client wrapper and the prompt-caching kwargs builders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from thoth.budget import KIND_ANTHROPIC, BudgetGuardLike
from thoth.config import Config

from .persona import DEFAULT_MAX_TOKENS, PERSONA


class LLMError(Exception):
    """Base error for LLM wrapper problems."""


class SchemaValidationError(LLMError):
    """Raised when model JSON output fails schema validation.

    The message lists every violation found, so a caller or a retry prompt sees all the
    problems at once rather than one at a time.
    """


class AnthropicLike(Protocol):
    """Structural type for the slice of the SDK this module uses.

    Anything exposing a messages attribute whose create returns a response satisfies it,
    so tests inject a small fake and need no real SDK.
    """

    @property
    def messages(self) -> Any:
        """The messages namespace exposing ``create``."""
        ...


@dataclass(frozen=True, slots=True)
class Message:
    """One chat turn handed to the model.

    The content is either a plain string for an ordinary turn, or a list of native
    content blocks. The structured form is what a multi-turn tool-use conversation
    requires: the assistant turn after a tool-use response must echo the model's blocks
    and the following user turn must carry a result per id, because the API rejects a
    tool-use exchange flattened to text. A block list passes through to the create call
    verbatim.
    """

    role: str
    """Either ``'user'`` or ``'assistant'``."""
    content: str | list[dict[str, Any]]
    """The turn's text, or a list of native Anthropic content blocks."""


def build_system_blocks(extra: str | None = None) -> list[dict[str, Any]]:
    """Returns the system parameter with a prompt-cache breakpoint.

    The first block is the persona, marked ephemeral so the stable prefix is cached
    across calls. Any extra text is appended as a second, uncached block.

    Args:
        extra: Optional system text appended uncached after the persona.

    Returns:
        The system content blocks.
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
    tool_choice: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Assembles the keyword arguments for the create call.

    Pure and side-effect-free, with no network and no SDK import.

    Args:
        config: Frozen runtime config supplying the default model id.
        messages: The conversation turns to send.
        system_extra: Optional uncached extra system text.
        max_tokens: Maximum tokens to generate.
        tools: Optional tool definitions, included only when provided.
        tool_choice: Optional directive forcing a specific tool, included only when
            provided.
        model: Model id overriding the configured default.

    Returns:
        The kwargs for the create call.
    """
    kwargs: dict[str, Any] = {
        "model": model if model is not None else config.anthropic_model,
        "max_tokens": max_tokens,
        "system": build_system_blocks(system_extra),
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    return kwargs


def make_client(config: Config) -> AnthropicLike:
    """Lazily imports the SDK and builds an authenticated client.

    The import happens only inside this function, so importing :mod:`thoth.llm` or
    constructing an :class:`LLM` never needs the package. The key is read first, so a
    missing key raises before the import is attempted.

    Args:
        config: Frozen runtime config carrying the API key.

    Returns:
        The authenticated client.
    """
    api_key = config.require_anthropic()
    from anthropic import Anthropic

    return Anthropic(api_key=api_key)


class LLM:
    """Thin wrapper holding a config plus an Anthropic client.

    The client is injectable for tests, and when omitted it is created lazily on first
    use, so constructing this never imports the SDK.
    """

    def __init__(
        self,
        config: Config,
        client: AnthropicLike | None = None,
        *,
        guard: BudgetGuardLike | None = None,
    ) -> None:
        """Stores the config, an optional client, and an optional budget guard.

        Args:
            config: The frozen runtime config.
            client: Injected client, or None to create one lazily on first use.
            guard: Optional daily-spend guard. When wired, each completion charges one
                call before the request and raises once the cap is reached. None
                disables the cap.
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
        """The Anthropic client, created lazily on first use."""
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
        tool_choice: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Any:
        """Calls the create endpoint with assembled kwargs and returns the response.

        Args:
            messages: The conversation turns to send.
            system_extra: Optional uncached extra system text.
            max_tokens: Maximum tokens to generate.
            tools: Optional tool definitions to pass through.
            tool_choice: Optional directive forcing or steering tool use.
            model: Model id overriding the configured default, such as a cheaper one
                for the intent gate.

        Returns:
            The raw response object.

        Raises:
            thoth.budget.BudgetExceededError: when a guard is wired and the daily cap
                is reached. It raises before the request, so nothing is spent and the
                ingest passes treat it as a deferral.
        """
        if self._guard is not None:
            # Charge before the request so a capped day defers rather than spends.
            # Every attempt, retries included, counts against the cap (issue #16)
            self._guard.charge(KIND_ANTHROPIC)
        kwargs = build_create_kwargs(
            self._config,
            messages,
            system_extra=system_extra,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
        )
        return self.client.messages.create(**kwargs)
