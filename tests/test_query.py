"""Tests for :mod:`thoth.query` -- cost-ordered, vault-only retrieval.

These tests build a real seeded vault under ``tmp_path`` (a static ``index.md`` plus a
handful of curated pages carrying frontmatter -- including the one-line ``summary:``
gloss (#72) -- and ``[[wikilinks]]``) and a real :class:`~thoth.vault.Vault` over it.
The semantic-recall seam is a
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
    METHOD_GREP,
    METHOD_RECALL,
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

# 🏠 PKM Vault — Home

![[_bases/home.base#Recent Captures (7d)]]
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


def _page(
    *,
    title: str,
    page_type: str,
    body: str,
    tags: str = "[controls]",
    summary: str | None = None,
) -> str:
    """Render a minimal valid page (frontmatter + body) as markdown text."""
    summary_line = f"summary: {summary}\n" if summary is not None else ""
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        "created: 2026-05-30\n"
        "updated: 2026-05-30\n"
        "source: slack\n"
        f"tags: {tags}\n"
        f"{summary_line}"
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
    (root / "notes" / "distributed-systems.md").write_text(
        _page(
            title="Distributed Systems",
            page_type="note",
            body=(
                "# Distributed Systems\n\n"
                "Notes on the CAP theorem and consensus. The acronym CAP stands for\n"
                "consistency, availability, partition-tolerance.\n"
            ),
            tags="[distributed]",
        ),
        encoding="utf-8",
    )
    (root / "entities" / "jane-doe.md").write_text(
        _page(
            title="Jane Doe",
            page_type="entity",
            body="# Jane Doe\n\nCollaborator on the controls work.\n",
            tags="[person]",
            # A distinctive word ("kinematics") that appears ONLY in the summary, so a
            # grep hit on it proves grep scans the frontmatter gloss (#72 / ADR 0008).
            summary="lead on the kinematics calibration effort",
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

    def recall(
        self, query: str, *, limit: int = 10, types: frozenset[str] | None = None
    ) -> list[RecallHit]:
        """Record the call and return the canned hits (type-scoped, truncated)."""
        self.recall_calls.append((query, limit))
        hits = self.hits
        if types is not None:
            hits = [hit for hit in hits if hit.page_type in types]
        return hits[:limit]


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


def test_searched_dirs_span_reference_and_actions() -> None:
    """The lexical scan targets the reference folders plus actions/ (issue #106)."""
    assert SEARCHED_DIRS == (
        "entities",
        "notes",
        "memories",
        "actions",
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
    citation = engine.build_citation("notes/distributed-systems.md")
    assert citation.obsidian_uri.startswith("obsidian://open?vault=pkm-vault&file=")
    assert "notes%2Fdistributed-systems.md" in citation.obsidian_uri
    # The scheme-independent handles are always present.
    assert citation.path == "notes/distributed-systems.md"
    assert citation.wikilink == "[[distributed-systems]]"


# --- summary gloss via grep (#72 / ADR 0008) ---------------------------------------


def test_grep_matches_frontmatter_summary(engine: QueryEngine) -> None:
    """grep finds a page by a word that lives ONLY in its ``summary:`` frontmatter.

    Replaces the old ``index.md`` catalog pass: the per-page gloss now rides in
    frontmatter, and the existing grep (which scans the whole file) absorbs it (#72).
    """
    # "kinematics" appears only in jane-doe's summary frontmatter, never in any body.
    assert engine.grep("kinematics") == ["entities/jane-doe.md"]


def test_build_citation_surfaces_summary_snippet(engine: QueryEngine) -> None:
    """``build_citation`` exposes the page's ``summary:`` gloss as its ``snippet``."""
    citation = engine.build_citation("entities/jane-doe.md")
    assert citation.snippet == "lead on the kinematics calibration effort"


def test_build_citation_snippet_empty_without_summary(engine: QueryEngine) -> None:
    """A page with no ``summary:`` frontmatter yields an empty citation snippet."""
    citation = engine.build_citation("entities/drive-control-module.md")
    assert citation.snippet == ""


# --- grep --------------------------------------------------------------------------


def test_grep_matches_body_text(engine: QueryEngine) -> None:
    """A term in a page body is found and returned as a vault-relative path."""
    hits = engine.grep("consensus")
    assert "notes/distributed-systems.md" in hits


def test_grep_matches_filename(engine: QueryEngine) -> None:
    """A term matching the filename is found (here also linked from another body)."""
    hits = engine.grep("drive-control-module")
    assert "entities/drive-control-module.md" in hits
    # Folder order puts entities first; the filename match leads.
    assert hits[0] == "entities/drive-control-module.md"


def test_grep_is_case_insensitive(engine: QueryEngine) -> None:
    """Matching ignores case on both filename and body."""
    assert "notes/distributed-systems.md" in engine.grep("CAP")
    assert "notes/distributed-systems.md" in engine.grep("cap")


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
    """A matching page in raw/ (still excluded) is not returned by grep."""
    (vault.root / "raw" / "articles" / "leaked.md").write_text(
        _page(
            title="Leaked",
            page_type="note",
            body="# Leaked\n\nunique-raw-token here.\n",
        ),
        encoding="utf-8",
    )
    assert engine.grep("unique-raw-token") == []


# --- grep ranking (issue #96) ------------------------------------------------------


def _ranking_engine(config: Config, vault: Vault, **pages: str) -> QueryEngine:
    """Write ``folder/slug.md`` pages (value = body) into the vault; return an engine.

    Keys are ``folder__slug`` (double underscore = the ``/`` separator) so a test can
    drop a handful of bodies into ``entities``/``notes``/``memories`` and immediately
    grep them. Each page is given a plain title equal to its slug so the title carries
    no extra query tokens unless the test puts them there.
    """
    for key, body in pages.items():
        folder, _, slug = key.partition("__")
        (vault.root / folder / f"{slug}.md").write_text(
            _page(title=slug, page_type="note", body=body),
            encoding="utf-8",
        )
    return QueryEngine(config, vault, _FakeHindsight(config))


def test_grep_ranks_most_tokens_first_across_folders(
    config: Config, vault: Vault
) -> None:
    """The page matching the MOST tokens leads -- even from memories/ (scanned last).

    Reproduces issue #96: a natural-language query whose best page lives in the
    last-scanned folder must not be buried below single-token junk from earlier folders.
    """
    engine = _ranking_engine(
        config,
        vault,
        entities__a_dog="A dog ran past.",
        notes__b_bed="The bed was made.",
        memories__the_dog_bed=(
            "My black curly dog sleeps on a gingham bed every night."
        ),
    )
    hits = engine.grep("black curly dog gingham bed")
    # The memories page matches all five tokens; it leads despite the folder order.
    assert hits[0] == "memories/the_dog_bed.md"


def test_grep_more_tokens_outranks_fewer_regardless_of_folder(
    config: Config, vault: Vault
) -> None:
    """A page matching more tokens sorts above one matching fewer, independent of order.

    The richer page is seeded in ``memories`` (scanned LAST) and the weaker ones in
    ``entities``/``notes`` (scanned first), so folder order alone would invert the
    desired ranking; the token-count score must win.
    """
    engine = _ranking_engine(
        config,
        vault,
        entities__one_token="alpha only here.",
        notes__one_other="beta only here.",
        memories__three_tokens="alpha beta gamma all present.",
    )
    hits = engine.grep("alpha beta gamma")
    assert hits[0] == "memories/three_tokens.md"
    # Both single-token pages still appear, ranked below the three-token page.
    assert hits.index("memories/three_tokens.md") < hits.index("entities/one_token.md")
    assert hits.index("memories/three_tokens.md") < hits.index("notes/one_other.md")


def test_grep_word_boundary_excludes_substring_noise(
    config: Config, vault: Vault
) -> None:
    """``"bed"`` skips ``embedded``; ``"do"`` skips ``window``/``document`` (#96).

    The old substring scan flooded results with these false hits and suppressed the
    recall fallback; word-boundary matching drops them.
    """
    engine = _ranking_engine(
        config,
        vault,
        notes__embed_noise="This page is all about embedded firmware.",
        memories__real_bed="The bed in the spare room is comfy.",
    )
    # "bed" matches only the real page, never the "embedded" substring page.
    assert engine.grep("bed") == ["memories/real_bed.md"]

    engine2 = _ranking_engine(
        config,
        vault,
        notes__doc_noise="Open the document in a new window of the browser.",
        memories__my_dog="Tell me about my dog: a friendly retriever.",
    )
    hits = engine2.grep("tell me about my dog")
    # "do" as a substring of document/window must NOT surface the noise page; only the
    # genuine "dog"/"tell"/"me"/"about"/"my" matches count.
    assert "notes/doc_noise.md" not in hits
    assert hits[0] == "memories/my_dog.md"


def test_grep_title_match_outranks_body_only_match(
    config: Config, vault: Vault
) -> None:
    """A page matching a token in its title/frontmatter outranks a body-only match.

    Same single-token count for both pages, so only the placement weight separates them:
    the page whose title carries the word must lead.
    """
    # Title page lives in memories (scanned LAST); body page in entities (first), so
    # folder order would put the body page first absent the title weighting. Neither
    # filename carries the query token, so only the title placement separates them.
    (vault.root / "entities" / "first-page.md").write_text(
        _page(
            title="First Page",
            page_type="note",
            body="We discussed kangaroo handling at length during the meeting.",
        ),
        encoding="utf-8",
    )
    (vault.root / "memories" / "second-page.md").write_text(
        _page(
            title="Kangaroo Facts",
            page_type="note",
            body="Some marsupial trivia lives here.",
        ),
        encoding="utf-8",
    )
    engine = QueryEngine(config, vault, _FakeHindsight(config))
    hits = engine.grep("kangaroo")
    assert hits[0] == "memories/second-page.md"
    assert "entities/first-page.md" in hits


def test_grep_summary_frontmatter_match_outranks_body_only(
    config: Config, vault: Vault
) -> None:
    """A token in the ``summary:`` gloss outweighs a body-only match (#72)."""
    # Neither filename nor the non-summary frontmatter carries "quokka": the body page
    # hits it only in prose, the summary page only in its summary: gloss.
    (vault.root / "entities" / "prose-page.md").write_text(
        _page(
            title="Prose Page",
            page_type="note",
            body="A passing reference to a quokka in the prose.",
        ),
        encoding="utf-8",
    )
    (vault.root / "memories" / "gloss-page.md").write_text(
        _page(
            title="Gloss Page",
            page_type="note",
            body="No marsupial words appear in this body at all.",
            summary="the definitive quokka reference",
        ),
        encoding="utf-8",
    )
    engine = QueryEngine(config, vault, _FakeHindsight(config))
    hits = engine.grep("quokka")
    assert hits[0] == "memories/gloss-page.md"


# --- follow_wikilinks --------------------------------------------------------------


def test_follow_wikilinks_resolves_existing_targets(engine: QueryEngine) -> None:
    """[[slug]] links in a body resolve to existing pages in the searched dirs."""
    resolved = engine.follow_wikilinks("entities/program-motion-controller.md")
    assert "entities/drive-control-module.md" in resolved
    assert "notes/distributed-systems.md" in resolved


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
        RecallHit(
            path="entities/program-motion-controller.md",
            text="SOURCE: ...",
            page_type="entity",
        ),
        RecallHit(
            path="entities/ghost.md",
            text="SOURCE: entities/ghost.md",
            page_type="entity",
        ),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    paths = engine.recall_paths("anything")
    assert paths == ["entities/program-motion-controller.md"]


def test_recall_paths_drops_paths_outside_vault(config: Config, vault: Vault) -> None:
    """A poisoned SOURCE path escaping the vault is dropped before it can be cited."""
    hits = [
        RecallHit(
            path="../../etc/passwd",
            text="SOURCE: ../../etc/passwd",
            page_type="entity",
        ),
        RecallHit(path="/etc/passwd", text="SOURCE: /etc/passwd", page_type="entity"),
        RecallHit(
            path="notes/distributed-systems.md",
            text="SOURCE: ok",
            page_type="note",
        ),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    assert engine.recall_paths("anything") == ["notes/distributed-systems.md"]


def test_recall_paths_dedupes_preserving_order(config: Config, vault: Vault) -> None:
    """Duplicate recall hits collapse to the first occurrence."""
    page = "notes/distributed-systems.md"
    hits = [
        RecallHit(path=page, text="a", page_type="note"),
        RecallHit(
            path="entities/drive-control-module.md", text="b", page_type="entity"
        ),
        RecallHit(path=page, text="c", page_type="note"),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    assert engine.recall_paths("x") == [page, "entities/drive-control-module.md"]


def test_recall_paths_scopes_to_reference_by_default(
    config: Config, vault: Vault
) -> None:
    """Recall is reference-scoped by default; actionable needs an explicit scope.

    The index holds every content page (ADR 0004), so knowledge Q&A filters to the
    reference types (entity/note/memory) to keep its precision, excluding the
    actionable ``action`` type (ADR 0005). A caller can widen the scope explicitly.
    """
    (vault.root / "actions" / "read-paper.md").write_text(
        _page(title="Read paper", page_type="action", body="todo", tags="[task]"),
        encoding="utf-8",
    )
    hits = [
        RecallHit(path="notes/distributed-systems.md", text="x", page_type="note"),
        RecallHit(path="actions/read-paper.md", text="y", page_type="action"),
    ]
    engine = QueryEngine(config, vault, _FakeHindsight(config, hits=hits))

    # Default: only the reference hit survives (the action is filtered out).
    assert engine.recall_paths("q") == ["notes/distributed-systems.md"]
    # Explicit actionable scope returns the action page.
    assert engine.recall_paths("q", types=frozenset({"action"})) == [
        "actions/read-paper.md"
    ]
    # No scope ("search everything") returns both, in order.
    assert engine.recall_paths("q", types=None) == [
        "notes/distributed-systems.md",
        "actions/read-paper.md",
    ]


def test_recall_paths_empty_limit(engine: QueryEngine) -> None:
    """A non-positive limit short-circuits to an empty list (no recall call)."""
    assert engine.recall_paths("x", limit=0) == []


# --- answer: end-to-end ------------------------------------------------------------


def test_answer_acronym_hits_grep_and_cites_real_page(engine: QueryEngine) -> None:
    """A known acronym is answered structurally; the cited path exists on disk."""
    result = engine.answer("CAP", max_pages=3)
    assert isinstance(result, QueryResult)
    cited = {c.path for c in result.citations}
    assert "notes/distributed-systems.md" in cited
    # Every cited path is a real, confined file under the vault root.
    for citation in result.citations:
        assert engine.build_citation(citation.path).path == citation.path


def test_answer_logs_success_line(
    engine: QueryEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """answer() emits one concise INFO line with consulted/cited counts + ms (#52)."""
    with caplog.at_level("INFO", logger="thoth.query"):
        engine.answer("program-motion-controller", max_pages=2)
    records = [r for r in caplog.records if "query answered:" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "consulted=" in msg
    assert "cited=" in msg
    assert "ms" in msg


def test_answer_full_grep_still_consults_recall_but_structural_leads(
    engine: QueryEngine, hindsight: _FakeHindsight
) -> None:
    """Recall ALWAYS votes (#143), but structural hits keep the lead on a score tie.

    Rewritten for the RRF blend (#143): recall is no longer gated on thin results, so
    it IS consulted even when grep + wikilinks already fill ``max_pages``. The fake here
    has no canned hits, so recall contributes nothing -- ``used_recall`` stays False and
    the structural order is preserved -- but the recall call must still have happened
    (concurrent, always-runs). The PMC query reaches PMC + its two existing wikilink
    targets, so the structural source alone fills max_pages=2.
    """
    result = engine.answer("program-motion-controller", max_pages=2)
    assert result.used_recall is False
    # Recall now always runs when use_recall is true (the thin-gate is gone, #143).
    assert hindsight.recall_calls
    # Empty recall ⇒ pure structural order: the grep #1 (PMC) still leads.
    assert result.provenance[0].path == "entities/program-motion-controller.md"
    assert result.provenance[0].methods == ("grep",)


def test_answer_use_recall_false_never_calls_hindsight(
    config: Config, vault: Vault
) -> None:
    """use_recall=False keeps the path structural-only: recall is never consulted."""
    hits = [RecallHit(path="notes/distributed-systems.md", text="x", page_type="note")]
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
    hits = [RecallHit(path="notes/distributed-systems.md", text="x", page_type="note")]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    result = engine.answer("zzzznolexicalmatch", max_pages=3)
    assert result.used_recall is True
    assert result.citations[0].path == "notes/distributed-systems.md"
    assert hindsight.recall_calls  # recall was consulted


def test_answer_thin_grep_tops_up_with_recall(config: Config, vault: Vault) -> None:
    """A thin grep result (< max_pages) is topped up by recall, grep keeping its rank.

    grep for ``kinematics`` hits exactly one page (jane-doe, summary-only match) with no
    wikilinks, so the cheap passes are thin (1 < max_pages=3) and recall (#107) fires to
    top up with a DIFFERENT page; the grep hit keeps its leading rank.
    """
    hits = [RecallHit(path="notes/distributed-systems.md", text="x", page_type="note")]
    hindsight = _FakeHindsight(config, hits=hits)
    # An LLM whose USED line names both candidates, so both ride into the citations and
    # we can assert grep keeps its leading rank ahead of the recall top-up.
    llm = LLM(config, client=_FakeClient("prose\nUSED: 1, 2"))  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, hindsight, llm)
    result = engine.answer("kinematics", max_pages=3)
    paths = [c.path for c in result.citations]
    assert "entities/jane-doe.md" in paths
    assert "notes/distributed-systems.md" in paths
    assert paths.index("entities/jane-doe.md") < paths.index(
        "notes/distributed-systems.md"
    )
    assert hindsight.recall_calls  # recall fired despite a non-empty grep result


def test_answer_full_grep_blends_recall_vote_structural_still_leads(
    config: Config, vault: Vault
) -> None:
    """Even when grep fills max_pages, recall votes via RRF but a tie keeps grep #1.

    Rewritten for the blend (#143): the thin-gate is gone, so recall is consulted (and
    its hit votes in the RRF fusion) even at max_pages=1. With the grep #1 (PMC) and the
    recall hit each at source rank 0, the fused scores tie and the structural tie-break
    keeps the grep page as the single cited page; the recall hit is crowded out only by
    the page budget, not by being ignored.
    """
    hits = [RecallHit(path="notes/distributed-systems.md", text="x", page_type="note")]
    hindsight = _FakeHindsight(config, hits=hits)
    # An LLM whose USED line is absent ⇒ all (one) cited pages kept; we read provenance.
    llm = LLM(config, client=_FakeClient("prose"))  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, hindsight, llm)
    result = engine.answer("program-motion-controller", max_pages=1)
    # Recall ran (no thin-gate), but grep #1 wins the score tie and is the sole cite.
    assert hindsight.recall_calls
    assert [p.path for p in result.provenance] == [
        "entities/program-motion-controller.md"
    ]
    assert result.provenance[0].methods == ("grep",)


# --- the RRF blend: recall always votes (issue #143) -------------------------------


def test_answer_recall_only_hit_gets_a_slot_despite_full_grep(
    config: Config, vault: Vault
) -> None:
    """Criterion A: a recall rank-0 hit is cited even when grep filled max_pages (#143).

    grep for ``the`` matches several reference pages (filling max_pages with structural
    hits), and a DISTINCT recall-only page (recall rank 0) is injected. Under the old
    thin-gate that page would be crowded out; under RRF its ``1/RRF_K`` score earns it a
    cited slot. The LLM's USED line names every candidate so all ride into citations.
    """
    recall_only = "memories/semantic-only.md"
    (vault.root / "memories" / "semantic-only.md").write_text(
        # No ``the`` token, so grep never finds it -- it can ONLY arrive via recall.
        _page(title="Semantic Only", page_type="memory", body="Zylqx marker prose."),
        encoding="utf-8",
    )
    hits = [RecallHit(path=recall_only, text="x", page_type="memory")]
    hindsight = _FakeHindsight(config, hits=hits)
    # USED names all candidates so the recall-only page survives into citations.
    used_line = "USED: " + ", ".join(str(i) for i in range(1, 9))
    llm = LLM(config, client=_FakeClient(f"prose\n{used_line}"))  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, hindsight, llm)
    result = engine.answer("the", max_pages=3)
    paths = {c.path for c in result.citations}
    assert recall_only in paths  # not crowded out by the full grep result
    assert result.used_recall is True
    prov = {p.path: p.methods for p in result.provenance}
    assert prov[recall_only] == (METHOD_RECALL,)


def test_answer_empty_recall_returns_grep_only_page(
    config: Config, vault: Vault
) -> None:
    """Criterion B: empty recall ⇒ a grep-only page (zero semantic units) still returns.

    The regression guard for the blend: a page that grep finds but recall (empty bank)
    never surfaces must still be cited, in pure structural order. The fake Hindsight has
    no canned hits, modelling a page with zero semantic units / an empty index.
    """
    (vault.root / "memories" / "lonely.md").write_text(
        _page(
            title="Lonely", page_type="memory", body="Contains the wibbleword token."
        ),
        encoding="utf-8",
    )
    hindsight = _FakeHindsight(config)  # empty bank: recall yields nothing
    engine = QueryEngine(config, vault, hindsight)
    result = engine.answer("wibbleword", max_pages=3)
    assert [c.path for c in result.citations] == ["memories/lonely.md"]
    assert hindsight.recall_calls  # recall ran, just returned nothing
    assert result.used_recall is False
    assert result.provenance[0].methods == (METHOD_GREP,)


def test_answer_provenance_records_methods_per_cited_page(
    config: Config, vault: Vault
) -> None:
    """Provenance tags each cited page grep-only / recall-only / both; ranks 1-based.

    jane-doe is a grep-only hit (summary match on ``kinematics``); recall-island is a
    recall-only hit; program-motion-controller is surfaced by BOTH (grep on its slug
    AND a recall hit), so it must carry both methods.
    """
    both = "entities/program-motion-controller.md"
    recall_only = "memories/recall-island.md"
    # A page no structural pass reaches (no query token, not wikilinked): recall-only.
    (vault.root / "memories" / "recall-island.md").write_text(
        _page(title="Recall Island", page_type="memory", body="Isolated qwzx body."),
        encoding="utf-8",
    )
    hits = [
        RecallHit(path=both, text="x", page_type="entity"),
        RecallHit(path=recall_only, text="y", page_type="memory"),
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    # grep "kinematics program-motion-controller" hits jane-doe (summary) + PMC (slug).
    used_line = "USED: 1, 2, 3, 4, 5"
    llm = LLM(config, client=_FakeClient(f"prose\n{used_line}"))  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, hindsight, llm)
    result = engine.answer(
        "kinematics",
        search_terms=["kinematics", "program-motion-controller"],
        max_pages=5,
    )
    prov = {p.path: p for p in result.provenance}
    assert prov["entities/jane-doe.md"].methods == (METHOD_GREP,)
    assert prov[recall_only].methods == (METHOD_RECALL,)
    assert prov[both].methods == (METHOD_GREP, METHOD_RECALL)
    # Ranks are 1-based and contiguous over the cited set.
    ranks = sorted(p.rank for p in result.provenance)
    assert ranks == list(range(1, len(result.provenance) + 1))


def test_answer_page_found_by_both_sources_floats_to_top(
    config: Config, vault: Vault
) -> None:
    """A page voted by BOTH grep and recall outscores a single-source page (#143)."""
    # drive-control-module: grep hit (its slug) AND recall hit ⇒ two RRF votes.
    both = "entities/drive-control-module.md"
    recall_only = "notes/distributed-systems.md"
    hits = [
        RecallHit(path=recall_only, text="x", page_type="note"),  # recall rank 0
        RecallHit(path=both, text="y", page_type="entity"),  # recall rank 1
    ]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    # grep finds only drive-control-module (rank 0); it also has a recall vote, so its
    # fused score (1/RRF_K + 1/(RRF_K+1)) beats the recall-only page (1/RRF_K).
    result = engine.answer("drive-control-module", max_pages=3)
    assert result.provenance[0].path == both
    assert result.provenance[0].methods == (METHOD_GREP, METHOD_RECALL)


def test_answer_debug_logs_per_page_methods_and_recall_wallclock(
    config: Config, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    """At DEBUG the blend breakdown (per-page methods + recall ms) is emitted (#143).

    At INFO only the existing one-liner appears; the DEBUG breakdown stays absent.
    """
    hits = [RecallHit(path="notes/distributed-systems.md", text="x", page_type="note")]
    engine = QueryEngine(config, vault, _FakeHindsight(config, hits=hits))

    # INFO level: only the unchanged one-liner, no blend breakdown.
    with caplog.at_level("INFO", logger="thoth.query"):
        engine.answer("program-motion-controller", max_pages=2)
    info_msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("query answered:") for m in info_msgs)
    assert not any("query blend:" in m for m in info_msgs)
    caplog.clear()

    # DEBUG level: the per-page method breakdown + the semantic-pass wall-clock appear.
    with caplog.at_level("DEBUG", logger="thoth.query"):
        engine.answer("program-motion-controller", max_pages=2)
    debug_msgs = [r.getMessage() for r in caplog.records]
    blend = [m for m in debug_msgs if "query blend:" in m]
    assert len(blend) == 1
    assert "semantic recall ran" in blend[0]
    assert "ms" in blend[0]
    # A per-page line names a cited path and the method(s) that surfaced it.
    assert "entities/program-motion-controller.md via grep" in blend[0]
    # The INFO one-liner is still emitted unchanged alongside the DEBUG breakdown.
    assert any(m.startswith("query answered:") for m in debug_msgs)


def test_answer_topup_recall_flag_only_when_used(config: Config, vault: Vault) -> None:
    """used_recall reflects whether a recall-only page is in the model's USED subset."""
    hits = [RecallHit(path="notes/distributed-systems.md", text="x", page_type="note")]
    # USED names only the grep page (index 1) -> recall consulted but not used.
    llm_grep = LLM(config, client=_FakeClient("prose\nUSED: 1"))  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config, hits=hits), llm_grep)
    result = engine.answer("kinematics", max_pages=3)
    assert result.used_recall is False
    # USED names the recall page (index 2) -> recall helped.
    llm_recall = LLM(config, client=_FakeClient("prose\nUSED: 2"))  # type: ignore[arg-type]
    engine2 = QueryEngine(config, vault, _FakeHindsight(config, hits=hits), llm_recall)
    result2 = engine2.answer("kinematics", max_pages=3)
    assert result2.used_recall is True


def test_answer_qa_recall_scope_includes_actions(config: Config, vault: Vault) -> None:
    """The knowledge-Q&A recall pass surfaces action-typed hits (issue #106)."""
    (vault.root / "actions" / "read-paper.md").write_text(
        _page(title="Read paper", page_type="action", body="todo", tags="[task]"),
        encoding="utf-8",
    )
    hits = [RecallHit(path="actions/read-paper.md", text="y", page_type="action")]
    hindsight = _FakeHindsight(config, hits=hits)
    engine = QueryEngine(config, vault, hindsight)
    # A query with no lexical hit falls through to recall; the action hit survives the
    # Q&A type scope (RECALL_QA_TYPES) instead of being filtered out.
    result = engine.answer("zzzznolexicalmatch", max_pages=3)
    assert "actions/read-paper.md" in {c.path for c in result.citations}


def test_grep_finds_action_pages(config: Config, vault: Vault) -> None:
    """Lexical grep now scans actions/, so a filed action page is reachable (#106)."""
    (vault.root / "actions" / "read-paper.md").write_text(
        _page(
            title="Read paper",
            page_type="action",
            body="Follow up on the zzqxaction marker.",
            tags="[task]",
        ),
        encoding="utf-8",
    )
    engine = QueryEngine(config, vault, _FakeHindsight(config))
    assert engine.grep("zzqxaction") == ["actions/read-paper.md"]


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


# --- answer: search_terms seed the grep (issue #102) -------------------------------


def test_answer_greps_search_terms_not_raw_query(config: Config, vault: Vault) -> None:
    """search_terms seed the grep, so the de-pluralised keyword finds the page (#102).

    The raw prose ("the docs about dogs") would miss ``dog`` under word-boundary grep,
    and the stop words match nothing useful; the gate's ``["dog"]`` keyword surfaces it.
    """
    engine = _ranking_engine(
        config,
        vault,
        memories__dog_page="My dog is a black curly Labradoodle.",
    )
    result = engine.answer(
        "list me the docs about dogs", search_terms=["dog"], max_pages=3
    )
    assert "memories/dog_page.md" in {c.path for c in result.citations}


def test_answer_composes_prose_from_raw_query_not_search_terms(
    config: Config, vault: Vault
) -> None:
    """The LLM prompt carries the original query; search_terms only drive the grep."""
    client = _FakeClient("A dog answer.\nUSED: 1")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)
    (vault.root / "memories" / "dog-page.md").write_text(
        _page(title="Dog Page", page_type="note", body="My dog is a Labradoodle."),
        encoding="utf-8",
    )
    engine.answer("list me the docs about dogs", search_terms=["dog"], max_pages=2)
    # The composed-prose prompt is keyed off the natural-language query, not keywords.
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "list me the docs about dogs" in prompt
    assert "Question: dog" not in prompt


def test_answer_empty_search_terms_falls_back_to_query(
    config: Config, vault: Vault
) -> None:
    """An empty/None search_terms greps the raw query -- the pre-#102 behaviour."""
    engine = _ranking_engine(
        config,
        vault,
        notes__consensus_page="A page about consensus and quorums.",
    )
    # No keywords -> the raw query is grepped (and still finds the page).
    by_none = engine.answer("consensus", search_terms=None, max_pages=3)
    by_empty = engine.answer("consensus", search_terms=[], max_pages=3)
    assert "notes/consensus_page.md" in {c.path for c in by_none.citations}
    assert "notes/consensus_page.md" in {c.path for c in by_empty.citations}


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


# --- the USED: source-filter + Slack-clean prose prompt (issue #34) ----------------


def test_answer_keeps_only_the_used_subset(config: Config, vault: Vault) -> None:
    """A ``USED: 2`` line keeps only the 2nd consulted page; the line is not displayed.

    The PMC query consults [PMC, drive-control-module] at max_pages=2; the model says it
    used only page 2, so the result cites just drive-control-module and the displayed
    prose carries no ``USED:`` line. ``consulted_count`` still records the full set.
    """
    client = _FakeClient("Drive control runs the rail.\nUSED: 2")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    assert result.consulted_count == 2
    assert [c.path for c in result.citations] == ["entities/drive-control-module.md"]
    assert result.answer == "Drive control runs the rail."
    assert "USED:" not in result.answer


def test_answer_used_none_yields_no_citations(config: Config, vault: Vault) -> None:
    """``USED: none`` keeps no citations (the answer cited nothing)."""
    client = _FakeClient("I could not find anything relevant.\nUSED: none")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    assert result.citations == []
    assert result.consulted_count == 2
    assert "USED:" not in result.answer


def test_answer_missing_used_line_falls_back_to_all(
    config: Config, vault: Vault
) -> None:
    """A reply with no ``USED:`` line keeps ALL consulted citations (no regression)."""
    client = _FakeClient("A plain answer with no selection line.")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    assert len(result.citations) == 2  # all consulted pages kept
    assert result.consulted_count == 2


def test_answer_garbled_used_line_falls_back_to_all(
    config: Config, vault: Vault
) -> None:
    """A garbled ``USED:`` line (no parseable index) keeps all consulted citations."""
    client = _FakeClient("An answer.\nUSED: pages one and three")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    assert len(result.citations) == 2


def test_answer_uses_last_used_line_when_model_emits_two(
    config: Config, vault: Vault
) -> None:
    """Two ``USED:`` lines: the LAST wins and NEITHER leaks into the displayed prose.

    The design's prompt promises the selection is on the *final* line; if the model
    misbehaves and emits two, parsing the first would (a) cite the wrong subset and
    (b) leave the trailing ``USED: 2`` visible in the Slack answer. The trailing line
    must drive the selection and the prose must carry no ``USED:`` text at all.
    """
    client = _FakeClient("Drive control runs the rail.\nUSED: 1\nUSED: 2")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    # The trailing "USED: 2" wins -> the 2nd consulted page, not the 1st.
    assert [c.path for c in result.citations] == ["entities/drive-control-module.md"]
    assert result.answer == "Drive control runs the rail."
    assert "USED:" not in result.answer


def test_answer_keeps_leading_used_prose_line_picks_trailing_selection(
    config: Config, vault: Vault
) -> None:
    """A prose sentence starting ``USED:`` is preserved; the trailing line selects.

    A legitimate answer line that happens to begin with ``USED:`` must NOT be consumed
    as the selection (which would both garble the choice and leave the real trailing
    ``USED: 2`` visible). The last match drives selection; the leading prose survives.
    """
    client = _FakeClient("USED: the rail drive to position the stage.\nUSED: 2")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    result = engine.answer("program-motion-controller", max_pages=2)
    # Trailing "USED: 2" selects the 2nd page; the leading prose line is preserved.
    assert [c.path for c in result.citations] == ["entities/drive-control-module.md"]
    assert result.answer == "USED: the rail drive to position the stage."


def test_answer_feeds_full_body_and_prompt_enforces_clean_prose(
    config: Config, vault: Vault
) -> None:
    """Image embeds reach the model; clean Slack prose is the prompt's job (#34).

    Regression: the body used to be sanitised of ``![[image]]`` embeds before reaching
    the LLM, which blinded the model to attachments (it would answer "no images on
    file"). The full excerpt is now handed over verbatim -- so the model can answer
    questions *about* the image -- and the prompt instructs Slack-legible prose that
    refers to pages by title and pastes no raw markup. The vault page is never modified.
    """
    embed = "![[diagram.png]]"
    body = f"# Embed Page\n\nIntro prose.\n{embed}\nMore prose after the embed.\n"
    (vault.root / "notes" / "embed-page.md").write_text(
        _page(title="Embed Page", page_type="note", body=body),
        encoding="utf-8",
    )
    client = _FakeClient("Composed.\nUSED: 1")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    engine.answer("embed-page", max_pages=1)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    # The model SEES the embed (so it can answer about the image) and nearby prose.
    assert embed in prompt
    assert "More prose after the embed." in prompt
    # Clean output is the prompt's responsibility, not a pre-processor's: Slack mrkdwn
    # (issue #63), no narrated source list, embeds named as a thing NOT to paste.
    assert "Slack mrkdwn" in prompt
    assert "*bold*" in prompt  # Slack mrkdwn, not GitHub **bold** (issue #63)
    assert "do not mention or list the sources" in prompt  # no source-list aside (#63)
    assert "![[embeds]]" in prompt  # named as a thing NOT to paste
    # The vault page itself is untouched.
    assert embed in (vault.root / "notes" / "embed-page.md").read_text(encoding="utf-8")


def test_answer_labels_candidates_with_indices(config: Config, vault: Vault) -> None:
    """Each candidate page is labelled with a 1-based ``[n]`` index in the prompt."""
    client = _FakeClient("Composed.\nUSED: 1")
    llm = LLM(config, client=client)  # type: ignore[arg-type]
    engine = QueryEngine(config, vault, _FakeHindsight(config), llm)

    engine.answer("program-motion-controller", max_pages=2)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "[1] ## Program Motion Controller" in prompt
    assert "[2] ## Drive Control Module" in prompt
