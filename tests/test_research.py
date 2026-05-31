"""Tests for :mod:`thoth.research` -- blended web + vault Q&A (``pkm_ask``).

These tests build a real seeded vault under ``tmp_path`` with a real
:class:`~thoth.vault.Vault` and a real :class:`~thoth.query.QueryEngine` (so
``build_citation`` confinement and ``write_page`` are exercised for real), and isolate
the two external boundaries:

* the model is a :class:`_FakeLLM` (a :class:`~thoth.llm.LLM` built on a
  :class:`_ScriptedClient`) returning canned Anthropic-shaped responses -- text blocks
  *and* ``tool_use`` blocks -- to drive the tool-use loop deterministically;
* the web is a :class:`_FakeExtractor` (a structural stand-in for
  :class:`~thoth.extract.Extractor`) returning canned :class:`~thoth.extract.WebHit` /
  :class:`~thoth.extract.ExtractedDoc`, or raising :class:`~thoth.extract.SsrfError` so
  the blocked-URL path is exercised without any DNS.

No network, no subprocess, no real Anthropic/Exa/Firecrawl. The load-bearing property
under test is that **vault citations are unfabricable**: a page the model "reads" that
does not resolve under the vault root yields no citation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from thoth.config import Config, load_config
from thoth.extract import ExtractedDoc, SsrfError, WebHit
from thoth.hindsight import Hindsight, RecallHit
from thoth.llm import LLM
from thoth.query import QueryEngine
from thoth.research import (
    RESEARCH_PREFIX,
    VAULT_READ_TOOL,
    WEB_EXTRACT_TOOL,
    WEB_SEARCH_TOOL,
    AskResult,
    ResearchEngine,
    ResearchError,
    WebCitation,
    _slugify,
    build_research_tools,
    force_web_requested,
    strip_research_prefix,
)
from thoth.vault import SLUG_RE, Vault

# --- vault seeding -----------------------------------------------------------------

_INDEX_SEED = """\
---
title: Home
type: summary
updated: 2026-05-30
---

# Home

## Knowledge catalog

### Entities
- [[program-motion-controller]] - central coordinator in the motor-control stack.

### Notes
- [[distributed-systems]] - notes on CAP and consensus.

### Memories
"""

_LOG_SEED = """\
# Vault Log

> Append-only.

## [2026-05-30] create | Vault initialized
- structure seeded
"""

_FOLDERS = (
    "raw/articles",
    "raw/papers",
    "raw/transcripts",
    "raw/assets",
    "entities",
    "notes",
    "memories",
    "actions",
    "inbox",
)


def _page(*, title: str, page_type: str, body: str, tags: str = "[controls]") -> str:
    """Render a minimal valid page (frontmatter + body) as markdown text."""
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        "created: 2026-05-30\n"
        "updated: 2026-05-30\n"
        "source: slack\n"
        f"tags: {tags}\n"
        "---\n\n"
        f"{body}\n"
    )


def _seed_vault(root: Path) -> None:
    """Write the folder skeleton, spine files, and two curated pages."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
    (root / "log.md").write_text(_LOG_SEED, encoding="utf-8")
    (root / "entities" / "program-motion-controller.md").write_text(
        _page(
            title="Program Motion Controller",
            page_type="entity",
            body=(
                "# Program Motion Controller\n\n"
                "The PMC is the central coordinator in the motor-control stack.\n"
                "It talks to [[distributed-systems]].\n"
            ),
        ),
        encoding="utf-8",
    )
    (root / "notes" / "distributed-systems.md").write_text(
        _page(
            title="Distributed Systems",
            page_type="note",
            body=("# Distributed Systems\n\nNotes on the CAP theorem and consensus.\n"),
            tags="[distributed]",
        ),
        encoding="utf-8",
    )


# --- fakes -------------------------------------------------------------------------


def _text_block(text: str) -> dict[str, Any]:
    """An Anthropic-shaped text content block."""
    return {"type": "text", "text": text}


def _tool_use_block(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """An Anthropic-shaped ``tool_use`` content block."""
    return {
        "type": "tool_use",
        "id": f"toolu_{name}",
        "name": name,
        "input": tool_input,
    }


def _text_response(text: str) -> dict[str, Any]:
    """A final (no-tool) response carrying a single text block."""
    return {"stop_reason": "end_turn", "content": [_text_block(text)]}


def _tool_response(*blocks: dict[str, Any]) -> dict[str, Any]:
    """A tool-use response carrying the given content blocks."""
    return {"stop_reason": "tool_use", "content": list(blocks)}


class _ScriptedMessages:
    """A fake ``client.messages`` returning the next scripted response per call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        """Record the kwargs and return the next scripted response."""
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class _ScriptedClient:
    """Structural Anthropic stand-in whose ``messages.create`` is scripted."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.messages = _ScriptedMessages(responses)


def _fake_llm(config: Config, responses: list[dict[str, Any]]) -> LLM:
    """Build an :class:`LLM` over a scripted client."""
    return LLM(config, client=_ScriptedClient(responses))  # type: ignore[arg-type]


class _FakeExtractor:
    """Structural stand-in for :class:`~thoth.extract.Extractor` (no network/DNS).

    Records every ``web_search``/``web_extract`` call and returns canned results, or
    raises a configured error (for the SSRF path). Matches the slice of the extractor
    surface :class:`ResearchEngine` uses.
    """

    def __init__(
        self,
        *,
        hits: list[WebHit] | None = None,
        docs: dict[str, ExtractedDoc] | None = None,
        extract_error: Exception | None = None,
    ) -> None:
        self._hits = hits if hits is not None else []
        self._docs = docs if docs is not None else {}
        self._extract_error = extract_error
        self.search_calls: list[str] = []
        self.extract_calls: list[str] = []

    def web_search(self, query: str, *, num_results: int = 5) -> list[WebHit]:
        """Record the query and return the canned hits."""
        self.search_calls.append(query)
        return list(self._hits)

    def web_extract(self, url: str) -> ExtractedDoc:
        """Record the URL and return a canned doc, or raise the configured error."""
        self.extract_calls.append(url)
        if self._extract_error is not None:
            raise self._extract_error
        if url in self._docs:
            return self._docs[url]
        return ExtractedDoc(source_url=url, title="Untitled", markdown="content")


class _FakeHindsight(Hindsight):
    """A :class:`Hindsight` drop-in that returns canned hits and spawns no process."""

    def __init__(self, config: Config, hits: list[RecallHit] | None = None) -> None:
        super().__init__(config)
        self._hits = hits if hits is not None else []

    def recall(
        self, query: str, *, limit: int = 10, types: frozenset[str] | None = None
    ) -> list[RecallHit]:
        """Return the canned hits (type-scoped, truncated to ``limit``)."""
        hits = self._hits
        if types is not None:
            hits = [hit for hit in hits if hit.page_type in types]
        return hits[:limit]


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A frozen Config whose vault path is a freshly seeded tmp vault."""
    root = tmp_path / "pkm-vault"
    root.mkdir()
    _seed_vault(root)
    return load_config({"PKM_VAULT": str(root)})


@pytest.fixture
def vault(config: Config) -> Vault:
    """A real Vault over the seeded tmp vault."""
    return Vault(config)


@pytest.fixture
def query_engine(config: Config, vault: Vault) -> QueryEngine:
    """A real QueryEngine over the vault + a fake Hindsight (no recall hits)."""
    return QueryEngine(config, vault, _FakeHindsight(config))


def _engine(
    config: Config,
    vault: Vault,
    query_engine: QueryEngine,
    extractor: _FakeExtractor,
    responses: list[dict[str, Any]],
    *,
    max_web_reads: int = 3,
) -> ResearchEngine:
    """Assemble a ResearchEngine with the given fake extractor + scripted LLM."""
    return ResearchEngine(
        config,
        vault,
        query_engine,
        extractor,  # type: ignore[arg-type]  # structural fake
        _fake_llm(config, responses),
        max_web_reads=max_web_reads,
    )


# --- force_web_requested / strip_research_prefix -----------------------------------


def test_force_web_requested_prefix_true() -> None:
    """A leading 'research:' marker (case-insensitive) requests the web."""
    assert force_web_requested("research: what is RAFT?") is True
    assert force_web_requested("RESEARCH: what is RAFT?") is True
    assert force_web_requested("  research:trimmed") is True


def test_force_web_requested_explicit_flag_true() -> None:
    """An explicit force_web=True requests the web regardless of text."""
    assert force_web_requested("what are my todos", force_web=True) is True


def test_force_web_requested_plain_personal_false() -> None:
    """A plain personal question with no marker stays vault-only."""
    assert force_web_requested("what are my todos") is False
    assert force_web_requested("a research paper on motors") is False  # not a prefix


def test_strip_research_prefix_removes_leading_marker() -> None:
    """A leading 'research:' marker (any case) is removed and the rest trimmed."""
    assert strip_research_prefix("research: what is RAFT?") == "what is RAFT?"
    assert strip_research_prefix("RESEARCH:   spaced  ") == "spaced"


def test_strip_research_prefix_leaves_plain_text() -> None:
    """Text without the marker is returned stripped but otherwise intact."""
    assert strip_research_prefix("  what are my todos  ") == "what are my todos"
    # The marker is only stripped when leading, not mid-string.
    assert strip_research_prefix("a research: note") == "a research: note"


def test_research_prefix_constant() -> None:
    """The exported prefix constant is the documented marker."""
    assert RESEARCH_PREFIX == "research:"


# --- build_research_tools ----------------------------------------------------------


def test_build_research_tools_vault_only_when_web_disallowed() -> None:
    """allow_web=False offers only the vault-read tool."""
    tools = build_research_tools(allow_web=False)
    assert tools == [VAULT_READ_TOOL]


def test_build_research_tools_includes_web_when_allowed() -> None:
    """allow_web=True appends web_search + web_extract after vault_read."""
    tools = build_research_tools(allow_web=True)
    names = [t["name"] for t in tools]
    assert names == ["vault_read", "web_search", "web_extract"]


def test_tool_schemas_have_valid_anthropic_shape() -> None:
    """Each tool dict carries name, description, and an object input_schema."""
    for tool in (VAULT_READ_TOOL, WEB_SEARCH_TOOL, WEB_EXTRACT_TOOL):
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str) and tool["description"]
        schema = tool["input_schema"]
        assert isinstance(schema, dict)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)


# --- ask(): vault-only path --------------------------------------------------------


def test_ask_vault_only_no_tool_use_never_touches_web(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A personal question answered with a text-only response uses no web at all."""
    extractor = _FakeExtractor()
    responses = [_text_response("Your PMC is the motor-control coordinator.")]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("what is the program motion controller")

    assert isinstance(result, AskResult)
    assert result.used_web is False
    assert result.web_citations == []
    # The extractor's web methods were never called.
    assert extractor.search_calls == []
    assert extractor.extract_calls == []
    assert result.answer == "Your PMC is the motor-control coordinator."


def test_ask_vault_only_offers_only_vault_read_tool(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """Without force/prefix the model is offered ONLY the vault-read tool."""
    extractor = _FakeExtractor()
    responses = [_text_response("answer")]
    engine = _engine(config, vault, query_engine, extractor, responses)

    engine.ask("what is the PMC")

    # Inspect the tools handed to the model on the first (only) call.
    first_call = _scripted(engine).calls[0]
    tool_names = [t["name"] for t in first_call["tools"]]
    assert tool_names == ["vault_read"]


def test_ask_vault_citations_are_harness_built_from_real_pages(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A page the model reads becomes an unfabricable citation (real obsidian uri)."""
    extractor = _FakeExtractor()
    path = "entities/program-motion-controller.md"
    responses = [
        _tool_response(_tool_use_block("vault_read", {"path": path})),
        _text_response("The PMC coordinates the motor stack."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("describe the program motion controller")

    assert [c.path for c in result.vault_citations] == [path]
    citation = result.vault_citations[0]
    assert citation.obsidian_uri == config.obsidian_uri(path)
    assert citation.wikilink == "[[program-motion-controller]]"
    assert result.used_web is False


# --- ask(): web path ---------------------------------------------------------------


def test_ask_web_path_dispatches_search_then_extract(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """force_web drives search -> extract -> final answer; web citations carry URLs."""
    url = "https://example.com/raft"
    extractor = _FakeExtractor(
        hits=[WebHit(url=url, title="Raft paper", snippet="consensus")],
        docs={url: ExtractedDoc(source_url=url, title="Raft paper", markdown="text")},
    )
    responses = [
        _tool_response(_tool_use_block("web_search", {"query": "raft consensus"})),
        _tool_response(_tool_use_block("web_extract", {"url": url})),
        _text_response("Raft is a consensus algorithm."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("explain raft", force_web=True)

    assert result.used_web is True
    assert extractor.search_calls == ["raft consensus"]
    assert extractor.extract_calls == [url]
    assert result.web_citations == [WebCitation(url=url, title="Raft paper")]
    assert result.answer == "Raft is a consensus algorithm."


def test_ask_tool_use_transcript_carries_native_blocks_keyed_by_id(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """The turn after a tool_use response is the API-required block exchange.

    This is the production contract the call-counting fake used to hide: after a
    ``stop_reason='tool_use'`` response, (1) the echoed assistant turn must carry the
    model's native ``tool_use`` block (with its ``id``), and (2) the following user turn
    must carry a ``tool_result`` block whose ``tool_use_id`` matches that id. A
    transcript that flattens this to text 400s against the real Messages API.
    """
    path = "entities/program-motion-controller.md"
    extractor = _FakeExtractor()
    responses = [
        _tool_response(_tool_use_block("vault_read", {"path": path})),
        _text_response("The PMC coordinates the motor stack."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    engine.ask("describe the program motion controller")

    # The SECOND create() call carries the full prior exchange in its messages.
    second_messages = _scripted(engine).calls[1]["messages"]
    # ... user prompt, assistant tool_use turn, user tool_result turn.
    assert len(second_messages) == 3

    assistant_turn = second_messages[1]
    assert assistant_turn["role"] == "assistant"
    tool_use_blocks = [
        b for b in assistant_turn["content"] if b.get("type") == "tool_use"
    ]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0]["id"] == "toolu_vault_read"
    assert tool_use_blocks[0]["name"] == "vault_read"

    user_turn = second_messages[2]
    assert user_turn["role"] == "user"
    result_blocks = user_turn["content"]
    assert isinstance(result_blocks, list)
    assert len(result_blocks) == 1
    assert result_blocks[0]["type"] == "tool_result"
    # The result is keyed to the SAME id the assistant's tool_use block carried.
    assert result_blocks[0]["tool_use_id"] == tool_use_blocks[0]["id"]
    assert result_blocks[0].get("is_error") is not True
    assert path in result_blocks[0]["content"]


def test_ask_web_path_caps_web_extract_dispatches(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """max_web_reads caps the number of web_extract dispatches to the extractor."""
    urls = [f"https://example.com/{i}" for i in range(4)]
    extractor = _FakeExtractor(
        docs={
            u: ExtractedDoc(source_url=u, title=f"t{i}", markdown="x")
            for i, u in enumerate(urls)
        }
    )
    # The model keeps asking to extract; the loop must stop dispatching past the cap.
    responses = [
        _tool_response(_tool_use_block("web_extract", {"url": urls[0]})),
        _tool_response(_tool_use_block("web_extract", {"url": urls[1]})),
        _tool_response(_tool_use_block("web_extract", {"url": urls[2]})),
        _tool_response(_tool_use_block("web_extract", {"url": urls[3]})),
        _text_response("done"),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses, max_web_reads=2)

    result = engine.ask("read everything", force_web=True)

    # Only two extractions actually reached the extractor.
    assert extractor.extract_calls == urls[:2]
    assert [c.url for c in result.web_citations] == urls[:2]
    assert result.used_web is True


def test_ask_research_prefix_forces_web_and_strips_marker(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A 'research:' prefixed question offers the web tools and strips the marker."""
    extractor = _FakeExtractor()
    responses = [_text_response("answer")]
    engine = _engine(config, vault, query_engine, extractor, responses)

    engine.ask("research: what is RAFT")

    first_call = _scripted(engine).calls[0]
    tool_names = [t["name"] for t in first_call["tools"]]
    assert "web_search" in tool_names and "web_extract" in tool_names
    # The marker is stripped from the prompt handed to the model.
    prompt = first_call["messages"][0]["content"]
    assert "research:" not in prompt.lower()
    assert "what is RAFT" in prompt


# --- ask(): unfabricable citations -------------------------------------------------


def test_ask_vault_read_of_nonexistent_page_yields_no_citation(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A model reading a non-existent vault page produces no citation for it."""
    extractor = _FakeExtractor()
    responses = [
        _tool_response(_tool_use_block("vault_read", {"path": "entities/ghost.md"})),
        _text_response("I could not find that page."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("describe the ghost page")

    assert result.vault_citations == []
    assert result.answer == "I could not find that page."


def test_ask_vault_read_escaping_path_yields_no_citation(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A model reading a path outside the vault yields no fabricated citation."""
    extractor = _FakeExtractor()
    responses = [
        _tool_response(_tool_use_block("vault_read", {"path": "../../etc/passwd"})),
        _text_response("That is not accessible."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("read the password file")

    assert result.vault_citations == []
    # The escaping path never reached a successful read; the answer still composes.
    assert result.answer == "That is not accessible."


# --- ask(): SSRF / extract failure tolerance ---------------------------------------


def test_ask_tolerates_ssrf_error_and_still_answers(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A blocked URL surfaces to the model as a tool_result error, never raised out."""
    extractor = _FakeExtractor(extract_error=SsrfError("blocked"))
    bad_url = "http://169.254.169.254/latest/meta-data"
    responses = [
        _tool_response(_tool_use_block("web_extract", {"url": bad_url})),
        _text_response("I could not read that URL, but here is what I know."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("research: read the metadata endpoint")

    # No SsrfError escaped; the answer composed; no web citation for the blocked URL.
    assert result.answer.startswith("I could not read that URL")
    assert result.web_citations == []
    assert result.used_web is True
    # The error was fed back as a tool_result block (is_error) on the follow-up turn,
    # keyed to the originating tool_use id.
    follow_up = _scripted(engine).calls[1]["messages"][-1]
    assert follow_up["role"] == "user"
    result_blocks = follow_up["content"]
    assert isinstance(result_blocks, list)
    assert result_blocks[0]["type"] == "tool_result"
    assert result_blocks[0]["tool_use_id"] == "toolu_web_extract"
    assert result_blocks[0]["is_error"] is True
    assert "blocked" in result_blocks[0]["content"]


# --- ask(): empty answer -----------------------------------------------------------


def test_ask_raises_on_blank_final_answer(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A blank/empty final assistant text raises ResearchError."""
    extractor = _FakeExtractor()
    responses = [_text_response("   ")]
    engine = _engine(config, vault, query_engine, extractor, responses)

    with pytest.raises(ResearchError):
        engine.ask("what is nothing")


# --- _slugify (python-slugify wrapper, issue #10) ----------------------------------


def test_slugify_transliterates_unicode() -> None:
    """python-slugify transliterates non-ASCII instead of stripping it (#10)."""
    assert _slugify("café notes") == "cafe-notes"
    assert _slugify("naïve Bayes") == "naive-bayes"


def test_slugify_empty_and_symbols_only_fall_back_to_query() -> None:
    """An empty / whitespace / symbols-only question falls back to the 'query' word."""
    assert _slugify("") == "query"
    assert _slugify("   ") == "query"
    assert _slugify("!!!") == "query"
    assert _slugify("…—🙂") == "query"


@pytest.mark.parametrize(
    "text",
    [
        "café notes",
        "naïve Bayes",
        "How does Raft relate to the PMC?",
        "a very long question about distributed consensus and raft and paxos and more",
        "日本語",
        "!!!",
        "",
        "Trailing---",
    ],
)
def test_slugify_always_satisfies_slug_re(text: str) -> None:
    """Every input yields a non-empty slug matching the vault SLUG_RE grammar (#10)."""
    slug = _slugify(text)
    assert slug
    assert SLUG_RE.fullmatch(slug), slug


def test_slugify_caps_word_count() -> None:
    """The slug keeps at most the project word cap (8 words)."""
    slug = _slugify("one two three four five six seven eight nine ten eleven twelve")
    assert len(slug.split("-")) <= 8
    assert SLUG_RE.fullmatch(slug)


def test_save_answer_unicode_question_slug(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """save_answer slugifies a unicode question by transliteration, not stripping (#10).

    The old hand-rolled slugifier dropped the accented bytes; python-slugify keeps the
    word, so the saved page lands on a meaningful ``notes/cafe-notes.md`` path.
    """
    extractor = _FakeExtractor()
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    result = AskResult(answer="Notes about a café.")

    rel = engine.save_answer("café notes", result, today=date(2026, 6, 1))

    assert rel == "notes/cafe-notes.md"
    assert vault.page_exists(rel)


# --- save_answer -------------------------------------------------------------------


def test_save_answer_writes_query_page_with_sources(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """save_answer writes notes/<slug>.md with type=note (tagged query), source=mcp."""
    extractor = _FakeExtractor()
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    url = "https://example.com/raft"
    citation = query_engine.build_citation("entities/program-motion-controller.md")
    result = AskResult(
        answer="Raft is a consensus algorithm used by the PMC.",
        vault_citations=[citation],
        web_citations=[WebCitation(url=url, title="Raft")],
        used_web=True,
    )

    rel = engine.save_answer(
        "how does raft relate to the PMC", result, today=date(2026, 6, 1)
    )

    assert rel == "notes/how-does-raft-relate-to-the-pmc.md"
    # The page round-trips via the vault.
    page = vault.read_page(rel)
    assert page.frontmatter["type"] == "note"
    assert page.frontmatter["tags"] == ["query"]
    assert page.frontmatter["source"] == "mcp"
    assert page.frontmatter["sources"] == [url]
    assert "## Sources" in page.body
    assert url in page.body
    assert "[[program-motion-controller]]" in page.body


def test_save_answer_explicit_slug(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """An explicit slug is honoured (validated by the vault)."""
    extractor = _FakeExtractor()
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    result = AskResult(answer="An answer.")

    rel = engine.save_answer("anything", result, slug="raft-notes")
    assert rel == "notes/raft-notes.md"
    assert vault.page_exists(rel)


def test_save_answer_rejects_invalid_slug(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A slug failing Vault.validate_slug raises ResearchError and writes nothing."""
    extractor = _FakeExtractor()
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    result = AskResult(answer="An answer.")

    with pytest.raises(ResearchError):
        engine.save_answer("anything", result, slug="Not A Slug")
    # Nothing new was written under notes/ (only the seeded page remains).
    assert [p.name for p in (vault.root / "notes").glob("*.md")] == [
        "distributed-systems.md"
    ]


def test_save_answer_cannot_escape_queries_folder(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A slug with path separators is rejected; nothing lands outside queries/."""
    extractor = _FakeExtractor()
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    result = AskResult(answer="An answer.")

    with pytest.raises(ResearchError):
        engine.save_answer("x", result, slug="../entities/evil")
    assert not (vault.root / "entities" / "evil.md").exists()


def test_save_answer_body_notes_when_no_external_sources(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A purely-personal answer with no sources still renders a Sources section."""
    extractor = _FakeExtractor()
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    result = AskResult(answer="Vault-only answer.")

    rel = engine.save_answer("a personal question", result)
    page = vault.read_page(rel)
    assert "## Sources" in page.body
    assert "no external sources" in page.body
    # No 'sources' frontmatter when there were no web URLs.
    assert "sources" not in page.frontmatter


# --- thin web pass-throughs --------------------------------------------------------


def test_web_search_passthrough(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """web_search delegates straight to the extractor."""
    hit = WebHit(url="https://e.com", title="t", snippet="s")
    extractor = _FakeExtractor(hits=[hit])
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    assert engine.web_search("query") == [hit]
    assert extractor.search_calls == ["query"]


def test_web_extract_passthrough(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """web_extract delegates straight to the extractor."""
    url = "https://e.com"
    doc = ExtractedDoc(source_url=url, title="t", markdown="m")
    extractor = _FakeExtractor(docs={url: doc})
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    assert engine.web_extract(url) == doc
    assert extractor.extract_calls == [url]


def test_web_extract_passthrough_propagates_ssrf(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """The thin pass-through surfaces SsrfError (the ask loop is what swallows it)."""
    extractor = _FakeExtractor(extract_error=SsrfError("blocked"))
    engine = _engine(config, vault, query_engine, extractor, [_text_response("x")])
    with pytest.raises(SsrfError):
        engine.web_extract("http://127.0.0.1")


# --- vault candidate pass ----------------------------------------------------------


def test_ask_surfaces_vault_candidates_in_prompt(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """The vault pass surfaces candidate page paths into the model's opening prompt."""
    extractor = _FakeExtractor()
    responses = [_text_response("answer")]
    engine = _engine(config, vault, query_engine, extractor, responses)

    engine.ask("distributed-systems")

    prompt = _scripted(engine).calls[0]["messages"][0]["content"]
    assert "notes/distributed-systems.md" in prompt


def test_ask_no_vault_match_still_answers_from_web(
    config: Config, vault: Vault, query_engine: QueryEngine
) -> None:
    """A question with no vault match is not fatal: the web path still answers."""
    url = "https://example.com/x"
    extractor = _FakeExtractor(
        docs={url: ExtractedDoc(source_url=url, title="X", markdown="m")}
    )
    responses = [
        _tool_response(_tool_use_block("web_extract", {"url": url})),
        _text_response("Here is a web-only answer."),
    ]
    engine = _engine(config, vault, query_engine, extractor, responses)

    result = engine.ask("research: zzzznovaultmatch topic", force_web=True)

    assert result.vault_citations == []
    assert [c.url for c in result.web_citations] == [url]
    assert result.answer == "Here is a web-only answer."


# --- helpers -----------------------------------------------------------------------


def _scripted(engine: ResearchEngine) -> _ScriptedMessages:
    """Reach the scripted messages fake behind an engine's injected LLM."""
    llm = engine._llm  # noqa: SLF001 - test reaches into the injected seam
    client = llm.client
    messages = client.messages
    assert isinstance(messages, _ScriptedMessages)
    return messages
