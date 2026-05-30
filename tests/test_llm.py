"""Tests for :mod:`thoth.llm`."""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

import pytest

from thoth.config import Config, ConfigError, load_config
from thoth.llm import (
    DATED_MODEL_FALLBACK,
    DEFAULT_MAX_TOKENS,
    LLM,
    PERSONA,
    LLMError,
    Message,
    SchemaValidationError,
    build_create_kwargs,
    build_system_blocks,
    extract_text,
    make_client,
    parse_json_block,
    validate_answer,
    validate_file_plan,
)

# anthropic is absent in CI; gate the one test that would touch the real SDK import.
HAVE_ANTHROPIC = importlib.util.find_spec("anthropic") is not None

# Obviously-fake placeholder only (gitleaks scans the commit).
FAKE_TOKEN = "test-token"


@pytest.fixture
def config() -> Config:
    """A minimal frozen Config for a fake vault path (no disk needed)."""
    return load_config({"PKM_VAULT": "/x"})


# --- a tiny injectable fake client ------------------------------------------


class _FakeMessages:
    """Records the kwargs of the last ``create`` call and returns a canned response."""

    def __init__(self, response: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def create(self, **kwargs: Any) -> Any:
        """Record kwargs and return the canned response."""
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    """Structural stand-in for the Anthropic SDK exposing ``.messages.create``."""

    def __init__(self, response: Any) -> None:
        self.messages = _FakeMessages(response)


# --- PERSONA invariants ------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "# PKM Agent Persona",
        "canonical",
        "Hindsight is a rebuildable index over the vault",
        "obsidian://open?vault=pkm-vault&file=<url-encoded vault-relative path>",
        "Europe/London",
    ],
)
def test_persona_contains_load_bearing_invariants(needle: str) -> None:
    """The persona keeps the invariants later phases depend on, verbatim."""
    assert needle in PERSONA


def test_persona_has_concise_tone_clause() -> None:
    """The concise-tone clause survives (it shapes every reply)."""
    assert "## Tone" in PERSONA
    assert "Concise" in PERSONA
    assert "not a\n  conversationalist" in PERSONA


def test_dated_model_fallback_value() -> None:
    """The proven dated fallback id is exposed for callers that hit a 404 alias."""
    assert DATED_MODEL_FALLBACK == "claude-sonnet-4-20250514"


# --- build_system_blocks -----------------------------------------------------


def test_build_system_blocks_caches_persona_prefix() -> None:
    """First block is the persona text with an ephemeral cache_control breakpoint."""
    blocks = build_system_blocks()
    assert len(blocks) == 1
    first = blocks[0]
    assert first["type"] == "text"
    assert first["text"] == PERSONA
    assert first["cache_control"] == {"type": "ephemeral"}


def test_build_system_blocks_appends_uncached_extra() -> None:
    """A given extra becomes a second, uncached text block after the persona."""
    blocks = build_system_blocks("SCHEMA TEXT")
    assert len(blocks) == 2
    assert blocks[0]["text"] == PERSONA
    assert "cache_control" in blocks[0]
    assert blocks[1] == {"type": "text", "text": "SCHEMA TEXT"}
    assert "cache_control" not in blocks[1]


# --- build_create_kwargs -----------------------------------------------------


def test_build_create_kwargs_defaults(config: Config) -> None:
    """Defaults use config.anthropic_model and render messages to role/content dicts."""
    msgs = [Message("user", "hello"), Message("assistant", "hi")]
    kwargs = build_create_kwargs(config, msgs)
    assert kwargs["model"] == config.anthropic_model
    assert kwargs["max_tokens"] == DEFAULT_MAX_TOKENS
    assert kwargs["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert kwargs["system"] == build_system_blocks()
    # tools omitted unless provided.
    assert "tools" not in kwargs


def test_build_create_kwargs_honours_overrides(config: Config) -> None:
    """model / max_tokens / system_extra / tools overrides flow through."""
    tools = [{"name": "search_vault"}]
    kwargs = build_create_kwargs(
        config,
        [Message("user", "q")],
        system_extra="EXTRA",
        max_tokens=512,
        tools=tools,
        model="claude-x-override",
    )
    assert kwargs["model"] == "claude-x-override"
    assert kwargs["max_tokens"] == 512
    assert kwargs["tools"] == tools
    assert kwargs["system"][-1] == {"type": "text", "text": "EXTRA"}


# --- LLM construction + lazy import seam ------------------------------------


def test_constructing_llm_does_not_import_anthropic(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building an LLM must never import the (CI-absent) anthropic package."""
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    LLM(config)
    assert "anthropic" not in sys.modules


def test_llm_complete_calls_create_once_with_assembled_kwargs(
    config: Config,
) -> None:
    """complete() forwards build_create_kwargs to the injected client exactly once."""
    canned = {"content": [{"type": "text", "text": "ok"}]}
    client = _FakeClient(canned)
    llm = LLM(config, client=client)
    msgs = [Message("user", "hello")]
    result = llm.complete(msgs, max_tokens=256)
    assert result is canned
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0] == build_create_kwargs(config, msgs, max_tokens=256)


def test_llm_uses_injected_client_without_make_client(config: Config) -> None:
    """An injected client is returned by the .client property (no make_client)."""
    client = _FakeClient({"content": []})
    llm = LLM(config, client=client)
    assert llm.client is client
    assert llm.config is config


# --- extract_text ------------------------------------------------------------


def test_extract_text_concatenates_text_blocks_ignoring_others() -> None:
    """Multiple text blocks join in order; non-text blocks are skipped."""
    response = {
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "tool_use", "name": "x", "input": {}},
            {"type": "text", "text": "world"},
        ]
    }
    assert extract_text(response) == "Hello world"


def test_extract_text_tolerates_object_blocks() -> None:
    """It also reads attribute-style blocks (real-SDK shape)."""

    class _Block:
        def __init__(self, type_: str, text: str | None = None) -> None:
            self.type = type_
            self.text = text

    class _Resp:
        def __init__(self) -> None:
            self.content = [_Block("text", "a"), _Block("image"), _Block("text", "b")]

    assert extract_text(_Resp()) == "ab"


def test_extract_text_empty_when_no_content() -> None:
    """A response with no content yields the empty string, not an error."""
    assert extract_text({}) == ""
    assert extract_text({"content": []}) == ""


# --- parse_json_block --------------------------------------------------------


def test_parse_json_block_strips_json_fence() -> None:
    """A ```json fenced object is extracted and parsed."""
    text = 'Here you go:\n```json\n{"answer": "42", "n": 1}\n```\nDone.'
    assert parse_json_block(text) == {"answer": "42", "n": 1}


def test_parse_json_block_bare_object() -> None:
    """A bare object (no fence) is parsed from the first brace."""
    text = 'prose then {"a": [1, 2], "b": {"c": 3}} trailing'
    assert parse_json_block(text) == {"a": [1, 2], "b": {"c": 3}}


def test_parse_json_block_raises_on_no_json() -> None:
    """Prose with no JSON object raises LLMError."""
    with pytest.raises(LLMError, match="no JSON object"):
        parse_json_block("just some prose, nothing structured")


def test_parse_json_block_raises_on_invalid_json() -> None:
    """A malformed object raises LLMError mentioning invalid JSON."""
    with pytest.raises(LLMError, match="invalid JSON"):
        parse_json_block('```json\n{"a": }\n```')


def test_parse_json_block_rejects_non_object_json() -> None:
    """A JSON array (not an object) is rejected."""
    with pytest.raises(LLMError, match="expected a JSON object"):
        parse_json_block("```json\n[1, 2, 3]\n```")


# --- file-plan fixtures + validation ----------------------------------------


def _good_page(slug: str = "program-motion-controller") -> dict[str, Any]:
    """A well-formed file-plan page that must pass validate_file_plan."""
    return {
        "action": "create",
        "folder": "entities",
        "slug": slug,
        "frontmatter": {
            "title": "Program Motion Controller",
            "type": "entity",
            "created": "2026-05-30",
            "updated": "2026-05-30",
            "source": "slack",
            "tags": ["controls"],
        },
        "body": "PMC coordinates motion. See [[drive-control-module]].",
        "wikilinks": ["drive-control-module", "motor-rail-api"],
        "embeds": [],
    }


def _good_plan() -> dict[str, Any]:
    """A two-page plan with index entries and a log block, all well-formed."""
    return {
        "pages": [_good_page(), _good_page("drive-control-module")],
        "index_entries": [
            {
                "section": "Entities",
                "wikilink": "program-motion-controller",
                "summary": "central coordinator",
            }
        ],
        "log": {
            "action": "ingest",
            "subject": "motor control",
            "files": ["entities/program-motion-controller.md"],
        },
    }


def test_validate_file_plan_accepts_well_formed_plan() -> None:
    """A fully valid plan passes without raising."""
    validate_file_plan(_good_plan())


def test_validate_file_plan_accepts_minimal_pages_only() -> None:
    """A plan with just a valid 'pages' list (no index/log) is acceptable."""
    validate_file_plan({"pages": [_good_page()]})


def test_validate_file_plan_rejects_unknown_action() -> None:
    """An action outside {create, update} is reported."""
    page = _good_page()
    page["action"] = "delete"
    with pytest.raises(SchemaValidationError, match="action"):
        validate_file_plan({"pages": [page]})


def test_validate_file_plan_rejects_folder_type_mismatch() -> None:
    """A type not allowed in the folder is reported (reuses vault contract)."""
    page = _good_page()
    page["folder"] = "entities"
    page["frontmatter"]["type"] = "action"
    with pytest.raises(SchemaValidationError) as exc:
        validate_file_plan({"pages": [page]})
    assert "pages[0]" in str(exc.value)


def test_validate_file_plan_rejects_bad_slug() -> None:
    """A malformed slug is reported (reuses Vault.validate_slug)."""
    page = _good_page()
    page["slug"] = "Bad Slug"
    with pytest.raises(SchemaValidationError):
        validate_file_plan({"pages": [page]})


def test_validate_file_plan_rejects_too_few_wikilinks() -> None:
    """Fewer than two outbound wikilinks is reported."""
    page = _good_page()
    page["wikilinks"] = ["only-one"]
    with pytest.raises(SchemaValidationError, match="wikilinks"):
        validate_file_plan({"pages": [page]})


def test_validate_file_plan_rejects_missing_required_field() -> None:
    """A missing required common frontmatter field names that field."""
    page = _good_page()
    del page["frontmatter"]["title"]
    with pytest.raises(SchemaValidationError, match="title"):
        validate_file_plan({"pages": [page]})


def test_validate_file_plan_rejects_invalid_source() -> None:
    """A source outside VALID_SOURCES is reported."""
    page = _good_page()
    page["frontmatter"]["source"] = "carrier-pigeon"
    with pytest.raises(SchemaValidationError, match="source"):
        validate_file_plan({"pages": [page]})


def test_validate_file_plan_rejects_empty_pages() -> None:
    """An empty pages list is rejected (nothing to write)."""
    with pytest.raises(SchemaValidationError, match="pages"):
        validate_file_plan({"pages": []})


def test_validate_file_plan_rejects_non_list_pages() -> None:
    """A non-list 'pages' is rejected."""
    with pytest.raises(SchemaValidationError, match="pages"):
        validate_file_plan({"pages": {"not": "a list"}})


def test_validate_file_plan_rejects_bad_log_action() -> None:
    """An unknown log action is reported."""
    plan = _good_plan()
    plan["log"]["action"] = "frobnicate"
    with pytest.raises(SchemaValidationError, match="log"):
        validate_file_plan(plan)


def test_validate_file_plan_rejects_malformed_index_entry() -> None:
    """An index entry missing a required key is reported."""
    plan = _good_plan()
    del plan["index_entries"][0]["summary"]
    with pytest.raises(SchemaValidationError, match="summary"):
        validate_file_plan(plan)


def test_validate_file_plan_collects_multiple_problems() -> None:
    """Every violation is surfaced at once, not just the first."""
    page = _good_page()
    page["action"] = "nope"
    page["wikilinks"] = []
    del page["frontmatter"]["title"]
    with pytest.raises(SchemaValidationError) as exc:
        validate_file_plan({"pages": [page]})
    message = str(exc.value)
    assert "action" in message
    assert "wikilinks" in message
    assert "title" in message


# --- answer validation -------------------------------------------------------


def _good_answer() -> dict[str, Any]:
    """A well-formed blended-answer object."""
    return {
        "answer": "The PMC coordinates motion.",
        "page_paths": ["entities/program-motion-controller.md"],
        "used_web": False,
        "web_sources": [],
    }


def test_validate_answer_accepts_good_answer() -> None:
    """A fully valid answer passes without raising."""
    validate_answer(_good_answer())


def test_validate_answer_accepts_web_answer() -> None:
    """An answer that used the web with sources is valid."""
    obj = _good_answer()
    obj["used_web"] = True
    obj["web_sources"] = ["https://example.com/a"]
    validate_answer(obj)


def test_validate_answer_rejects_missing_answer() -> None:
    """A missing 'answer' key is reported."""
    obj = _good_answer()
    del obj["answer"]
    with pytest.raises(SchemaValidationError, match="answer"):
        validate_answer(obj)


def test_validate_answer_rejects_empty_answer() -> None:
    """A blank 'answer' string is reported."""
    obj = _good_answer()
    obj["answer"] = "   "
    with pytest.raises(SchemaValidationError, match="answer"):
        validate_answer(obj)


def test_validate_answer_rejects_non_list_page_paths() -> None:
    """A non-list 'page_paths' is reported."""
    obj = _good_answer()
    obj["page_paths"] = "entities/foo.md"
    with pytest.raises(SchemaValidationError, match="page_paths"):
        validate_answer(obj)


def test_validate_answer_rejects_non_string_page_paths() -> None:
    """A 'page_paths' list with a non-string element is reported."""
    obj = _good_answer()
    obj["page_paths"] = ["ok", 123]
    with pytest.raises(SchemaValidationError, match="page_paths"):
        validate_answer(obj)


def test_validate_answer_rejects_non_bool_used_web() -> None:
    """A non-boolean 'used_web' is reported."""
    obj = _good_answer()
    obj["used_web"] = "yes"
    with pytest.raises(SchemaValidationError, match="used_web"):
        validate_answer(obj)


# --- make_client seam --------------------------------------------------------


def test_make_client_raises_config_error_without_key_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No API key => ConfigError, and anthropic is never imported (key check first).

    This holds whether or not anthropic is installed: require_anthropic() raises
    before the lazy ``from anthropic import Anthropic`` line is reached.
    """
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    cfg = load_config({"PKM_VAULT": "/x"})
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_client(cfg)
    assert "anthropic" not in sys.modules


@pytest.mark.skipif(HAVE_ANTHROPIC, reason="anthropic IS installed here")
def test_make_client_import_error_when_anthropic_absent() -> None:
    """With a key set but anthropic absent, the lazy import raises ImportError."""
    cfg = load_config({"PKM_VAULT": "/x", "ANTHROPIC_API_KEY": FAKE_TOKEN})
    with pytest.raises(ImportError):
        make_client(cfg)


@pytest.mark.skipif(not HAVE_ANTHROPIC, reason="anthropic not installed")
def test_make_client_builds_real_client_when_available() -> None:
    """When anthropic IS installed, make_client returns a usable client object."""
    cfg = load_config({"PKM_VAULT": "/x", "ANTHROPIC_API_KEY": FAKE_TOKEN})
    client = make_client(cfg)
    assert hasattr(client, "messages")
