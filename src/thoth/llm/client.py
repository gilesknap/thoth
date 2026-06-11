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
    tool_choice: dict[str, Any] | None = None,
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
        tool_choice: Optional ``tool_choice`` directive (e.g. forcing a specific tool
            via ``{"type": "tool", "name": ...}``); included only when provided.
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
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
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
        tool_choice: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Any:
        """Call ``client.messages.create`` with assembled kwargs; return the response.

        Args:
            messages: The conversation turns to send.
            system_extra: Optional uncached extra system text.
            max_tokens: Maximum tokens to generate.
            tools: Optional tool definitions to pass through.
            tool_choice: Optional ``tool_choice`` directive forcing/steering tool use.
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
            tool_choice=tool_choice,
            model=model,
        )
        return self.client.messages.create(**kwargs)
