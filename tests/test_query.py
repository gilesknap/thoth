"""Tests for :mod:`thoth.query` -- cost-ordered, vault-only retrieval.

These tests build a real seeded vault under ``tmp_path`` (an ``index.md`` with catalog
lines plus a handful of curated pages carrying frontmatter and ``[[wikilinks]]``) and a
real :class:`~thoth.vault.Vault` over it. The semantic-recall seam is a
:class:`_FakeHindsight` (subclassing :class:`~thoth.hindsight.Hindsight` so it is a
drop-in type but spawns no subprocess) returning canned hits, and the optional prose LLM
is a tiny injected fake exposing ``.messages.create``. No network, no subprocess, no
real Hindsight CLI.

The load-bearing property under test is that **citations are unfabricable**: every cited
path is run back through the vault's confinement and link encoder, so a path outside the
vault cannot be cited and an ``obsidian://`` link cannot be invented by a model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from thoth.config import Config, load_config
from thoth.hindsight import Hindsight, RecallHit
from thoth.llm import LLM
from thoth.query import (
    SEARCHED_DIRS,
    QueryEngine,
    QueryError,
    QueryResult,
)
from thoth.vault import PathConfinementError, Vault

# --- vault seeding -----------------------------------------------------------------

_INDEX_SEED = """\
---
title: Home
type: summary
updated: 2026-05-30
---

# Home

## Knowledge catalog
> One line per page: [[link]] - summary.

### Entities
- [[program-motion-controller]] - central coordinator in the motor-control stack.
- [[drive-control-module]] — hardware interface for the motor rail.

### Concepts
- [[distributed-systems]] - notes on CAP and consensus.

### Comparisons

### Queries

### People
- [[people/jane-doe]] - collaborator on home + controls work.
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
    "concepts",
    "comparisons",
    "queries",
    "actions",
    "media",
    "memories",
    "people",
    "inbox",
)


def _page(
    *,
    title: str,
    page_type: str,
    body: str,
    tags: str = "[controls]",
) -> str:
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
    """Write the folder skeleton, spine files, and a few curated pages."""
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
                "It talks to [[drive-control-module]] and [[motor-rail-api]].\n"
                "Background: [[distributed-systems]].\n"
            ),
        ),
        encoding="utf-8",
    )
    (root / "entities" / "drive-control-module.md").write_text(
        _page(
            title="Drive Control Module",
            page_type="entity",
            body=(
                "# Drive Control Module\n\n"
                "Hardware interface for the motor rail; the DCM drives the rail.\n"
            ),
        ),
        encoding="utf-8",
    )
    (root / "concepts" / "distributed-systems.md").write_text(
        _page(
            title="Distributed Systems",
            page_type="concept",
            body=(
                "# Distributed Systems\n\n"
                "Notes on the CAP theorem and consensus. The acronym CAP stands for\n"
                "consistency, availability, partition-tolerance.\n"
            ),
            tags="[distributed]",
        ),
        encoding="utf-8",
    )
    (root / "people" / "jane-doe.md").write_text(
        _page(
            title="Jane Doe",
            page_type="entity",
            body="# Jane Doe\n\nCollaborator on the controls work.\n",
            tags="[person]",
        ),
        encoding="utf-8",
    )


# --- fakes -------------------------------------------------------------------------


class _FakeHindsight(Hindsight):
    """A :class:`Hindsight` drop-in that returns canned hits and never spawns a process.

    Subclasses the real class so it satisfies the :class:`QueryEngine` constructor's
    type, but overrides :meth:`recall` to return a pre-seeded list and to count calls
    (so a test can assert recall was *not* consulted on the cheap path).
    """

    def __init__(self, config: Config, hits: list[RecallHit] | None = None) -> None:
        super().__init__(config)
        self.hits: list[RecallHit] = [] if hits is None else hits
        self.recall_calls: list[tuple[str, int]] = []

    def recall(self, query: str, *, limit: int = 10) -> list[RecallHit]:
        """Record the call and return the canned hits (truncated to ``limit``)."""
        self.recall_calls.append((query, limit))
        return self.hits[:limit]


class _FakeMessages:
    """Records ``create`` kwargs and returns a canned Anthropic-shaped response."""

    def __init__(self, text: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = {"content": [{"type": "text", "text": text}]}

    def create(self, **kwargs: Any) -> dict[str, Any]:
        """Record the kwargs and return the canned response."""
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    """Structural stand-in for the Anthropic SDK exposing ``.messages.create``."""

    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


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
def hindsight(config: Config) -> _FakeHindsight:
    """A fake Hindsight with no canned hits (override ``.hits`` per test)."""
    return _FakeHindsight(config)


@pytest.fixture
def engine(config: Config, vault: Vault, hindsight: _FakeHindsight) -> QueryEngine:
    """A QueryEngine over the real vault + fake Hindsight, no LLM (excerpt fallback)."""
    return QueryEngine(config, vault, hindsight)


# --- module constants --------------------------------------------------------------


def test_searched_dirs_are_curated_knowledge_folders() -> None:
    """The lexical scan targets the curated-knowledge folders plus people."""
    assert SEARCHED_DIRS == (
        "entities",
        "concepts",
        "comparisons",
        "queries",
        "people",
    )


# --- build_citation: the unfabricable step -----------------------------------------


def test_build_citation_matches_config_encoder_and_filename(
    config: Config, engine: QueryEngine
) -> None:
    """obsidian_uri equals config.obsidian_uri(path); wikilink is the real filename."""
    path = "entities/program-motion-controller.md"
    citation = engine.build_citation(path)

    assert citation.path == path
    assert citation.title == "Program Motion Controller"
    assert citation.obsidian_uri == config.obsidian_uri(path)
    assert citation.wikilink == "[[program-motion-controller]]"


def test_build_citation_falls_back_to_slug_when_no_title(
    vault: Vault, engine: QueryEngine
) -> None:
    """A page without a title frontmatter yields the slug as the citation title."""
    # Write a page whose frontmatter omits 'title' (read_page tolerates it).
    target = vault.root / "entities" / "no-title.md"
    target.write_text(
        "---\ntype: entity\nsource: slack\n---\n\n# body\n", encoding="utf-8"
    )
    citation = engine.build_citation("entities/no-title.md")
    assert citation.title == "no-title"
    assert citation.wikilink == "[[no-title]]"


def test_build_citation_rejects_path_outside_vault(engine: QueryEngine) -> None:
    """A path escaping the vault root cannot be cited (confinement raises)."""
    with pytest.raises(PathConfinementError):
        engine.build_citation("../etc/passwd")


def test_build_citation_rejects_absolute_path(engine: QueryEngine) -> None:
    """An absolute path is rejected by confinement before any citation is built."""
    with pytest.raises(PathConfinementError):
        engine.build_citation("/etc/passwd")


def test_citation_obsidian_uri_is_percent_encoded(engine: QueryEngine) -> None:
    """obsidian_uri uses %2F separators; the path + wikilink ride alongside."""
    citation = engine.build_citation("concepts/distributed-systems.md")
    assert citation.obsidian_uri.startswith("obsidian://open?vault=pkm-vault&file=")
    assert "concepts%2Fdistributed-systems.md" in citation.obsidian_uri
    # The scheme-independent handles are always present.
    assert citation.path == "concepts/distributed-systems.md"
    assert citation.wikilink == "[[distributed-systems]]"


# --- index_summaries ---------------------------------------------------------------


def test_index_summaries_parses_catalog_lines(engine: QueryEngine) -> None:
    """Catalog lines parse into {target: summary}, ignoring blank sections/headings."""
    summaries = engine.index_summaries()
    assert (
        summaries["program-motion-controller"]
        == "central coordinator in the motor-control stack."
    )
    assert summaries["distributed-systems"] == "notes on CAP and consensus."
    assert summaries["people/jane-doe"] == "collaborator on home + controls work."
    # Headings and the blockquote legend are not catalog lines.
    assert "Knowledge catalog" not in summaries
    assert "link" not in summaries


def test_index_summaries_handles_em_dash_separator(engine: QueryEngine) -> None:
    """A catalog line using an em dash separator parses just like a hyphen."""
    summaries = engine.index_summaries()
    assert summaries["drive-control-module"] == "hardware interface for the motor rail."


def test_index_summaries_empty_when_no_index(
    config: Config, vault: Vault, hindsight: _FakeHindsight
) -> None:
    """A vault with no index.md yields an empty map rather than raising."""
    (vault.root / "index.md").unlink()
    engine = QueryEngine(config, vault, hindsight)
    assert engine.index_summaries() == {}


# --- grep --------------------------------------------------------------------------


def test_grep_matches_body_text(engine: QueryEngine) -> None:
    """A term in a page body is found and returned as a vault-relative path."""
    hits = engine.grep("consensus")
    assert "concepts/distributed-systems.md" in hits


def test_grep_matches_filename(engine: QueryEngine) -> None:
    """A term matching the filename is found (here also linked from another body)."""
    hits = engine.grep("drive-control-module")
    assert "entities/drive-control-module.md" in hits
    # Folder order puts entities first; the filename match leads.
    assert hits[0] == "entities/drive-control-module.md"


def test_grep_is_case_insensitive(engine: QueryEngine) -> None:
    """Matching ignores case on both filename and body."""
    assert "concepts/distributed-systems.md" in engine.grep("CAP")
    assert "concepts/distributed-systems.md" in engine.grep("cap")


def test_grep_respects_limit(engine: QueryEngine) -> None:
    """The limit caps the number of returned paths."""
    # 'the' appears in several bodies; a limit of 1 returns exactly one path.
    hits = engine.grep("the", limit=1)
    assert len(hits) == 1


def test_grep_empty_term_returns_empty(engine: QueryEngine) -> None:
    """A blank term (or a non-positive limit) returns no hits, never scans."""
    assert engine.grep("") == []
    assert engine.grep("   ") == []
    assert engine.grep("cap", limit=0) == []


def test_grep_skips_unsearched_folders(vault: Vault, engine: QueryEngine) -> None:
    """A matching page in a non-searched folder (e.g. actions) is not returned."""
    (vault.root / "actions" / "fix-fence.md").write_text(
        _page(
            title="Fix fence",
            page_type="action",
            body="# Fix fence\n\nunique-action-token here.\n",
            tags="[task]",
        ),
        encoding="utf-8",
    )
    assert engine.grep("unique-action-token") == []


# --- follow_wikilinks --------------------------------------------------------------


def test_follow_wikilinks_resolves_existing_targets(engine: QueryEngine) -> None:
    """[[slug]] links in a body resolve to existing pages in the searched dirs."""
    resolved = engine.follow_wikilinks("entities/program-motion-controller.md")
    assert "entities/drive-control-module.md" in resolved
    assert "concepts/distributed-systems.md" in resolved


def test_follow_wikilinks_skips_dangling_links(engine: QueryEngine) -> None:
    """A [[motor-rail-api]] link with no page is skipped (not fabricated)."""
    resolved = engine.follow_wikilinks("entities/program-motion-controller.md")
    assert all("motor-rail-api" not in path for path in resolved)


def test_follow_wikilinks_missing_page_returns_empty(engine: QueryEngine) -> None:
    """Following links from a non-existent page returns an empty list, never raises."""
    assert engine.follow_wikilinks("entities/nope.md") == []


def test_follow_wikilinks_respects_limit(engine: QueryEngine) -> None:
    """The limit caps the resolved-link count."""
    resolved = engine.follow_wikilinks("entities/program-motion-controller.md", limit=1)
    assert len(resolved) == 1


# --- recall_paths ------------------------------------------------------------------


def test_recall_paths_keeps_only_real_pages(config: Config, vault: Vault) -> None:
    """Recall hits whose SOURCE path does not resolve to a real page are dropped."""
    hits = [
        RecallHit(path="entities/program-motion-controller.md", text="SOURCE: ..."),
        RecallHit(path="entities/ghost.md", text="SOURCE: entities/ghost.md"),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    paths = engine.recall_paths("anything")
    assert paths == ["entities/program-motion-controller.md"]


def test_recall_paths_drops_paths_outside_vault(config: Config, vault: Vault) -> None:
    """A poisoned SOURCE path escaping the vault is dropped before it can be cited."""
    hits = [
        RecallHit(path="../../etc/passwd", text="SOURCE: ../../etc/passwd"),
        RecallHit(path="/etc/passwd", text="SOURCE: /etc/passwd"),
        RecallHit(path="concepts/distributed-systems.md", text="SOURCE: ok"),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    assert engine.recall_paths("anything") == ["concepts/distributed-systems.md"]


def test_recall_paths_dedupes_preserving_order(config: Config, vault: Vault) -> None:
    """Duplicate recall hits collapse to the first occurrence."""
    page = "concepts/distributed-systems.md"
    hits = [
        RecallHit(path=page, text="a"),
        RecallHit(path="entities/drive-control-module.md", text="b"),
        RecallHit(path=page, text="c"),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    assert engine.recall_paths("x") == [page, "entities/drive-control-module.md"]


def test_recall_paths_empty_limit(engine: QueryEngine) -> None:
    """A non-positive limit short-circuits to an empty list (no recall call)."""
    assert engine.recall_paths("x", limit=0) == []


# --- answer: end-to-end ------------------------------------------------------------


def test_answer_acronym_hits_grep_and_cites_real_page(engine: QueryEngine) -> None:
    """A known acronym is answered structurally; the cited path exists on disk."""
    result = engine.answer("CAP", max_pages=3)
    assert isinstance(result, QueryResult)
    cited = {c.path for c in result.citations}
    assert "concepts/distributed-systems.md" in cited
    # Every cited path is a real, confined file under the vault root.
    for citation in result.citations:
        assert engine.build_citation(citation.path).path == citation.path


def test_answer_structural_path_does_not_use_recall(
    engine: QueryEngine, hindsight: _FakeHindsight
) -> None:
    """When grep/index already answer, used_recall is False and recall is not called."""
    result = engine.answer("program-motion-controller", max_pages=5)
    assert result.used_recall is False
    assert hindsight.recall_calls == []


def test_answer_use_recall_false_never_calls_hindsight(
    config: Config, vault: Vault
) -> None:
    """use_recall=False keeps the path structural-only: recall is never consulted."""
    hits = [RecallHit(path="concepts/distributed-systems.md", text="x")]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    # A query that grep cannot satisfy would normally fall through to recall.
    with pytest.raises(QueryError):
        engine.answer("zzzznomatch", max_pages=3, use_recall=False)
    assert hindsight.recall_calls == []


def test_answer_falls_back_to_recall_and_sets_flag(
    config: Config, vault: Vault
) -> None:
    """A phrasing-independent query with no lexical hit is answered via recall."""
    hits = [RecallHit(path="concepts/distributed-systems.md", text="x")]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    result = engine.answer("zzzznolexicalmatch", max_pages=3)
    assert result.used_recall is True
    assert result.citations[0].path == "concepts/distributed-systems.md"
    assert hindsight.recall_calls  # recall was consulted


def test_answer_caps_citations_at_max_pages(config: Config, vault: Vault) -> None:
    """No more than max_pages citations are attached even when more pages match."""
    hindsight = _FakeHindsight(config)
    engine = QueryEngine(config, vault, hindsight)
    # 'the' / 'motor' appear across several bodies; cap to 2.
    result = engine.answer("the", max_pages=2)
    assert len(result.citations) <= 2


def test_answer_no_match_raises(engine: QueryEngine) -> None:
    """A query that matches nothing anywhere raises QueryError."""
    with pytest.raises(QueryError):
        engine.answer("zzzzznothingmatchesthis")


def test_answer_rejects_bad_max_pages(engine: QueryEngine) -> None:
    """max_pages below 1 is rejected up front."""
    with pytest.raises(QueryError):
        engine.answer("cap", max_pages=0)


# --- answer: prose composition -----------------------------------------------------


def test_answer_excerpt_fallback_without_llm(engine: QueryEngine) -> None:
    """Without an LLM the answer is a deterministic excerpt of the top cited page."""
    result = engine.answer("program-motion-controller", max_pages=1)
    assert result.citations[0].path == "entities/program-motion-controller.md"
    assert "central coordinator" in result.answer
    assert result.used_recall is False


def test_answer_uses_injected_llm_for_prose(config: Config, vault: Vault) -> None:
    """With an injected LLM the prose is the model's reply; citations stay real."""
    client = _FakeClient("Composed answer about the motor stack.")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    hindsight = _FakeHindsight(config)
    engine = QueryEngine(config, vault, hindsight, llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    assert result.answer == "Composed answer about the motor stack."
    # The model was actually called, and citations are still real, confined paths.
    assert client.messages.calls
    assert result.citations
    for citation in result.citations:
        assert citation.obsidian_uri == config.obsidian_uri(citation.path)


def test_answer_deduplicates_citations(engine: QueryEngine) -> None:
    """A page reachable via several passes is cited only once."""
    # PMC is reachable via index + grep + wikilinks; it must appear exactly once.
    result = engine.answer("program-motion-controller", max_pages=5)
    paths = [c.path for c in result.citations]
    assert len(paths) == len(set(paths))
