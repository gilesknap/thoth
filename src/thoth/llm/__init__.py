"""Anthropic client wrapper, the PKM persona, and the file-plan contract.

This package owns three framework-independent things the ingest and query phases
build on:

* the verbatim **PKM Agent Persona** system-prompt string (:data:`PERSONA`), lifted
  from the SPEC Appendix, which makes the vault canonical, makes Hindsight a derived
  index, and bakes in the ``obsidian://`` retrieval format and a concise tone;
* helpers that assemble the ``messages.create`` keyword arguments with prompt caching
  (a stable :data:`PERSONA` prefix carrying a ``cache_control`` breakpoint), plus a
  thin injectable :class:`LLM` wrapper around the Anthropic SDK; and
* the curate file-plan contract (:func:`file_plan_contract_text`) the harness holds
  model output to, with its validator (:func:`validate_file_plan`).

The ``anthropic`` SDK is imported **lazily**, only inside :func:`make_client`, so that
importing :mod:`thoth.llm` (for example at pytest collection or by a tool that only
needs the contract) never requires the package to be installed. The client is also
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

from .client import (
    LLM,
    AnthropicLike,
    LLMError,
    Message,
    SchemaValidationError,
    build_create_kwargs,
    build_system_blocks,
    make_client,
)
from .contract import file_plan_contract_text
from .persona import DEFAULT_MAX_TOKENS, PERSONA
from .responses import _block_id as _block_id
from .responses import _block_name as _block_name
from .responses import _tool_use_blocks as _tool_use_blocks
from .responses import (
    assistant_blocks_message,
    extract_text,
    extract_tool_use,
    parse_json_block,
    response_content_blocks,
    tool_result_block,
)
from .validation import validate_file_plan

__all__ = [
    "DEFAULT_MAX_TOKENS",
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
    "extract_tool_use",
    "make_client",
    "parse_json_block",
    "response_content_blocks",
    "tool_result_block",
    "validate_file_plan",
]
