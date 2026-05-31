"""Tests for :mod:`thoth.mcp_server` -- the FastMCP stdio server's pure tool bodies.

The seven ``pkm_*`` functions are exercised directly with a :class:`~thoth.mcp_server.
ToolContext` whose ``ingestor``/``query_engine``/``research`` are recording fakes (they
capture the delegated call and return a canned report/result) and whose ``vault`` is a
**real** :class:`~thoth.vault.Vault` over a ``tmp_path`` vault -- so ``pkm_write_page``,
``pkm_todos`` and ``pkm_recent`` hit real path confinement and real frontmatter parsing,
and ``pkm_todos``/``pkm_recent`` drive the real :class:`~thoth.summary.SummaryEngine`
scans. ``build_server`` is tested with a fake ``FastMCP`` injected into ``sys.modules``
(a recording double whose ``.tool()`` decorator captures the registered names and
callables), so no ``mcp`` package and no live stdio are needed. A test also asserts that
importing the module does not pull in ``mcp``.

No network, no subprocess, no real Slack/Anthropic/MCP runtime is touched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

from thoth.config import Config, load_config
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.mcp_server import (
    SERVER_NAME,
    TOOL_NAMES,
    ToolContext,
    ToolResult,
    build_server,
    pkm_ask,
    pkm_ingest,
    pkm_recent,
    pkm_save_answer,
    pkm_search,
    pkm_todos,
    pkm_write_page,
)
from thoth.query import Citation, QueryEngine, QueryError, QueryResult
from thoth.research import AskResult, ResearchEngine, ResearchError, WebCitation
from thoth.vault import Vault

# Obviously-fake placeholder only (gitleaks scans the commit).
FAKE_TOKEN = "x" * 8

# --- vault seeding -----------------------------------------------------------------

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

_INDEX_SEED = """\
---
title: Home
type: summary
created: 2026-05-30
updated: 2026-05-30
source: manual
tags: [home]
---

# Home

## Knowledge catalog

### Entities

### Concepts

### Comparisons

### Queries

### People
"""

_LOG_SEED = """\
# Vault Log

## [2026-05-30] create | Vault initialized
- structure seeded
"""


def _action_page(
    *,
    title: str,
    status: str,
    due_date: str | None = None,
    priority: str | None = None,
) -> str:
    """Render a minimal valid action page (frontmatter + body) as markdown text."""
    lines = [
        "---",
        f"title: {title}",
        "type: action",
        "created: 2026-05-30",
        "updated: 2026-05-30",
        "source: slack",
        "tags: [admin]",
        f"status: {status}",
    ]
    if due_date is not None:
        lines.append(f"due_date: {due_date}")
    if priority is not None:
        lines.append(f"priority: {priority}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _curated_page(
    *, title: str, page_type: str, updated: str, created: str | None = None
) -> str:
    """Render a minimal valid curated page with explicit created/updated dates."""
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        f"created: {created or updated}\n"
        f"updated: {updated}\n"
        "source: slack\n"
        "tags: [controls]\n"
        "---\n\n"
        f"# {title}\n"
    )


def _seed_vault(root: Path) -> None:
    """Write the folder skeleton and the spine files (index.md / log.md)."""
    for folder in _FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
    (root / "log.md").write_text(_LOG_SEED, encoding="utf-8")


# --- fakes -------------------------------------------------------------------------


class FakeIngestor:
    """Records ingest calls and returns a canned report (or raises a canned error)."""

    def __init__(
        self, report: IngestReport | None = None, error: Exception | None = None
    ) -> None:
        self.captures: list[Capture] = []
        self._report = report if report is not None else _report()
        self._error = error

    def ingest(self, capture: Capture) -> IngestReport:
        """Record the capture and return the canned report (or raise)."""
        self.captures.append(capture)
        if self._error is not None:
            raise self._error
        return self._report


class FakeQueryEngine:
    """Records query calls and returns a canned result (or raises a canned error)."""

    def __init__(
        self, result: QueryResult | None = None, error: Exception | None = None
    ) -> None:
        self.queries: list[tuple[str, int]] = []
        self._result = result if result is not None else _query_result()
        self._error = error

    def answer(
        self, query: str, *, max_pages: int = 5, use_recall: bool = True
    ) -> QueryResult:
        """Record the query and return the canned result (or raise)."""
        self.queries.append((query, max_pages))
        if self._error is not None:
            raise self._error
        return self._result

    def build_citation(self, path: str) -> Citation:
        """Build a canned citation for a path (used by pkm_save_answer)."""
        slug = path.rsplit("/", 1)[-1].removesuffix(".md")
        return _citation(path=path, title=slug, slug=slug)


class FakeResearch:
    """Records ask/save calls and returns canned results (or raises canned errors)."""

    def __init__(
        self,
        result: AskResult | None = None,
        error: Exception | None = None,
        *,
        saved_path: str = "queries/saved.md",
        save_error: Exception | None = None,
    ) -> None:
        self.asks: list[tuple[str, bool]] = []
        self.saves: list[tuple[str, AskResult, str | None]] = []
        self._result = result if result is not None else _ask_result()
        self._error = error
        self._saved_path = saved_path
        self._save_error = save_error

    def ask(
        self, question: str, *, force_web: bool = False, max_pages: int = 5
    ) -> AskResult:
        """Record the question and return the canned result (or raise)."""
        self.asks.append((question, force_web))
        if self._error is not None:
            raise self._error
        return self._result

    def save_answer(
        self,
        question: str,
        result: AskResult,
        *,
        slug: str | None = None,
        today: Any = None,
    ) -> str:
        """Record the save and return the canned path (or raise)."""
        self.saves.append((question, result, slug))
        if self._save_error is not None:
            raise self._save_error
        return self._saved_path


# --- canned builders ---------------------------------------------------------------


def _report(**overrides: Any) -> IngestReport:
    """Build an IngestReport with sensible filed-one-page defaults."""
    base: dict[str, Any] = {
        "page_paths": ["concepts/exa-search.md"],
        "raw_paths": ["raw/articles/exa-search.md"],
        "asset_paths": [],
        "obsidian_links": ["obsidian://open?vault=pkm-vault&file=concepts%2Fexa.md"],
        "wikilinks": ["[[exa-search]]"],
        "committed": True,
        "conflict": False,
        "message": "",
    }
    base.update(overrides)
    return IngestReport(**base)


def _citation(
    path: str = "concepts/exa-search.md",
    title: str = "Exa Search",
    slug: str = "exa-search",
) -> Citation:
    """Build a Citation with a realistic harness-built obsidian uri + wikilink."""
    uri = f"obsidian://open?vault=pkm-vault&file={path.replace('/', '%2F')}"
    return Citation(path=path, title=title, obsidian_uri=uri, wikilink=f"[[{slug}]]")


def _query_result(**overrides: Any) -> QueryResult:
    """Build a QueryResult with one citation by default."""
    base: dict[str, Any] = {
        "answer": "Exa is a semantic search engine.",
        "citations": [_citation()],
        "used_recall": False,
    }
    base.update(overrides)
    return QueryResult(**base)


def _ask_result(**overrides: Any) -> AskResult:
    """Build an AskResult with one vault citation and one web citation by default."""
    base: dict[str, Any] = {
        "answer": "Blended answer.",
        "vault_citations": [_citation()],
        "web_citations": [WebCitation(url="https://example.com/a", title="A")],
        "used_web": True,
        "saved_path": None,
    }
    base.update(overrides)
    return AskResult(**base)


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """A real Vault over a freshly seeded tmp_path vault."""
    _seed_vault(tmp_path)
    config = load_config({"PKM_VAULT": str(tmp_path)})
    return Vault(config)


@pytest.fixture
def config(vault: Vault) -> Config:
    """The frozen Config backing the tmp vault (shares the vault's root)."""
    return load_config({"PKM_VAULT": str(vault.root)})


def _context(
    config: Config,
    vault: Vault,
    *,
    ingestor: FakeIngestor | None = None,
    query_engine: FakeQueryEngine | None = None,
    research: FakeResearch | None = None,
) -> ToolContext:
    """Build a ToolContext wired to a real vault and (fake) collaborators."""
    return ToolContext(
        config=config,
        vault=vault,
        ingestor=cast(Ingestor, ingestor if ingestor is not None else FakeIngestor()),
        query_engine=cast(
            QueryEngine, query_engine if query_engine is not None else FakeQueryEngine()
        ),
        research=cast(
            ResearchEngine, research if research is not None else FakeResearch()
        ),
    )


# --------------------------------------------------------------------------------------
# import safety + lazy mcp
# --------------------------------------------------------------------------------------


def test_importing_module_does_not_import_mcp() -> None:
    """Importing thoth.mcp_server must not pull in the mcp package (lazy only)."""
    assert "thoth.mcp_server" in sys.modules
    assert "mcp" not in sys.modules


# --------------------------------------------------------------------------------------
# build_server (fake FastMCP injected via sys.modules)
# --------------------------------------------------------------------------------------


class _FakeFastMCP:
    """A recording stand-in for FastMCP: ``.tool()`` captures name + callable."""

    instances: list[_FakeFastMCP] = []

    def __init__(self, name: str) -> None:
        self.name = name
        self.registered: dict[str, Any] = {}
        self.ran_with: list[dict[str, Any]] = []
        _FakeFastMCP.instances.append(self)

    def tool(self, *, name: str) -> Any:
        """Return a decorator that records the registered callable under ``name``."""

        def decorator(func: Any) -> Any:
            self.registered[name] = func
            return func

        return decorator

    def run(self, **kwargs: Any) -> None:
        """Record a (never-reached in tests) stdio run call."""
        self.ran_with.append(kwargs)


@pytest.fixture
def fake_fastmcp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeFastMCP]:
    """Inject a fake ``mcp.server.fastmcp.FastMCP`` so build_server imports it.

    Builds throwaway ``mcp``, ``mcp.server`` and ``mcp.server.fastmcp`` modules carrying
    the recording :class:`_FakeFastMCP`, registers them in ``sys.modules`` for the
    duration of the test, and resets the recorder. ``monkeypatch`` removes them on
    teardown so the "module does not import mcp" invariant is not disturbed.
    """
    _FakeFastMCP.instances.clear()
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    server_mod.fastmcp = fastmcp_mod  # type: ignore[attr-defined]
    mcp_mod.server = server_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_mod)
    return _FakeFastMCP


def test_build_server_registers_exactly_tool_names(
    config: Config, vault: Vault, fake_fastmcp: type[_FakeFastMCP]
) -> None:
    """build_server registers exactly TOOL_NAMES on a FastMCP named SERVER_NAME."""
    server = build_server(_context(config, vault))
    assert isinstance(server, _FakeFastMCP)
    assert server.name == SERVER_NAME == "thoth"
    assert set(server.registered) == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == 7


def test_registered_tools_delegate_to_the_right_op(
    config: Config, vault: Vault, fake_fastmcp: type[_FakeFastMCP]
) -> None:
    """Each registered callable, when invoked, delegates to its matching context op."""
    ingestor = FakeIngestor()
    query_engine = FakeQueryEngine()
    research = FakeResearch()
    ctx = _context(
        config, vault, ingestor=ingestor, query_engine=query_engine, research=research
    )
    server = build_server(ctx)

    # pkm_search -> query_engine.answer
    out = server.registered["pkm_search"](query="hello", max_pages=3)
    assert isinstance(out, ToolResult)
    assert query_engine.queries == [("hello", 3)]

    # pkm_ask -> research.ask (force_web forwarded)
    server.registered["pkm_ask"](question="q", force_web=True)
    assert research.asks == [("q", True)]

    # pkm_save_answer -> research.save_answer
    server.registered["pkm_save_answer"](question="q", answer="an answer")
    assert len(research.saves) == 1
    assert research.saves[0][0] == "q"

    # pkm_ingest -> ingestor.ingest (Capture with source='mcp')
    server.registered["pkm_ingest"](text="a note")
    assert len(ingestor.captures) == 1
    assert ingestor.captures[0].source == "mcp"

    # pkm_write_page -> vault.write_page (real write under the tmp vault)
    res = server.registered["pkm_write_page"](
        folder="entities",
        slug="widget",
        frontmatter={
            "title": "Widget",
            "type": "entity",
            "source": "mcp",
            "tags": ["x"],
        },
        body="# Widget",
    )
    assert res.ok is True
    assert (vault.root / "entities" / "widget.md").is_file()


def test_build_server_invokes_todos_and_recent(
    config: Config, vault: Vault, fake_fastmcp: type[_FakeFastMCP]
) -> None:
    """The registered pkm_todos / pkm_recent callables run against the real vault."""
    server = build_server(_context(config, vault))
    assert server.registered["pkm_todos"](include_done=False).ok is True
    assert server.registered["pkm_recent"](days=7, limit=20).ok is True


# --------------------------------------------------------------------------------------
# pkm_ingest
# --------------------------------------------------------------------------------------


def test_pkm_ingest_text_builds_mcp_capture_and_renders_links(
    config: Config, vault: Vault
) -> None:
    """pkm_ingest(text=...) builds Capture(source='mcp') and renders the report."""
    ingestor = FakeIngestor()
    ctx = _context(config, vault, ingestor=ingestor)
    result = pkm_ingest(ctx, text="capture me")
    assert result.ok is True
    assert ingestor.captures[0].text == "capture me"
    assert ingestor.captures[0].source == "mcp"
    assert ingestor.captures[0].url is None
    assert ingestor.captures[0].path is None
    # The harness-built obsidian link + wikilink surface in the rendered text.
    assert "concepts/exa-search.md" in result.text
    assert "[[exa-search]]" in result.text
    assert result.data["page_paths"] == ["concepts/exa-search.md"]


def test_pkm_ingest_path_is_confined_and_resolved(config: Config, vault: Vault) -> None:
    """A vault-relative path is confined and passed as a resolved Path to ingest."""
    (vault.root / "raw" / "assets" / "a.png").write_bytes(b"img")
    ingestor = FakeIngestor()
    ctx = _context(config, vault, ingestor=ingestor)
    result = pkm_ingest(ctx, path="raw/assets/a.png")
    assert result.ok is True
    captured = ingestor.captures[0]
    assert isinstance(captured.path, Path)
    assert captured.path == vault.root / "raw" / "assets" / "a.png"


def test_pkm_ingest_rejects_escaping_path_before_calling_ingestor(
    config: Config, vault: Vault
) -> None:
    """An escaping path is rejected (ok=False) and the ingestor is never called."""
    ingestor = FakeIngestor()
    ctx = _context(config, vault, ingestor=ingestor)
    result = pkm_ingest(ctx, path="../../etc/passwd")
    assert result.ok is False
    assert "outside the vault" in result.text
    assert ingestor.captures == []


def test_pkm_ingest_surfaces_ingest_error_without_raising(
    config: Config, vault: Vault
) -> None:
    """An IngestError becomes ToolResult(ok=False), never raised into the runtime."""
    ingestor = FakeIngestor(error=IngestError("classify failed"))
    ctx = _context(config, vault, ingestor=ingestor)
    result = pkm_ingest(ctx, text="boom")
    assert result.ok is False
    assert "classify failed" in result.text


def test_pkm_ingest_reports_conflict_ok_false(config: Config, vault: Vault) -> None:
    """A conflicting report is surfaced ok=False with the conflict message."""
    report = _report(
        conflict=True,
        committed=False,
        message="rebase conflict on concepts/exa-search.md",
    )
    ctx = _context(config, vault, ingestor=FakeIngestor(report=report))
    result = pkm_ingest(ctx, text="x")
    assert result.ok is False
    assert "Vault conflict" in result.text
    assert "rebase conflict" in result.text
    assert result.data["conflict"] is True


def test_pkm_ingest_reports_deferred_ok_true(config: Config, vault: Vault) -> None:
    """A deferred report is surfaced ok=True as a partial success (raw saved)."""
    report = _report(
        page_paths=[],
        raw_paths=["inbox/hold-deadbeef0000.md"],
        committed=True,
        deferred=True,
        message="curation deferred -- LLM unavailable",
    )
    ctx = _context(config, vault, ingestor=FakeIngestor(report=report))
    result = pkm_ingest(ctx, text="x")
    # The raw was saved, so this is success (ok=True) even though curation was deferred.
    assert result.ok is True
    assert "inbox/hold-deadbeef0000.md" in result.text
    assert "deferred" in result.text.lower()
    assert result.data["deferred"] is True


def test_pkm_ingest_refuses_base64_blob(config: Config, vault: Vault) -> None:
    """A base64-blob-shaped text arg is refused before any ingest (closed surface)."""
    ingestor = FakeIngestor()
    ctx = _context(config, vault, ingestor=ingestor)
    blob = "A" * 400  # long, unbroken base64-alphabet run with no spaces
    result = pkm_ingest(ctx, text=blob)
    assert result.ok is False
    assert "base64" in result.text.lower() or "inline binary" in result.text.lower()
    assert ingestor.captures == []


def test_pkm_ingest_refuses_data_uri(config: Config, vault: Vault) -> None:
    """A data:...;base64, URI is refused (closed surface: never inline binary)."""
    ingestor = FakeIngestor()
    ctx = _context(config, vault, ingestor=ingestor)
    result = pkm_ingest(ctx, url="data:image/png;base64,iVBORw0KGgoAAAANS")
    assert result.ok is False
    assert ingestor.captures == []


def test_pkm_ingest_requires_one_input(config: Config, vault: Vault) -> None:
    """With no text/url/path, pkm_ingest returns ok=False and never calls ingest."""
    ingestor = FakeIngestor()
    ctx = _context(config, vault, ingestor=ingestor)
    result = pkm_ingest(ctx)
    assert result.ok is False
    assert ingestor.captures == []


# --------------------------------------------------------------------------------------
# pkm_search
# --------------------------------------------------------------------------------------


def test_pkm_search_renders_answer_and_citations(config: Config, vault: Vault) -> None:
    """pkm_search renders the answer + MCP-style citation (link, path, wikilink)."""
    query_engine = FakeQueryEngine()
    ctx = _context(config, vault, query_engine=query_engine)
    result = pkm_search(ctx, query="what is exa", max_pages=4)
    assert result.ok is True
    assert query_engine.queries == [("what is exa", 4)]
    assert "Exa is a semantic search engine." in result.text
    assert (
        "(obsidian://open?vault=pkm-vault&file=concepts%2Fexa-search.md)" in result.text
    )
    assert "`concepts/exa-search.md`" in result.text
    assert "[[exa-search]]" in result.text
    assert result.data["citations"] == ["concepts/exa-search.md"]


def test_pkm_search_query_error_is_ok_false(config: Config, vault: Vault) -> None:
    """A QueryError is surfaced as ToolResult(ok=False), never raised."""
    query_engine = FakeQueryEngine(error=QueryError("no match"))
    ctx = _context(config, vault, query_engine=query_engine)
    result = pkm_search(ctx, query="nothing here")
    assert result.ok is False
    assert "no match" in result.text


# --------------------------------------------------------------------------------------
# pkm_ask
# --------------------------------------------------------------------------------------


def test_pkm_ask_forwards_force_web_and_surfaces_web_sources(
    config: Config, vault: Vault
) -> None:
    """pkm_ask forwards force_web; used_web + web sources surface in text and data."""
    research = FakeResearch()
    ctx = _context(config, vault, research=research)
    result = pkm_ask(ctx, question="explain X", force_web=True)
    assert result.ok is True
    assert research.asks == [("explain X", True)]
    assert result.data["used_web"] is True
    assert result.data["web_citations"] == ["https://example.com/a"]
    assert "https://example.com/a" in result.text
    # The vault citation also renders.
    assert "[[exa-search]]" in result.text
    # The reply surfaces the offer-to-save affordance (SPEC section 7.1 step 4).
    assert "pkm_save_answer" in result.text


def test_pkm_ask_research_error_is_ok_false(config: Config, vault: Vault) -> None:
    """A ResearchError is surfaced as ToolResult(ok=False), never raised."""
    research = FakeResearch(error=ResearchError("empty answer"))
    ctx = _context(config, vault, research=research)
    result = pkm_ask(ctx, question="?")
    assert result.ok is False
    assert "empty answer" in result.text


# --------------------------------------------------------------------------------------
# pkm_save_answer (the offer-to-save write, SPEC section 7.1 step 4)
# --------------------------------------------------------------------------------------


def test_pkm_save_answer_delegates_and_returns_path(
    config: Config, vault: Vault
) -> None:
    """pkm_save_answer reconstructs an AskResult and delegates to save_answer."""
    research = FakeResearch(saved_path="queries/how-x-works.md")
    ctx = _context(config, vault, research=research)

    result = pkm_save_answer(
        ctx,
        question="how does X work",
        answer="X works like so.",
        web_sources=["https://example.com/x"],
        vault_paths=["concepts/exa-search.md"],
    )

    assert result.ok is True
    assert result.data["path"] == "queries/how-x-works.md"
    # The reconstructed result carried the supplied web + vault citations.
    assert len(research.saves) == 1
    saved_question, saved_result, saved_slug = research.saves[0]
    assert saved_question == "how does X work"
    assert saved_slug is None
    assert saved_result.answer == "X works like so."
    assert [w.url for w in saved_result.web_citations] == ["https://example.com/x"]
    assert [c.path for c in saved_result.vault_citations] == ["concepts/exa-search.md"]
    assert saved_result.used_web is True


def test_pkm_save_answer_empty_answer_is_ok_false(config: Config, vault: Vault) -> None:
    """An empty answer is refused before any vault write."""
    research = FakeResearch()
    ctx = _context(config, vault, research=research)
    result = pkm_save_answer(ctx, question="q", answer="   ")
    assert result.ok is False
    assert research.saves == []


def test_pkm_save_answer_vault_rejection_is_ok_false(
    config: Config, vault: Vault
) -> None:
    """A ResearchError from save_answer surfaces as ToolResult(ok=False)."""
    research = FakeResearch(save_error=ResearchError("invalid slug"))
    ctx = _context(config, vault, research=research)
    result = pkm_save_answer(ctx, question="q", answer="an answer", slug="Bad Slug")
    assert result.ok is False
    assert "invalid slug" in result.text


# --------------------------------------------------------------------------------------
# pkm_todos (real SummaryEngine over the tmp vault)
# --------------------------------------------------------------------------------------


def test_pkm_todos_lists_open_actions_with_status_due_priority(
    config: Config, vault: Vault
) -> None:
    """Open actions are returned with status/due/priority and [[wikilinks]]."""
    (vault.root / "actions" / "fix-fence.md").write_text(
        _action_page(
            title="Fix fence",
            status="todo",
            due_date="2026-06-10",
            priority="high",
        ),
        encoding="utf-8",
    )
    (vault.root / "actions" / "call-bank.md").write_text(
        _action_page(title="Call bank", status="in_progress"),
        encoding="utf-8",
    )
    ctx = _context(config, vault)
    result = pkm_todos(ctx)
    assert result.ok is True
    # SummaryEngine renders folder-qualified wikilinks ([[actions/<slug>]]).
    assert "[[actions/fix-fence]]" in result.text
    assert "[[actions/call-bank]]" in result.text
    assert "status: todo" in result.text
    assert "priority: high" in result.text
    assert "due: 2026-06-10" in result.text
    assert set(result.data["open"]) == {"actions/fix-fence.md", "actions/call-bank.md"}


def test_pkm_todos_excludes_done_unless_requested(config: Config, vault: Vault) -> None:
    """Done/cancelled actions are excluded by default and shown with include_done."""
    (vault.root / "actions" / "open-one.md").write_text(
        _action_page(title="Open one", status="todo"), encoding="utf-8"
    )
    (vault.root / "actions" / "done-one.md").write_text(
        _action_page(title="Done one", status="done"), encoding="utf-8"
    )
    (vault.root / "actions" / "cancelled-one.md").write_text(
        _action_page(title="Cancelled one", status="cancelled"), encoding="utf-8"
    )
    ctx = _context(config, vault)

    default = pkm_todos(ctx)
    assert "[[actions/open-one]]" in default.text
    assert "[[actions/done-one]]" not in default.text
    assert "[[actions/cancelled-one]]" not in default.text
    assert default.data["closed"] == []

    with_done = pkm_todos(ctx, include_done=True)
    assert "[[actions/open-one]]" in with_done.text
    assert "[[actions/done-one]]" in with_done.text
    assert "[[actions/cancelled-one]]" in with_done.text
    assert set(with_done.data["closed"]) == {
        "[[actions/done-one]]",
        "[[actions/cancelled-one]]",
    }


def test_pkm_todos_empty_vault_is_ok_with_note(config: Config, vault: Vault) -> None:
    """With no actions, pkm_todos returns ok=True with a 'no open actions' note."""
    ctx = _context(config, vault)
    result = pkm_todos(ctx)
    assert result.ok is True
    assert "No open actions" in result.text
    assert result.data["open"] == []


# --------------------------------------------------------------------------------------
# pkm_recent (real SummaryEngine over the tmp vault)
# --------------------------------------------------------------------------------------


def test_pkm_recent_lists_recent_pages_with_obsidian_links(
    config: Config, vault: Vault
) -> None:
    """Recent curated pages carry a harness-built obsidian:// link + path + wikilink."""
    import datetime as _dt

    today = _dt.date.today().isoformat()
    (vault.root / "notes" / "fresh.md").write_text(
        _curated_page(title="Fresh", page_type="note", updated=today),
        encoding="utf-8",
    )
    ctx = _context(config, vault)
    result = pkm_recent(ctx, days=7, limit=20)
    assert result.ok is True
    # pkm_recent renders PageRef.wikilink verbatim (SummaryEngine mints a bare-slug
    # wikilink for curated pages).
    assert "[[fresh]]" in result.text
    assert "`notes/fresh.md`" in result.text
    expected_uri = "obsidian://open?vault=pkm-vault&file=notes%2Ffresh.md"
    assert f"({expected_uri})" in result.text
    paths = [page["path"] for page in result.data["pages"]]
    assert "notes/fresh.md" in paths


def test_pkm_recent_respects_limit(config: Config, vault: Vault) -> None:
    """pkm_recent caps the listing at `limit` pages."""
    import datetime as _dt

    today = _dt.date.today().isoformat()
    for i in range(5):
        (vault.root / "notes" / f"page-{i}.md").write_text(
            _curated_page(title=f"Page {i}", page_type="note", updated=today),
            encoding="utf-8",
        )
    ctx = _context(config, vault)
    result = pkm_recent(ctx, days=7, limit=2)
    assert result.ok is True
    assert len(result.data["pages"]) == 2


# --------------------------------------------------------------------------------------
# pkm_write_page (real Vault write under the tmp vault)
# --------------------------------------------------------------------------------------


def test_pkm_write_page_writes_and_returns_path(config: Config, vault: Vault) -> None:
    """pkm_write_page delegates to Vault.write_page and returns the path + link."""
    ctx = _context(config, vault)
    result = pkm_write_page(
        ctx,
        folder="entities",
        slug="my-entity",
        frontmatter={
            "title": "My Entity",
            "type": "entity",
            "source": "mcp",
            "tags": ["x"],
        },
        body="# My Entity\n\nBody.",
    )
    assert result.ok is True
    assert result.data["path"] == "entities/my-entity.md"
    assert (vault.root / "entities" / "my-entity.md").is_file()
    # The reply carries the harness-built obsidian link + wikilink.
    assert "[[my-entity]]" in result.text
    expected_uri = "obsidian://open?vault=pkm-vault&file=entities%2Fmy-entity.md"
    assert f"({expected_uri})" in result.text
    # And the page round-trips through the vault reader.
    page = vault.read_page("entities/my-entity.md")
    assert page.frontmatter["title"] == "My Entity"


def test_pkm_write_page_rejects_bad_folder_type(config: Config, vault: Vault) -> None:
    """A folder/type mismatch yields ok=False (SchemaError caught); writes nothing."""
    ctx = _context(config, vault)
    result = pkm_write_page(
        ctx,
        folder="entities",
        slug="wrong",
        frontmatter={
            "title": "Wrong",
            "type": "note",  # note is not allowed in entities/
            "source": "mcp",
            "tags": ["x"],
        },
        body="# Wrong",
    )
    assert result.ok is False
    assert "Vault rejected" in result.text
    assert not (vault.root / "entities" / "wrong.md").exists()


def test_pkm_write_page_rejects_bad_slug(config: Config, vault: Vault) -> None:
    """An invalid slug yields ok=False (SlugError caught) and writes nothing."""
    ctx = _context(config, vault)
    result = pkm_write_page(
        ctx,
        folder="entities",
        slug="Not A Slug",
        frontmatter={
            "title": "X",
            "type": "entity",
            "source": "mcp",
            "tags": ["x"],
        },
        body="# X",
    )
    assert result.ok is False
    assert "Vault rejected" in result.text
    # No file with that (invalid) name leaked into the vault.
    assert list((vault.root / "entities").glob("*.md")) == []
