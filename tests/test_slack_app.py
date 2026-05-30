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
from typing import Any, cast

import pytest

from thoth.config import Config, load_config
from thoth.git_sync import VaultConflictError
from thoth.ingest import Capture, IngestError, Ingestor, IngestReport
from thoth.query import Citation, QueryEngine, QueryError, QueryResult
from thoth.research import AskResult, ResearchEngine, ResearchError, WebCitation
from thoth.slack_app import (
    DEDUPE_TTL_SECONDS,
    EventDedupe,
    Handlers,
    PendingSaves,
    SlackError,
    build_app,
    parse_allowed_users,
    render_ask_result,
    render_citation,
    render_ingest_report,
    render_query_result,
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


class Recorder:
    """A fake ``say`` callable that captures every reply string."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        """Record one reply."""
        self.messages.append(text)


class FakeSlackClient:
    """A fake Slack web client exposing files_info + download (no network)."""

    def __init__(
        self,
        *,
        file_info: dict[str, Any] | None = None,
        payload: bytes = b"binary-bytes",
    ) -> None:
        self.files_info_calls: list[str] = []
        self.downloaded: list[str] = []
        self._file_info = file_info
        self._payload = payload

    def chat_postMessage(  # noqa: N802 - Slack SDK method name
        self, *, channel: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Record nothing useful here; present to satisfy the protocol."""
        return {"ok": True}

    def files_info(self, *, file: str) -> dict[str, Any]:  # noqa: N802 - SDK name
        """Return canned file metadata for a ``file_shared`` lookup."""
        self.files_info_calls.append(file)
        return {"ok": True, "file": self._file_info or {}}

    def download(self, url: str) -> bytes:
        """Record the URL and return canned bytes (stands in for an HTTP GET)."""
        self.downloaded.append(url)
        return self._payload


def _handlers(
    config: Config,
    *,
    ingestor: FakeIngestor | None = None,
    query_engine: FakeQueryEngine | None = None,
    research: FakeResearch | None = None,
    allowed: frozenset[str] = frozenset({ALLOWED}),
    dedupe: EventDedupe | None = None,
    pending_saves: PendingSaves | None = None,
) -> tuple[Handlers, FakeIngestor, FakeQueryEngine]:
    """Construct Handlers wired to fakes, returning the fakes for assertions.

    When ``research`` is given the free-text path takes the blended ask; otherwise it is
    ``None`` and the free-text path stays vault-only (the existing behaviour). The
    research fake is reachable on the returned ``Handlers.research`` for assertions.
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
    if dedupe is not None:
        kwargs["dedupe"] = dedupe
    if pending_saves is not None:
        kwargs["pending_saves"] = pending_saves
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
    assert "[[exa-search]]" in say.messages[0]


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


def test_handle_message_confirm_save_files_last_answer(config: Config) -> None:
    """A follow-up 'y' files the previous blended answer as a queries/ page."""
    research = FakeResearch(saved_path="queries/explain-raft.md")
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
    assert "queries/explain-raft.md" in say.messages[-1]
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


def test_handle_message_ignores_file_share_subtype(config: Config) -> None:
    """A file-upload 'message' (subtype file_share) is ignored by handle_message.

    A Slack DM file upload fans out into BOTH a message (subtype ``file_share``) and a
    separate ``file_shared`` event; the file is ingested only by handle_file_shared.
    handle_message must therefore NOT also act on the upload message (which would
    double-process a captioned upload: caption -> a query/second ingest while the file
    is ingested). The caption is intentionally dropped here.
    """
    handlers, ing, qry = _handlers(config)
    say = Recorder()
    # A captioned upload message: text caption + the file_share subtype.
    handlers.handle_message(
        {
            "user": ALLOWED,
            "subtype": "file_share",
            "text": "what is in this picture?",
            "ts": "9.9",
            "files": [{"id": "F1", "name": "pic.png"}],
        },
        say,
    )
    assert ing.captures == []  # no second ingest from the caption
    assert qry.queries == []  # the caption is not run as a query
    assert say.messages == []


def test_captioned_upload_ingests_exactly_once(config: Config) -> None:
    """A captioned image upload is ingested EXACTLY once (only via file_shared).

    Drives both events Slack emits for one captioned upload through their respective
    handlers and asserts a single ingest with the file path (never a duplicate from the
    caption message). The two handlers carry different dedupe keys, so this guards the
    cross-handler double-processing the SPEC closed-surface review flagged.
    """
    dedupe = EventDedupe(clock=lambda: 0.0)
    handlers, ing, qry = _handlers(config, dedupe=dedupe)
    say = Recorder()
    file_info = {
        "name": "pic.png",
        "url_private_download": "https://files.slack.com/pic.png",
    }
    client = FakeSlackClient(file_info=file_info, payload=b"PNGDATA")

    # 1) The message event for the captioned upload (subtype file_share, has caption).
    handlers.handle_message(
        {
            "user": ALLOWED,
            "subtype": "file_share",
            "text": "https://example.com/looks-like-a-url-caption",
            "ts": "10.1",
            "client_msg_id": "CM1",
        },
        say,
    )
    # 2) The separate file_shared event for the same upload.
    handlers.handle_file_shared(
        {"user_id": ALLOWED, "file_id": "F1", "event_id": "EVF10"}, client, say
    )

    assert len(ing.captures) == 1  # exactly one ingest, from file_shared
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
# handle_file_shared
# --------------------------------------------------------------------------------------


def test_handle_file_shared_downloads_to_tmp_and_ingests(config: Config) -> None:
    """An allowed file upload is downloaded to a tmp path and ingested by path."""
    file_info = {
        "name": "diagram.png",
        "url_private_download": "https://files.slack.com/diagram.png",
    }
    client = FakeSlackClient(file_info=file_info, payload=b"PNGDATA")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_file_shared(
        {"user_id": ALLOWED, "file_id": "F1", "event_id": "EVF1"}, client, say
    )
    assert client.downloaded == ["https://files.slack.com/diagram.png"]
    assert len(ing.captures) == 1
    capture = ing.captures[0]
    assert capture.path is not None
    assert capture.url is None
    assert capture.text is None
    assert capture.filename == "diagram.png"
    # The bytes really landed on disk (server-side, never base64).
    assert capture.path.read_bytes() == b"PNGDATA"
    assert capture.path.suffix == ".png"
    capture.path.unlink()


def test_handle_file_shared_denied_user_not_downloaded(config: Config) -> None:
    """A non-allowed user is rejected BEFORE any download or ingest."""
    client = FakeSlackClient(
        file_info={"url_private_download": "https://files.slack.com/x.png"}
    )
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_file_shared(
        {"user_id": DENIED, "file_id": "F1", "event_id": "EVF2"}, client, say
    )
    assert client.downloaded == []
    assert client.files_info_calls == []
    assert ing.captures == []
    assert len(say.messages) == 1
    assert "not authorised" in say.messages[0].lower()


def test_handle_file_shared_no_url_warns(config: Config) -> None:
    """A file with no downloadable URL warns and does not ingest."""
    client = FakeSlackClient(file_info={"name": "x.png"})  # no url_private*
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_file_shared(
        {"user_id": ALLOWED, "file_id": "F1", "event_id": "EVF3"}, client, say
    )
    assert ing.captures == []
    assert client.downloaded == []
    assert len(say.messages) == 1
    assert "could not find" in say.messages[0].lower()


def test_handle_file_shared_redelivery_dropped(config: Config) -> None:
    """A redelivered file_shared event downloads + ingests exactly once."""
    file_info = {
        "name": "scan.pdf",
        "url_private": "https://files.slack.com/scan.pdf",
    }
    client = FakeSlackClient(file_info=file_info, payload=b"PDF")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    event = {"user_id": ALLOWED, "file_id": "F9", "event_id": "EVF9"}
    handlers.handle_file_shared(dict(event), client, say)
    handlers.handle_file_shared(dict(event), client, say)
    assert len(ing.captures) == 1
    assert len(client.downloaded) == 1
    ing.captures[0].path.unlink()  # type: ignore[union-attr]


def test_handle_file_shared_embedded_file_object(config: Config) -> None:
    """A payload that embeds the file object skips the files_info round trip."""
    embedded = {
        "name": "photo.jpg",
        "url_private_download": "https://files.slack.com/photo.jpg",
    }
    client = FakeSlackClient(payload=b"JPG")
    handlers, ing, _ = _handlers(config)
    say = Recorder()
    handlers.handle_file_shared(
        {"user_id": ALLOWED, "file": embedded, "event_id": "EVF4"}, client, say
    )
    assert client.files_info_calls == []  # used embedded object
    assert client.downloaded == ["https://files.slack.com/photo.jpg"]
    assert len(ing.captures) == 1
    ing.captures[0].path.unlink()  # type: ignore[union-attr]


def test_download_bytes_without_capability_raises(config: Config) -> None:
    """A client lacking a download method raises SlackError (no silent loss)."""

    class NoDownload:
        def files_info(self, *, file: str) -> dict[str, Any]:  # noqa: N802
            return {
                "file": {
                    "name": "x.png",
                    "url_private_download": "https://files.slack.com/x.png",
                }
            }

    handlers, _, _ = _handlers(config)
    say = Recorder()
    with pytest.raises(SlackError):
        handlers.handle_file_shared(
            {"user_id": ALLOWED, "file_id": "F1", "event_id": "EVF5"},
            cast(Any, NoDownload()),
            say,
        )


# --------------------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------------------


def test_render_citation_has_link_path_and_wikilink() -> None:
    """render_citation emits <uri|label>, the plain path, and the [[wikilink]]."""
    rendered = render_citation(_citation())
    assert "obsidian://open?vault=pkm-vault&file=concepts%2Fexa-search.md" in rendered
    assert "Exa Search" in rendered  # the label
    assert "`concepts/exa-search.md`" in rendered  # scheme-independent plain path
    assert "[[exa-search]]" in rendered  # scheme-independent wikilink


def test_render_citation_falls_back_to_path_label() -> None:
    """When a citation has no title, the path is used as the link label."""
    rendered = render_citation(_citation(title=""))
    assert "|concepts/exa-search.md>" in rendered


def test_render_query_result_lists_every_citation() -> None:
    """render_query_result shows the answer then one line per citation."""
    result = _result(
        answer="Two engines.",
        citations=[
            _citation(path="concepts/exa.md", title="Exa", slug="exa"),
            _citation(path="concepts/firecrawl.md", title="Firecrawl", slug="fc"),
        ],
    )
    rendered = render_query_result(result)
    assert rendered.startswith("Two engines.")
    assert "[[exa]]" in rendered
    assert "[[fc]]" in rendered
    assert "`concepts/exa.md`" in rendered
    assert "`concepts/firecrawl.md`" in rendered


def test_render_query_result_no_citations_notes_it() -> None:
    """A citation-less answer renders a 'no sources' note (never a fabricated link)."""
    rendered = render_query_result(_result(citations=[]))
    assert "obsidian://" not in rendered
    assert "no vault sources" in rendered.lower()


def test_render_ask_result_combines_vault_and_web_with_offer() -> None:
    """render_ask_result lists vault + web sources and the offer-to-save line."""
    result = _ask_result()
    rendered = render_ask_result(result)
    assert rendered.startswith("Raft is a consensus algorithm.")
    # Vault citation (unfabricable link + wikilink) and web URL both appear.
    assert "[[exa-search]]" in rendered
    assert "https://example.com/raft" in rendered
    assert "Save this answer" in rendered


def test_render_ask_result_offer_suppressed_when_asked() -> None:
    """offer_save=False omits the save line (used for an empty/edge answer)."""
    rendered = render_ask_result(_ask_result(), offer_save=False)
    assert "Save this answer" not in rendered


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


def test_render_ingest_report_one_to_two_lines_with_links() -> None:
    """render_ingest_report names the filed page and includes link + wikilink."""
    rendered = render_ingest_report(_report())
    assert "concepts/exa-search.md" in rendered
    assert "obsidian://open?vault=pkm-vault&file=concepts%2Fexa.md" in rendered
    assert "[[exa-search]]" in rendered
    assert len(rendered.splitlines()) <= 3  # head + refs (+ optional message)


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


def test_render_ingest_report_uneven_links_and_wikilinks() -> None:
    """Mismatched link/wikilink counts still surface every reference."""
    report = _report(
        obsidian_links=[
            "obsidian://open?vault=pkm-vault&file=a.md",
            "obsidian://open?vault=pkm-vault&file=b.md",
        ],
        wikilinks=["[[a]]"],
    )
    rendered = render_ingest_report(report)
    assert "file=a.md" in rendered
    assert "file=b.md" in rendered
    assert "[[a]]" in rendered


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
