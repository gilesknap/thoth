"""Tests for :mod:`thoth.slack_app`.

These exercise the pure handler logic, the ``mrkdwn`` renderers, the allow-list parser
and the TTL dedupe with fakes only. ``slack_bolt`` is never imported (the module imports
it lazily, and a test asserts that importing the module does not pull it in). The
:class:`~thoth.ingest.Ingestor` and :class:`~thoth.query.QueryEngine` are replaced by
duck-typed fakes that record calls and return canned results, so no LLM, vault, git,
hindsight, or Slack socket is touched.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from thoth.budget import BudgetExceededError
from thoth.config import Config, load_config
from thoth.git_sync import Divergence, GitSync, VaultConflictError
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.intent import IntentClassifier, IntentDecision
from thoth.query import Citation, QueryEngine, QueryError, QueryResult
from thoth.research import AskResult, ResearchEngine, ResearchError, WebCitation
from thoth.slack_app import (
    DEDUPE_TTL_SECONDS,
    EventDedupe,
    Handlers,
    PendingSaves,
    Responder,
    build_app,
    parse_allowed_users,
    render_ask_result,
    render_citation,
    render_ingest_report,
    render_query_result,
    serve_with_alerting,
)
from thoth.state import EventStore

# Obviously-fake placeholder only (gitleaks scans the commit).
FAKE_TOKEN = "x" * 8

ALLOWED = "U_ALLOWED"
DENIED = "U_DENIED"


# --------------------------------------------------------------------------------------
# fixtures + fakes
# --------------------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    """A minimal frozen Config (no disk access needed for these tests)."""
    return load_config({"PKM_VAULT": "/x"})


def _report(**overrides: Any) -> IngestReport:
    """Build an IngestReport with sensible filed-one-page defaults."""
    base: dict[str, Any] = {
        "page_paths": ["concepts/exa-search.md"],
        "raw_paths": ["raw/articles/exa-search.md"],
        "asset_paths": [],
        "obsidian_links": ["obsidian://open?vault=pkm-vault&file=concepts%2Fexa.md"],
        "wikilinks": ["[[exa-search]]"],
        "titles": ["Exa Search"],
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


def _result(**overrides: Any) -> QueryResult:
    """Build a QueryResult with one citation by default."""
    base: dict[str, Any] = {
        "answer": "Exa is a semantic search engine.",
        "citations": [_citation()],
        "used_recall": False,
    }
    base.update(overrides)
    return QueryResult(**base)


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
        self.queries: list[str] = []
        self._result = result if result is not None else _result()
        self._error = error

    def answer(
        self, query: str, *, max_pages: int = 5, use_recall: bool = True
    ) -> QueryResult:
        """Record the query and return the canned result (or raise)."""
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._result


class FakeResearch:
    """Records ask/save calls and returns canned results (or raises canned errors)."""

    def __init__(
        self,
        result: AskResult | None = None,
        error: Exception | None = None,
        *,
        saved_path: str = "queries/saved-answer.md",
        save_error: Exception | None = None,
    ) -> None:
        self.asks: list[str] = []
        self.saves: list[tuple[str, AskResult]] = []
        self._result = result if result is not None else _ask_result()
        self._error = error
        self._saved_path = saved_path
        self._save_error = save_error

    def ask(
        self, question: str, *, force_web: bool = False, max_pages: int = 5
    ) -> AskResult:
        """Record the question and return the canned blended result (or raise)."""
        self.asks.append(question)
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
        self.saves.append((question, result))
        if self._save_error is not None:
            raise self._save_error
        return self._saved_path


def _ask_result(**overrides: Any) -> AskResult:
    """Build an AskResult with one vault citation and one web citation by default."""
    base: dict[str, Any] = {
        "answer": "Raft is a consensus algorithm.",
        "vault_citations": [_citation()],
        "web_citations": [WebCitation(url="https://example.com/raft", title="Raft")],
        "used_web": True,
    }
    base.update(overrides)
    return AskResult(**base)


class FakeIntentClassifier:
    """Records classify calls and returns a canned routing decision (issue #5)."""

    def __init__(self, intent: str = "ask", confidence: str = "high") -> None:
        self.classified: list[str] = []
        self._decision = IntentDecision(intent=intent, confidence=confidence)

    def classify(self, text: str) -> IntentDecision:
        """Record the text and return the canned decision."""
        self.classified.append(text)
        return self._decision


class Recorder:
    """A fake ``say`` callable that captures every reply string."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        """Record one reply."""
        self.messages.append(text)


class FakeSlackClient:
    """A fake Slack web client: files_info + download + post/update (no network).

    Records every ``chat.postMessage`` and ``chat.update`` so a test can assert the
    placeholder-then-edit processing-feedback flow (issue #34). ``chat_postMessage``
    returns a canned ``ts`` so the :class:`~thoth.slack_app.Responder` captures it and
    later edits that message via ``chat_update`` instead of posting a second reply.
    """

    def __init__(
        self,
        *,
        file_info: dict[str, Any] | None = None,
        payload: bytes = b"binary-bytes",
        post_ts: str | None = "1700000000.000100",
    ) -> None:
        self.files_info_calls: list[str] = []
        self.downloaded: list[str] = []
        self.posts: list[dict[str, str]] = []
        self.updates: list[dict[str, str]] = []
        self._file_info = file_info
        self._payload = payload
        self._post_ts = post_ts

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Record the placeholder post and return a canned ``ts`` (or none)."""
        self.posts.append({"channel": channel, "text": text})
        response: dict[str, Any] = {"ok": True}
        if self._post_ts is not None:
            response["ts"] = self._post_ts
        return response

    def chat_update(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, ts: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Record the in-place edit of a previously-posted placeholder."""
        self.updates.append({"channel": channel, "ts": ts, "text": text})
        return {"ok": True}

    def files_info(self, *, file: str) -> dict[str, Any]:  # noqa: N802 - SDK name
        """Return canned file metadata for a ``file_shared`` lookup."""
        self.files_info_calls.append(file)
        return {"ok": True, "file": self._file_info or {}}

    def download(self, url: str) -> bytes:
        """Record the URL and return canned bytes (stands in for an HTTP GET)."""
        self.downloaded.append(url)
        return self._payload


class FakeAlerter:
    """Records every alert routed through it (the errors-to-Slack seam, issue #15)."""

    def __init__(self) -> None:
        self.exceptions: list[tuple[str, BaseException]] = []
        self.divergences: list[tuple[int, datetime | None, str]] = []

    def alert_exception(self, where: str, exc: BaseException) -> bool:
        """Record an unhandled-exception alert."""
        self.exceptions.append((where, exc))
        return True

    def alert_unpushed_divergence(
        self, *, commits_ahead: int, since: datetime | None, detail: str = ""
    ) -> bool:
        """Record an unpushed-divergence alert."""
        self.divergences.append((commits_ahead, since, detail))
        return True


class FakeGitSync:
    """A duck-typed GitSync whose ``divergence`` returns a canned value (no git)."""

    def __init__(self, divergence: Divergence) -> None:
        self._divergence = divergence
        self.calls = 0

    def divergence(self, *, timeout: float = 30.0) -> Divergence:
        """Record the call and return the canned divergence."""
        self.calls += 1
        return self._divergence


def _handlers(
    config: Config,
    *,
    ingestor: FakeIngestor | None = None,
    query_engine: FakeQueryEngine | None = None,
    research: FakeResearch | None = None,
    intent_classifier: FakeIntentClassifier | None = None,
    allowed: frozenset[str] = frozenset({ALLOWED}),
    dedupe: EventDedupe | None = None,
    pending_saves: PendingSaves | None = None,
    alerter: FakeAlerter | None = None,
    git: FakeGitSync | None = None,
) -> tuple[Handlers, FakeIngestor, FakeQueryEngine]:
    """Construct Handlers wired to fakes, returning the fakes for assertions.

    When ``research`` is given the free-text path takes the blended ask; otherwise it is
    ``None`` and the free-text path stays vault-only (the existing behaviour). The
    research fake is reachable on the returned ``Handlers.research`` for assertions. An
    ``alerter`` + ``git`` enable the unpushed-divergence alert (issue #15).
    """
    ing = ingestor if ingestor is not None else FakeIngestor()
    qry = query_engine if query_engine is not None else FakeQueryEngine()
    kwargs: dict[str, Any] = {
        "config": config,
        "ingestor": cast(Ingestor, ing),
        "query_engine": cast(QueryEngine, qry),
        "allowed_users": allowed,
    }
    if research is not None:
        kwargs["research"] = cast(ResearchEngine, research)
    if intent_classifier is not None:
        kwargs["intent_classifier"] = cast(IntentClassifier, intent_classifier)
    if dedupe is not None:
        kwargs["dedupe"] = dedupe
    if pending_saves is not None:
        kwargs["pending_saves"] = pending_saves
    if alerter is not None:
        kwargs["alerter"] = alerter
    if git is not None:
        kwargs["git"] = cast(GitSync, git)
    return Handlers(**kwargs), ing, qry


# --------------------------------------------------------------------------------------
# parse_allowed_users
# --------------------------------------------------------------------------------------


def test_parse_allowed_users_none_is_empty() -> None:
    """None and a blank string both yield an empty (fail-closed) set."""
    assert parse_allowed_users(None) == frozenset()
    assert parse_allowed_users("") == frozenset()
    assert parse_allowed_users("   ") == frozenset()


def test_parse_allowed_users_comma_and_whitespace() -> None:
    """Comma- and whitespace-separated ids are both split."""
    assert parse_allowed_users("U1,U2 U3") == frozenset({"U1", "U2", "U3"})
    assert parse_allowed_users("U1, U2,  U3") == frozenset({"U1", "U2", "U3"})


def test_parse_allowed_users_strips_mention_wrappers() -> None:
    """A leading @ and <@U..|name> mention wrappers are trimmed to bare ids."""
    assert parse_allowed_users("@U1") == frozenset({"U1"})
    assert parse_allowed_users("<@U2>") == frozenset({"U2"})
    assert parse_allowed_users("<@U3|alice>") == frozenset({"U3"})
    assert parse_allowed_users("<@U1>, @U2  U3") == frozenset({"U1", "U2", "U3"})


# --------------------------------------------------------------------------------------
# is_allowed
# --------------------------------------------------------------------------------------


def test_is_allowed_only_for_listed_ids(config: Config) -> None:
    """is_allowed is True only for ids in the allow-list; empty id is False."""
    handlers, _, _ = _handlers(config)
    assert handlers.is_allowed(ALLOWED) is True
    assert handlers.is_allowed(DENIED) is False
    assert handlers.is_allowed("") is False


def test_denied_user_neither_ingests_nor_queries(config: Config) -> None:
    """A non-allowed sender triggers no ingest/query; a refusal is sent."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message({"user": DENIED, "text": "what are my todos"}, say)
    assert ing.captures == []
    assert qry.queries == []
    assert len(say.messages) == 1
    assert "not authorised" in say.messages[0].lower()


# --------------------------------------------------------------------------------------
# EventDedupe
# --------------------------------------------------------------------------------------


def test_dedupe_first_unseen_then_seen() -> None:
    """First seen() reports unseen, a second with the same id reports seen."""
    dedupe = EventDedupe(clock=lambda: 0.0)
    assert dedupe.seen("E1") is False
    assert dedupe.seen("E1") is True


def test_dedupe_empty_id_never_recorded() -> None:
    """An empty event id is always unseen and never recorded (cannot dedupe)."""
    dedupe = EventDedupe(clock=lambda: 0.0)
    assert dedupe.seen("") is False
    assert dedupe.seen("") is False


def test_dedupe_prune_drops_expired_with_injected_clock() -> None:
    """prune() drops entries older than the TTL using the injected clock."""
    now = {"t": 100.0}
    dedupe = EventDedupe(ttl_seconds=10.0, clock=lambda: now["t"])
    assert dedupe.seen("E1") is False
    now["t"] = 105.0  # within TTL
    assert dedupe.seen("E1") is True
    now["t"] = 200.0  # past TTL -> pruned -> unseen again
    assert dedupe.seen("E1") is False


def test_dedupe_default_ttl_constant() -> None:
    """The module pins the SPEC one-hour dedupe TTL."""
    assert DEDUPE_TTL_SECONDS == 3600.0


def test_dedupe_mark_records_without_seen() -> None:
    """mark() records an id so a later seen() reports it as already processed."""
    dedupe = EventDedupe(clock=lambda: 0.0)
    dedupe.mark("E9")
    assert dedupe.seen("E9") is True


# --------------------------------------------------------------------------------------
# EventDedupe durable backing (processed_events in state.db)
# --------------------------------------------------------------------------------------


def test_dedupe_recognises_event_after_simulated_restart(tmp_path: Any) -> None:
    """An event seen before a restart is dropped after restart via the durable store.

    Acceptance for the durable Slack dedupe (#18): the in-memory cache is lost on
    restart, but a *fresh* EventDedupe built over the *same* state.db recognises the
    earlier event as already-processed (the processed_events row survived).
    """
    db = tmp_path / "state.db"
    # First "process": records E1 both in the cache and the durable store.
    before = EventDedupe(clock=lambda: 0.0, store=EventStore(db, clock=lambda: 0.0))
    assert before.seen("E1") is False

    # Restart: a brand-new dedupe + store over the same file, empty in-memory cache.
    after = EventDedupe(clock=lambda: 1.0, store=EventStore(db, clock=lambda: 1.0))
    assert after.seen("E1") is True
    # A genuinely new id is still unseen after the restart.
    assert after.seen("E2") is False


def test_dedupe_store_prunes_past_ttl(tmp_path: Any) -> None:
    """A durably-recorded id past the TTL is pruned and recognised as unseen again."""
    db = tmp_path / "state.db"
    now = {"t": 100.0}
    store = EventStore(db, clock=lambda: now["t"])
    dedupe = EventDedupe(ttl_seconds=10.0, clock=lambda: now["t"], store=store)
    assert dedupe.seen("E1") is False
    now["t"] = 200.0  # past TTL for both the cache and the store
    # A fresh dedupe (no cache) over the same store: the row is pruned -> unseen.
    fresh = EventDedupe(ttl_seconds=10.0, clock=lambda: now["t"], store=store)
    assert fresh.seen("E1") is False
    store.close()


def test_dedupe_cache_hit_short_circuits_store(tmp_path: Any) -> None:
    """A repeat within one process is served from the cache (store still consistent)."""
    db = tmp_path / "state.db"
    store = EventStore(db, clock=lambda: 0.0)
    dedupe = EventDedupe(clock=lambda: 0.0, store=store)
    assert dedupe.seen("E1") is False
    assert dedupe.seen("E1") is True  # cache hit
    # The durable store agrees independently (a fresh dedupe sees it too).
    other = EventDedupe(clock=lambda: 0.0, store=store)
    assert other.seen("E1") is True
    store.close()


# --------------------------------------------------------------------------------------
# handle_message routing + dedupe
# --------------------------------------------------------------------------------------


def test_handle_message_routes_bare_url_to_ingest(config: Config) -> None:
    """A bare URL message is routed to ingestor.ingest(Capture(url=...))."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "https://example.com/article", "ts": "1.1"}, say
    )
    assert qry.queries == []
    assert len(ing.captures) == 1
    capture = ing.captures[0]
    assert capture.url == "https://example.com/article"
    assert capture.text is None
    assert capture.source == "slack"
    assert say.messages  # a confirmation was rendered


def test_handle_message_routes_free_text_to_query(config: Config) -> None:
    """Free text is routed to query_engine.answer and rendered."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is exa search?", "ts": "2.2"}, say
    )
    assert ing.captures == []
    assert qry.queries == ["what is exa search?"]
    assert "Exa is a semantic search engine." in say.messages[0]
    assert "obsidian://" in say.messages[0]
    assert "[[" not in say.messages[0]


def test_handle_message_capture_prefix_routes_to_text_ingest(config: Config) -> None:
    """A 'note:'/'capture:' prefix files free text as a Capture(text=...)."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "note: remember to call the dentist", "ts": "3.3"},
        say,
    )
    assert qry.queries == []
    assert len(ing.captures) == 1
    assert ing.captures[0].text == "remember to call the dentist"
    assert ing.captures[0].url is None


def test_handle_message_url_with_trailing_text_is_a_query(config: Config) -> None:
    """A URL embedded in a sentence is treated as a question, not a bare-URL capture."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is https://example.com about?", "ts": "4.4"},
        say,
    )
    assert ing.captures == []
    assert qry.queries == ["what is https://example.com about?"]


def test_handle_message_free_text_uses_research_when_wired(config: Config) -> None:
    """With a research engine, free text takes the blended ask + offer-to-save."""
    research = FakeResearch()
    handlers, _, qry = _handlers(config, research=research)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "explain raft", "channel": "D1", "ts": "9.1"}, say
    )
    # The blended path was used, NOT the vault-only query path.
    assert research.asks == ["explain raft"]
    assert qry.queries == []
    assert "Raft is a consensus algorithm." in say.messages[0]
    # Both the web source and the offer-to-save surface.
    assert "https://example.com/raft" in say.messages[0]
    assert "Save this answer" in say.messages[0]


# --------------------------------------------------------------------------------------
# intent gate: bare free-text routing (issue #5)
# --------------------------------------------------------------------------------------


def test_intent_gate_routes_capture_to_ingest_with_hint(config: Config) -> None:
    """A 'capture' verdict files bare prose as a note + a recoverable one-line hint."""
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="capture", confidence="high")
    handlers, ing, qry = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    text = "remind me to call the dentist tomorrow"
    handlers.handle_message(
        {"user": ALLOWED, "text": text, "channel": "D1", "ts": "5.1"}, say
    )
    # Filed as a note via ingest -- never answered/queried.
    assert gate.classified == [text]
    assert research.asks == []
    assert qry.queries == []
    assert len(ing.captures) == 1
    assert ing.captures[0].text == text  # full text, no prefix stripped
    assert ing.captures[0].url is None
    assert ing.captures[0].source == "slack"
    # The recoverable hint is appended so a misfile costs one reply to fix.
    assert "If you meant to ask" in say.messages[0]


def test_intent_gate_routes_query_to_vault_only(config: Config) -> None:
    """A 'query' verdict runs the vault-only query even when research is wired."""
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="query", confidence="high")
    handlers, ing, qry = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what did I save about exa?", "ts": "5.2"}, say
    )
    assert qry.queries == ["what did I save about exa?"]
    assert research.asks == []  # the read-only vault path, not the blended ask
    assert ing.captures == []


def test_intent_gate_routes_ask_to_blended(config: Config) -> None:
    """An 'ask' verdict takes the blended web+vault path (with offer-to-save)."""
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="ask", confidence="high")
    handlers, _, qry = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is raft?", "channel": "D1", "ts": "5.3"}, say
    )
    assert research.asks == ["what is raft?"]
    assert qry.queries == []
    assert "Save this answer" in say.messages[0]


def test_handle_message_logs_free_text_route(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """A bare free-text message logs the engine it was routed to (issue #52)."""
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="query", confidence="high")
    handlers, _, _ = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    with caplog.at_level("INFO", logger="thoth.slack_app"):
        handlers.handle_message(
            {"user": ALLOWED, "text": "what did I save about exa?", "ts": "5.2b"}, say
        )
    records = [
        r for r in caplog.records if "slack routed free text to" in r.getMessage()
    ]
    assert len(records) == 1
    assert "query" in records[0].getMessage()


def test_intent_gate_capture_with_no_research_still_files(config: Config) -> None:
    """A 'capture' verdict files even when no research engine is wired."""
    gate = FakeIntentClassifier(intent="capture", confidence="high")
    handlers, ing, qry = _handlers(config, intent_classifier=gate)  # research is None
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "the wifi password is hunter2", "ts": "5.4"}, say
    )
    assert len(ing.captures) == 1
    assert ing.captures[0].text == "the wifi password is hunter2"
    assert qry.queries == []


def test_intent_gate_low_confidence_routes_to_ask(config: Config) -> None:
    """A low-confidence verdict (whatever the intent) falls back to the blended ask.

    Answering a misfiled note is harmless; silently filing a real question is not, so
    the gate defaults to ask when unsure -- the capture is NOT filed.
    """
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="capture", confidence="low")
    handlers, ing, _ = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "is raft like paxos?", "channel": "D1", "ts": "5.5"},
        say,
    )
    assert ing.captures == []  # not filed despite the 'capture' guess
    assert research.asks == ["is raft like paxos?"]


def test_intent_gate_prefix_capture_skips_gate_and_hint(config: Config) -> None:
    """An explicit 'note:' prefix is filed deterministically -- gate never consulted."""
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="ask", confidence="high")
    handlers, ing, _ = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "note: buy milk", "ts": "5.6"}, say
    )
    assert gate.classified == []  # short-circuited before the gate
    assert ing.captures[0].text == "buy milk"
    # No recoverable hint on a deliberate, unambiguous prefix capture.
    assert "If you meant to ask" not in say.messages[0]


def test_intent_gate_research_prefix_bypasses_gate(config: Config) -> None:
    """A 'research:' marker is a deterministic ask escape hatch -- gate is skipped."""
    research = FakeResearch()
    gate = FakeIntentClassifier(intent="capture", confidence="high")
    handlers, ing, _ = _handlers(config, research=research, intent_classifier=gate)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "research: who won the 2022 final?", "ts": "5.9"},
        say,
    )
    assert gate.classified == []  # bypassed the gate
    assert ing.captures == []  # not filed despite the 'capture' verdict the gate holds
    assert research.asks == ["research: who won the 2022 final?"]


def test_free_text_without_gate_preserves_pre_gate_default(config: Config) -> None:
    """With no classifier wired, bare free text keeps the pre-gate behaviour.

    Blended ask when a research engine is wired; vault-only query when it is not.
    """
    research = FakeResearch()
    with_research, _, qry_a = _handlers(config, research=research)
    say = Recorder()
    with_research.handle_message(
        {"user": ALLOWED, "text": "explain raft", "channel": "D1", "ts": "5.7"}, say
    )
    assert research.asks == ["explain raft"]
    assert qry_a.queries == []

    no_research, _, qry_b = _handlers(config)
    no_research.handle_message(
        {"user": ALLOWED, "text": "explain raft", "ts": "5.8"}, Recorder()
    )
    assert qry_b.queries == ["explain raft"]


def test_handle_message_confirm_save_files_last_answer(config: Config) -> None:
    """A follow-up 'y' files the previous blended answer as a notes/ page."""
    research = FakeResearch(saved_path="notes/explain-raft.md")
    handlers, _, _ = _handlers(config, research=research)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "explain raft", "channel": "D1", "ts": "10.1"}, say
    )
    handlers.handle_message(
        {"user": ALLOWED, "text": "y", "channel": "D1", "ts": "10.2"}, say
    )
    # save_answer was called once, with the original question and the remembered result.
    assert len(research.saves) == 1
    assert research.saves[0][0] == "explain raft"
    # The save reply is the one concise shared ref: clickable title only, no wikilink.
    assert "obsidian://" in say.messages[-1]
    assert "|Explain Raft>" in say.messages[-1]
    assert ">: " not in say.messages[-1]  # title-only, no trailing path (issue #63)
    assert "[[" not in say.messages[-1]
    # A second 'y' has nothing pending and falls through to a fresh ask (not a save).
    handlers.handle_message(
        {"user": ALLOWED, "text": "y", "channel": "D1", "ts": "10.3"}, say
    )
    assert len(research.saves) == 1
    assert research.asks == ["explain raft", "y"]


def test_handle_message_confirm_save_is_per_channel(config: Config) -> None:
    """A 'y' in a channel with no pending answer does not save another channel's."""
    research = FakeResearch()
    handlers, _, _ = _handlers(config, research=research)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "explain raft", "channel": "D1", "ts": "11.1"}, say
    )
    # 'y' arrives in a DIFFERENT channel: no pending answer there -> falls through.
    handlers.handle_message(
        {"user": ALLOWED, "text": "y", "channel": "D2", "ts": "11.2"}, say
    )
    assert research.saves == []
    # D2's 'y' was treated as a fresh question instead.
    assert research.asks == ["explain raft", "y"]


def test_handle_message_save_rejection_is_fail_loud(config: Config) -> None:
    """A vault rejection on save is surfaced fail-loud, never swallowed."""
    research = FakeResearch(save_error=ResearchError("invalid slug"))
    handlers, _, _ = _handlers(config, research=research)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "explain raft", "channel": "D1", "ts": "12.1"}, say
    )
    handlers.handle_message(
        {"user": ALLOWED, "text": "yes", "channel": "D1", "ts": "12.2"}, say
    )
    assert ":x:" in say.messages[-1]
    assert "invalid slug" in say.messages[-1]


def test_handle_message_confirm_without_research_falls_through(config: Config) -> None:
    """Without a research engine, a 'y' is a normal vault-only query (no save path)."""
    handlers, _, qry = _handlers(config)  # research is None
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "y", "channel": "D1", "ts": "13.1"}, say
    )
    # It routed to the vault-only query path (the only one available).
    assert qry.queries == ["y"]


def test_handle_message_redelivery_dropped(config: Config) -> None:
    """The same event_id delivered twice is processed exactly once."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    event = {"user": ALLOWED, "text": "what is exa?", "event_id": "EV1"}
    handlers.handle_message(dict(event), say)
    handlers.handle_message(dict(event), say)
    assert qry.queries == ["what is exa?"]  # only once
    assert len(say.messages) == 1


def test_handle_message_ignores_bot_and_subtype(config: Config) -> None:
    """Bot messages and edit/join subtypes are ignored (no loop on own replies)."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message({"bot_id": "B1", "text": "hi", "user": ALLOWED}, say)
    handlers.handle_message(
        {"subtype": "message_changed", "text": "hi", "user": ALLOWED}, say
    )
    assert ing.captures == []
    assert qry.queries == []
    assert say.messages == []


def test_captioned_upload_ingests_the_file_and_ignores_caption(config: Config) -> None:
    """A captioned image upload ingests the FILE once; the caption is ignored.

    A DM file upload arrives as a ``message`` with subtype ``file_share`` carrying the
    full ``files`` objects. handle_message downloads + ingests each file by path (never
    base64) and never runs the caption as a query/ingest -- even a caption that looks
    like a bare URL (which guards the cross-handler double-processing the SPEC
    closed-surface review flagged; the bare ``file_shared`` event is now a no-op).
    """
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    client = FakeSlackClient(payload=b"PNGDATA")
    handlers.handle_message(
        {
            "user": ALLOWED,
            "subtype": "file_share",
            "text": "https://example.com/looks-like-a-url-caption",
            "ts": "10.1",
            "client_msg_id": "CM1",
            "files": [
                {
                    "name": "pic.png",
                    "url_private_download": "https://files.slack.com/pic.png",
                }
            ],
        },
        say,
        client,
    )
    assert client.downloaded == ["https://files.slack.com/pic.png"]
    assert len(ing.captures) == 1  # the file, exactly once
    assert ing.captures[0].path is not None
    assert ing.captures[0].url is None
    assert qry.queries == []  # the bare-URL caption did NOT become a query
    ing.captures[0].path.unlink()


def test_handle_message_blank_text_after_allow_is_ignored(config: Config) -> None:
    """An allowed user's empty message is dropped without ingest/query."""
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message({"user": ALLOWED, "text": "   ", "ts": "5.5"}, say)
    assert ing.captures == []
    assert qry.queries == []
    assert say.messages == []


def test_handle_message_query_error_is_fail_loud(config: Config) -> None:
    """A QueryError surfaces as an mrkdwn error, not an unhandled crash."""
    qry = FakeQueryEngine(error=QueryError("index.md missing"))
    handlers, _, _ = _handlers(config, query_engine=qry)
    say = Recorder()
    handlers.handle_message({"user": ALLOWED, "text": "anything?", "ts": "6.6"}, say)
    assert len(say.messages) == 1
    assert "index.md missing" in say.messages[0]


def test_handle_message_query_budget_exceeded_is_fail_safe(config: Config) -> None:
    """A budget trip on the vault-query path replies fail-safe, not a crash (#16)."""
    qry = FakeQueryEngine(error=BudgetExceededError("cap reached"))
    handlers, _, _ = _handlers(config, query_engine=qry)
    say = Recorder()
    handlers.handle_message({"user": ALLOWED, "text": "anything?", "ts": "6.9"}, say)
    assert len(say.messages) == 1
    assert "budget" in say.messages[0].lower()
    assert "still saved" in say.messages[0].lower()


def test_handle_message_ask_budget_exceeded_is_fail_safe(config: Config) -> None:
    """A budget trip on the blended-ask path replies fail-safe, not a crash (#16)."""
    research = FakeResearch(error=BudgetExceededError("cap reached"))
    handlers, _, _ = _handlers(config, research=research)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is raft?", "ts": "7.1"}, say
    )
    assert len(say.messages) == 1
    assert "budget" in say.messages[0].lower()


def test_handle_message_ingest_error_is_fail_loud(config: Config) -> None:
    """An IngestError surfaces as an mrkdwn error, not an unhandled crash."""
    ing = FakeIngestor(error=IngestError("bad file plan"))
    handlers, _, _ = _handlers(config, ingestor=ing)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "https://example.com", "ts": "7.7"}, say
    )
    assert len(say.messages) == 1
    assert "bad file plan" in say.messages[0]


def test_handle_message_vault_conflict_named_path(config: Config) -> None:
    """A VaultConflictError from the ingestor is rendered fail-loud with its detail."""
    ing = FakeIngestor(error=VaultConflictError("VAULT CONFLICT on entities/foo.md"))
    handlers, _, _ = _handlers(config, ingestor=ing)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "https://example.com", "ts": "8.8"}, say
    )
    assert len(say.messages) == 1
    assert "conflict" in say.messages[0].lower()
    assert "entities/foo.md" in say.messages[0]


# --------------------------------------------------------------------------------------
# unpushed-divergence alert (issue #15)
# --------------------------------------------------------------------------------------


def test_conflict_report_routes_an_unpushed_divergence_alert(config: Config) -> None:
    """A report.conflict ingest also routes an unpushed-divergence alert to Slack.

    Acceptance (issue #15): forcing a push conflict produces a visible alert with the
    commits-ahead count + oldest-unpushed time read from git.
    """
    report = _report(
        committed=False, conflict=True, message="VAULT CONFLICT: paths concepts/x.md"
    )
    ing = FakeIngestor(report=report)
    alerter = FakeAlerter()
    since = datetime(2026, 5, 29, 8, 0, tzinfo=UTC)
    git = FakeGitSync(Divergence(commits_ahead=2, since=since))
    handlers, _, _ = _handlers(config, ingestor=ing, alerter=alerter, git=git)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "capture: a note", "ts": "9.9"}, say
    )
    # The in-thread reply is still the fail-loud conflict line...
    assert any("conflict" in m.lower() for m in say.messages)
    # ...and exactly one divergence alert was routed with the git-derived count + time.
    assert git.calls == 1
    assert alerter.divergences == [(2, since, "VAULT CONFLICT: paths concepts/x.md")]


def test_raised_vault_conflict_routes_a_divergence_alert(config: Config) -> None:
    """A raised VaultConflictError (not just a report) also routes the alert."""
    ing = FakeIngestor(error=VaultConflictError("VAULT CONFLICT on entities/foo.md"))
    alerter = FakeAlerter()
    git = FakeGitSync(Divergence(commits_ahead=1, since=None))
    handlers, _, _ = _handlers(config, ingestor=ing, alerter=alerter, git=git)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "https://example.com", "ts": "8.8"}, say
    )
    assert len(alerter.divergences) == 1
    ahead, _since, detail = alerter.divergences[0]
    assert ahead == 1
    assert "entities/foo.md" in detail


def test_no_divergence_alert_on_a_clean_ingest(config: Config) -> None:
    """A normal (non-conflict) ingest routes no divergence alert."""
    alerter = FakeAlerter()
    git = FakeGitSync(Divergence(commits_ahead=0, since=None))
    handlers, _, _ = _handlers(config, alerter=alerter, git=git)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "capture: hello", "ts": "1.1"}, say
    )
    assert alerter.divergences == []
    assert git.calls == 0


def test_conflict_without_alerter_is_a_clean_noop(config: Config) -> None:
    """With no alerter wired a conflict still replies but routes no alert (no crash)."""
    report = _report(committed=False, conflict=True, message="VAULT CONFLICT")
    handlers, _, _ = _handlers(config, ingestor=FakeIngestor(report=report))
    say = Recorder()
    # Must not raise even though no alerter/git is configured.
    handlers.handle_message({"user": ALLOWED, "text": "capture: x", "ts": "2.2"}, say)
    assert any("conflict" in m.lower() for m in say.messages)


# --------------------------------------------------------------------------------------
# serve_with_alerting: daemon top-level supervision (issue #15)
# --------------------------------------------------------------------------------------


def test_serve_with_alerting_reports_and_reraises_on_crash() -> None:
    """An unhandled daemon exception is alerted, then re-raised (systemd sees exit).

    Acceptance (issue #15): killing the daemon / exhausting a quota surfaces a Slack
    alert within a bounded window -- here the top-level loop posts before the process
    exits non-zero.
    """
    alerter = FakeAlerter()
    boom = RuntimeError("socket died")

    def serve() -> None:
        raise boom

    with pytest.raises(RuntimeError, match="socket died"):
        serve_with_alerting(serve, alerter)
    assert alerter.exceptions == [("slack daemon", boom)]


def test_serve_with_alerting_clean_return_posts_nothing() -> None:
    """A daemon loop that returns normally routes no alert."""
    alerter = FakeAlerter()
    ran = {"n": 0}

    def serve() -> None:
        ran["n"] += 1

    serve_with_alerting(serve, alerter)
    assert ran["n"] == 1
    assert alerter.exceptions == []


def test_serve_with_alerting_clean_stop_is_silent() -> None:
    """A clean stop (SystemExit/KeyboardInterrupt) re-raises without alerting.

    A routine ``systemctl stop`` / deploy restart unwinds the blocking loop via
    SystemExit or KeyboardInterrupt; that is not a crash and must not post an alert
    (which would cause alert fatigue), but it still propagates so the process exits.
    """
    alerter = FakeAlerter()

    def serve_sysexit() -> None:
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        serve_with_alerting(serve_sysexit, alerter)

    def serve_ctrl_c() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        serve_with_alerting(serve_ctrl_c, alerter)

    assert alerter.exceptions == []


# --------------------------------------------------------------------------------------
# file uploads (message / file_share subtype)
# --------------------------------------------------------------------------------------


def _file_share_event(*files: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Build a ``message``/``file_share`` event carrying the given file objects."""
    event: dict[str, Any] = {
        "user": ALLOWED,
        "subtype": "file_share",
        "channel": "D1",
        "text": "",
        "ts": "20.1",
        "client_msg_id": "CMF",
        "files": list(files),
    }
    event.update(overrides)
    return event


def test_file_upload_downloads_to_tmp_and_ingests(config: Config) -> None:
    """An allowed file upload is downloaded to a tmp path and ingested by path."""
    client = FakeSlackClient(payload=b"PNGDATA")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        _file_share_event(
            {
                "name": "diagram.png",
                "url_private_download": "https://files.slack.com/diagram.png",
            }
        ),
        say,
        client,
    )
    assert client.downloaded == ["https://files.slack.com/diagram.png"]
    assert len(ing.captures) == 1
    capture = ing.captures[0]
    assert capture.path is not None
    assert capture.url is None
    assert capture.text is None
    assert capture.filename == "diagram.png"
    assert capture.source == "slack"
    # The bytes really landed on disk (server-side, never base64).
    assert capture.path.read_bytes() == b"PNGDATA"
    assert capture.path.suffix == ".png"
    capture.path.unlink()


def test_file_upload_with_multiple_files_ingests_each(config: Config) -> None:
    """An upload carrying several files ingests every one of them."""
    client = FakeSlackClient(payload=b"DATA")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        _file_share_event(
            {"name": "a.png", "url_private_download": "https://files.slack.com/a.png"},
            {"name": "b.pdf", "url_private": "https://files.slack.com/b.pdf"},
        ),
        say,
        client,
    )
    assert client.downloaded == [
        "https://files.slack.com/a.png",
        "https://files.slack.com/b.pdf",
    ]
    assert [c.filename for c in ing.captures] == ["a.png", "b.pdf"]
    for capture in ing.captures:
        assert capture.path is not None
        capture.path.unlink()


def test_file_upload_denied_user_not_downloaded(config: Config) -> None:
    """A non-allowed user is rejected BEFORE any download or ingest."""
    client = FakeSlackClient(payload=b"X")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        _file_share_event(
            {"url_private_download": "https://files.slack.com/x.png"}, user=DENIED
        ),
        say,
        client,
    )
    assert client.downloaded == []
    assert ing.captures == []
    assert len(say.messages) == 1
    assert "not authorised" in say.messages[0].lower()


def test_file_upload_no_url_warns(config: Config) -> None:
    """A file with no downloadable URL warns and does not ingest."""
    client = FakeSlackClient(payload=b"X")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(_file_share_event({"name": "x.png"}), say, client)
    assert ing.captures == []
    assert client.downloaded == []
    assert len(say.messages) == 1
    assert "could not find" in say.messages[0].lower()


def test_file_upload_redelivery_dropped(config: Config) -> None:
    """A redelivered file_share message downloads + ingests exactly once."""
    client = FakeSlackClient(payload=b"PDF")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    event = _file_share_event(
        {"name": "scan.pdf", "url_private": "https://files.slack.com/scan.pdf"}
    )
    handlers.handle_message(dict(event), say, client)
    handlers.handle_message(dict(event), say, client)
    assert len(ing.captures) == 1
    assert len(client.downloaded) == 1
    ing.captures[0].path.unlink()  # type: ignore[union-attr]


def test_download_bytes_uses_token_get_when_no_download_helper(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a real WebClient-like client (token, no .download), bytes are fetched via an
    authenticated https GET carrying the bot token."""

    class TokenClient:
        token = "xoxb-probe"

    captured: dict[str, Any] = {}

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"REALBYTES"

    def fake_urlopen(request: Any, timeout: float = 0.0) -> _Resp:
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr("thoth.slack_app.urllib.request.urlopen", fake_urlopen)
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        _file_share_event(
            {"name": "p.png", "url_private_download": "https://files.slack.com/p.png"}
        ),
        say,
        cast(Any, TokenClient()),
    )
    assert captured["url"] == "https://files.slack.com/p.png"
    assert captured["auth"] == "Bearer xoxb-probe"
    assert len(ing.captures) == 1
    assert ing.captures[0].path is not None
    assert ing.captures[0].path.read_bytes() == b"REALBYTES"
    ing.captures[0].path.unlink()


def test_download_without_token_or_helper_is_fail_loud(config: Config) -> None:
    """A client with neither a download helper nor a token is surfaced fail-loud."""

    class NoCapability:
        pass

    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        _file_share_event(
            {"name": "x.png", "url_private_download": "https://files.slack.com/x.png"}
        ),
        say,
        cast(Any, NoCapability()),
    )
    assert ing.captures == []
    assert len(say.messages) == 1
    assert ":x:" in say.messages[0]
    assert "could not download" in say.messages[0].lower()


# --------------------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------------------


def test_render_citation_is_title_only_clickable_link() -> None:
    """render_citation emits <uri|title> only: no trailing path, no wikilink (#63)."""
    rendered = render_citation(_citation())
    assert rendered == (
        "<obsidian://open?vault=pkm-vault&file=concepts%2Fexa-search.md|Exa Search>"
    )
    assert "[[" not in rendered  # the dead wikilink is gone
    assert ">: " not in rendered  # the trailing vault path is gone (issue #63)


def test_render_citation_falls_back_to_path_label() -> None:
    """When a citation has no title, the path is used as the link label."""
    rendered = render_citation(_citation(title=""))
    assert rendered == (
        "<obsidian://open?vault=pkm-vault&file=concepts%2Fexa-search.md"
        "|concepts/exa-search.md>"
    )


def test_render_query_result_lists_every_citation() -> None:
    """render_query_result shows the answer then one concise line per citation."""
    result = _result(
        answer="Two engines.",
        citations=[
            _citation(path="concepts/exa.md", title="Exa", slug="exa"),
            _citation(path="concepts/firecrawl.md", title="Firecrawl", slug="fc"),
        ],
    )
    rendered = render_query_result(result)
    assert rendered.startswith("Two engines.")
    assert "<obsidian://open?vault=pkm-vault&file=concepts%2Fexa.md|Exa>" in rendered
    assert (
        "<obsidian://open?vault=pkm-vault&file=concepts%2Ffirecrawl.md|Firecrawl>"
        in rendered
    )
    assert "[[" not in rendered
    assert ">: " not in rendered  # title-only, no trailing path (issue #63)


def test_render_query_result_no_citations_renders_no_note() -> None:
    """A citation-less answer is the prose alone -- no trailing note (issue #53)."""
    rendered = render_query_result(_result(answer="No idea.", citations=[]))
    assert rendered == "No idea."
    assert "obsidian://" not in rendered
    assert "no vault sources" not in rendered.lower()


def test_render_ask_result_combines_vault_and_web_with_offer() -> None:
    """render_ask_result lists vault + web sources (concise) and the offer line."""
    result = _ask_result()
    rendered = render_ask_result(result)
    assert rendered.startswith("Raft is a consensus algorithm.")
    # Both vault and web sources are title-only clickable links (issue #63).
    assert (
        "<obsidian://open?vault=pkm-vault&file=concepts%2Fexa-search.md|Exa Search>"
        in rendered
    )
    assert "<https://example.com/raft|Raft>" in rendered
    assert "Save this answer" in rendered
    assert "[[" not in rendered
    assert ">: " not in rendered  # no trailing path on any ref (issue #63)


def test_render_ask_result_offer_suppressed_when_asked() -> None:
    """offer_save=False omits the save line (used for an empty/edge answer)."""
    rendered = render_ask_result(_ask_result(), offer_save=False)
    assert "Save this answer" not in rendered


def test_no_slack_renderer_emits_dead_wikilinks() -> None:
    """No Slack-facing renderer leaks an un-clickable [[wikilink]] (issue #53)."""
    outputs = [
        render_citation(_citation()),
        render_query_result(_result()),
        render_ask_result(_ask_result()),
        render_ingest_report(_report()),
    ]
    for rendered in outputs:
        assert "[[" not in rendered


def test_pending_saves_remember_take_is_one_shot() -> None:
    """A remembered answer is returned once by take(), then gone."""
    pending = PendingSaves()
    result = _ask_result()
    pending.remember("D1", "explain raft", result)
    taken = pending.take("D1")
    assert taken is not None
    assert taken[0] == "explain raft"
    assert taken[1] is result
    # A second take finds nothing.
    assert pending.take("D1") is None


def test_pending_saves_prune_drops_expired_with_injected_clock() -> None:
    """An offer older than the TTL is pruned and no longer takeable."""
    clock = {"t": 1000.0}
    pending = PendingSaves(ttl_seconds=100.0, clock=lambda: clock["t"])
    pending.remember("D1", "q", _ask_result())
    clock["t"] = 1101.0  # advance just past the TTL
    assert pending.take("D1") is None


def test_render_ingest_report_is_concise_with_clickable_ref() -> None:
    """render_ingest_report shows a Filed header + one title-only clickable ref (#63).

    The ref is a single clickable ``<uri|title>`` link with no trailing path and no
    dead [[wikilink]] (issue #63).
    """
    rendered = render_ingest_report(_report())
    lines = rendered.splitlines()
    assert lines[0] == "Filed 1 page(s):"
    assert lines[1] == (
        "<obsidian://open?vault=pkm-vault&file=concepts%2Fexa.md|Exa Search>"
    )
    assert "[[" not in rendered
    assert ">: " not in rendered  # no trailing vault path (issue #63)


def test_render_ingest_report_not_committed_marks_header() -> None:
    """An un-committed filed report flags '(not yet committed)' on the header."""
    rendered = render_ingest_report(_report(committed=False))
    assert rendered.splitlines()[0] == "Filed 1 page(s): (not yet committed)"


def test_render_ingest_report_conflict_is_fail_loud() -> None:
    """A conflict report renders a warning naming the path, not a success line."""
    report = _report(
        conflict=True,
        committed=False,
        message="VAULT CONFLICT on entities/foo.md",
    )
    rendered = render_ingest_report(report)
    assert "conflict" in rendered.lower()
    assert "entities/foo.md" in rendered


def test_render_ingest_report_falls_back_to_raw_paths() -> None:
    """With no curated page, the raw path is named instead."""
    report = _report(page_paths=[], raw_paths=["raw/transcripts/memo.md"])
    rendered = render_ingest_report(report)
    assert "raw/transcripts/memo.md" in rendered


def test_render_ingest_report_deferred_is_partial_success() -> None:
    """A deferred report says raw saved + curation deferred, naming the held page."""
    report = _report(
        page_paths=[],
        raw_paths=["inbox/hold-deadbeef0000.md"],
        committed=True,
        deferred=True,
        message="curation deferred -- LLM unavailable",
    )
    rendered = render_ingest_report(report)
    assert "inbox/hold-deadbeef0000.md" in rendered
    assert "deferred" in rendered.lower()
    assert "saved raw" in rendered.lower()


def test_render_ingest_report_multi_page_one_ref_each() -> None:
    """A multi-page report renders one title-only ref line per page (issue #63)."""
    report = _report(
        page_paths=["concepts/a.md", "concepts/b.md"],
        obsidian_links=[
            "obsidian://open?vault=pkm-vault&file=concepts%2Fa.md",
            "obsidian://open?vault=pkm-vault&file=concepts%2Fb.md",
        ],
        wikilinks=["[[a]]", "[[b]]"],
        titles=["Page A", "Page B"],
    )
    rendered = render_ingest_report(report)
    lines = rendered.splitlines()
    assert lines[0] == "Filed 2 page(s):"
    assert lines[1] == "<obsidian://open?vault=pkm-vault&file=concepts%2Fa.md|Page A>"
    assert lines[2] == "<obsidian://open?vault=pkm-vault&file=concepts%2Fb.md|Page B>"
    assert "[[" not in rendered
    assert ">: " not in rendered  # title-only, no trailing path (issue #63)


# --------------------------------------------------------------------------------------
# processing feedback: placeholder post + chat.update (issue #34, Slice B)
# --------------------------------------------------------------------------------------


def test_query_posts_placeholder_then_updates_it(config: Config) -> None:
    """A query posts an immediate placeholder, then edits THAT message with the answer.

    Acceptance (issue #34, Slice B): a fake Slack client records the placeholder
    ``chat.postMessage`` and the final ``chat.update`` (no second post); the placeholder
    is a "Looking…" line and the edit carries the rendered answer.
    """
    client = FakeSlackClient()
    handlers, _, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is exa?", "channel": "D1", "ts": "2.2"},
        say,
        client,
    )
    assert qry.queries == ["what is exa?"]
    # An immediate placeholder was posted to the channel...
    assert len(client.posts) == 1
    assert client.posts[0]["channel"] == "D1"
    assert "Looking" in client.posts[0]["text"]
    # ...and the SAME message was edited in place with the final render (no 2nd post).
    assert len(client.updates) == 1
    assert client.updates[0]["ts"] == "1700000000.000100"
    assert "Exa is a semantic search engine." in client.updates[0]["text"]
    # The single-say fallback was NOT used (the edit carried the reply).
    assert say.messages == []


def test_ingest_posts_filing_placeholder_then_updates(config: Config) -> None:
    """A capture posts a "Filing…" placeholder, then edits it with the confirmation."""
    client = FakeSlackClient()
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "note: buy milk", "channel": "D1", "ts": "3.3"},
        say,
        client,
    )
    assert len(ing.captures) == 1
    assert len(client.posts) == 1
    assert "Filing" in client.posts[0]["text"]
    assert len(client.updates) == 1
    assert "Filed 1 page(s):" in client.updates[0]["text"]
    assert say.messages == []


def test_feedback_falls_back_to_single_say_without_client(config: Config) -> None:
    """With no web client (the text-only path) the reply is one say, no placeholder.

    The placeholder+update is best-effort UX layered over the existing single-say
    contract; a client-less call (or non-DM path) must still deliver exactly one reply.
    """
    handlers, _, qry = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is exa?", "channel": "D1", "ts": "2.2"}, say
    )
    assert qry.queries == ["what is exa?"]
    assert len(say.messages) == 1
    assert "Exa is a semantic search engine." in say.messages[0]


def test_feedback_falls_back_when_post_returns_no_ts(config: Config) -> None:
    """When the placeholder post yields no ts, the final reply falls back to a say."""
    client = FakeSlackClient(post_ts=None)
    handlers, _, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is exa?", "channel": "D1", "ts": "2.2"},
        say,
        client,
    )
    # The placeholder still posted, but with no ts there is nothing to edit...
    assert len(client.posts) == 1
    assert client.updates == []
    # ...so the answer came through the single-say fallback instead.
    assert len(say.messages) == 1
    assert "Exa is a semantic search engine." in say.messages[0]


def test_responder_finish_falls_back_when_update_raises() -> None:
    """A failed chat.update degrades to a fresh say so the reply is never lost."""

    class FlakyClient:
        def __init__(self) -> None:
            self.updates = 0

        def chat_postMessage(  # noqa: N802 - SDK name
            self, *, channel: str, text: str, **kwargs: Any
        ) -> dict[str, Any]:
            return {"ok": True, "ts": "9.9"}

        def chat_update(  # noqa: N802 - SDK name
            self, *, channel: str, ts: str, text: str, **kwargs: Any
        ) -> dict[str, Any]:
            self.updates += 1
            raise RuntimeError("edit window expired")

    say = Recorder()
    responder = Responder(say, client=cast(Any, FlakyClient()), channel="D1")
    responder.progress(":mag: Looking…")
    responder.finish("the answer")
    assert say.messages == ["the answer"]


# --------------------------------------------------------------------------------------
# concise rendering: the answer Sources block carries no dead [[wikilink]] (#53, #34)
# --------------------------------------------------------------------------------------


def test_query_renders_concise_sources_no_wikilink(config: Config) -> None:
    """A query renders the concise #53 Sources block -- no dead wikilink in Slack."""
    handlers, _, _ = _handlers(config)
    say = Recorder()
    handlers.handle_message(
        {"user": ALLOWED, "text": "what is exa?", "ts": "31.1"}, say
    )
    assert "*Sources:*" in say.messages[0]
    assert "[[" not in say.messages[0]


def test_render_query_result_has_no_wikilink() -> None:
    """render_query_result keeps #53's concise, no-wikilink Sources block."""
    rendered = render_query_result(_result())
    assert "*Sources:*" in rendered
    assert "[[" not in rendered


def test_render_ask_result_has_no_wikilink() -> None:
    """render_ask_result keeps #53's concise Sources block -- no dead wikilink."""
    rendered = render_ask_result(_ask_result())
    assert "*Sources:*" in rendered
    assert "[[" not in rendered


# --------------------------------------------------------------------------------------
# import safety + lazy slack_bolt
# --------------------------------------------------------------------------------------


def test_importing_module_does_not_import_slack_bolt() -> None:
    """Importing thoth.slack_app must not pull in slack_bolt (lazy only)."""
    # The module is already imported by this test file's top-level import; the contract
    # is that doing so did not import slack_bolt.
    assert "thoth.slack_app" in sys.modules
    assert "slack_bolt" not in sys.modules


def test_slack_bolt_is_absent_in_ci() -> None:
    """slack_bolt is an optional extra, absent in CI; the module imported anyway."""
    # If slack_bolt is ever present locally this assertion is skipped, but the import
    # of thoth.slack_app at the top of this file already proves module import is safe.
    if importlib.util.find_spec("slack_bolt") is None:
        with pytest.raises(ImportError):
            import slack_bolt  # noqa: F401


def test_build_app_raises_clearly_without_slack_bolt(config: Config) -> None:
    """build_app raises ImportError (not at module import) when slack_bolt absent."""
    if importlib.util.find_spec("slack_bolt") is not None:
        pytest.skip("slack_bolt is installed; cannot assert the missing-dep path")
    ing = cast(Ingestor, FakeIngestor())
    qry = cast(QueryEngine, FakeQueryEngine())
    cfg_with_tokens = load_config(
        {
            "PKM_VAULT": "/x",
            "SLACK_BOT_TOKEN": FAKE_TOKEN,
            "SLACK_APP_TOKEN": FAKE_TOKEN,
        }
    )
    with pytest.raises(ImportError):
        build_app(cfg_with_tokens, ing, qry)


# --------------------------------------------------------------------------- #
# Standing-directive commands: remember / forget / list (issue #43).
# --------------------------------------------------------------------------- #


def test_remember_prefix_adds_directive_and_confirms() -> None:
    """``remember: <rule>`` stores the rule on the vault and confirms it."""
    stub = _StubIngestor(_make_report())
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(
        _event(text="remember: tag work notes with #work"), sent.append
    )
    assert stub._vault.get_directives() == ["tag work notes with #work"]
    assert "tag work notes with #work" in sent[0]
    # A directive command never reaches the capture/ingest path.
    assert not stub.captures


def test_remember_duplicate_is_reported_not_duplicated() -> None:
    """A repeated ``remember:`` is idempotent and says so."""
    stub = _StubIngestor(_make_report())
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="remember: be concise"), sent.append)
    handlers.handle_message(_event(text="remember: be concise"), sent.append)
    assert stub._vault.get_directives() == ["be concise"]
    assert "Already remembered" in sent[1]


def test_forget_prefix_removes_directive_by_text() -> None:
    """``forget: <rule>`` removes a matching directive."""
    vault = _FakeVault()
    vault.add_directive("rule one")
    vault.add_directive("rule two")
    stub = _StubIngestor(_make_report(), vault=vault)
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="forget: rule one"), sent.append)
    assert vault.get_directives() == ["rule two"]
    assert "Forgotten" in sent[0]


def test_forget_prefix_removes_directive_by_index() -> None:
    """``forget: <index>`` removes the directive at that 1-based position."""
    vault = _FakeVault()
    vault.add_directive("rule one")
    vault.add_directive("rule two")
    stub = _StubIngestor(_make_report(), vault=vault)
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="forget: 1"), sent.append)
    assert vault.get_directives() == ["rule two"]
    assert "Forgotten" in sent[0]


def test_forget_unknown_directive_reports_no_match() -> None:
    """``forget:`` with no match tells the user nothing was removed."""
    stub = _StubIngestor(_make_report())
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="forget: never set"), sent.append)
    assert "No matching directive" in sent[0]


def test_list_shows_current_directives() -> None:
    """A bare ``list`` shows the numbered directives."""
    vault = _FakeVault()
    vault.add_directive("first rule")
    vault.add_directive("second rule")
    stub = _StubIngestor(_make_report(), vault=vault)
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="list"), sent.append)
    assert "first rule" in sent[0]
    assert "second rule" in sent[0]
    assert not stub.captures


def test_directives_word_shows_current_directives() -> None:
    """A bare ``directives`` is an alias for ``list``."""
    vault = _FakeVault()
    vault.add_directive("only rule")
    stub = _StubIngestor(_make_report(), vault=vault)
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="directives"), sent.append)
    assert "only rule" in sent[0]


def test_list_with_no_directives_shows_empty_state() -> None:
    """``list`` with no directives shows the empty-state hint."""
    stub = _StubIngestor(_make_report())
    handlers = _handlers(ingestor=stub)
    sent: list[str] = []
    handlers.handle_message(_event(text="list"), sent.append)
    assert "No standing directives" in sent[0]
