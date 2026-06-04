"""Tests for :mod:`thoth.hindsight`.

Every test isolates the external boundary: no real ``hindsight-api`` server, no
Postgres, no Gemini, and no socket. A :class:`RecordingTransport` fake stands in for
the :class:`httpx.BaseTransport` seam, recording each :class:`httpx.Request` it is
handed and returning a canned :class:`httpx.Response` (a chosen status + JSON, or
raising a transport error), so the tests assert on the exact URL/body the client
builds, on how it classifies the response, and on the bounded retry around the checked
calls -- all without opening a connection.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from thoth.config import Config, load_config
from thoth.hindsight import (
    DEFAULT_BANK,
    DEFAULT_BASE_URL,
    SOURCE_SENTINEL,
    Hindsight,
    HindsightError,
    HindsightTransientError,
    RecallHit,
    parse_recall,
    retain_text,
)

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class RecordingTransport:
    """A recording :class:`httpx.MockTransport` wrapper for tests.

    Wraps an :class:`httpx.MockTransport` around a handler that records every
    :class:`httpx.Request` it sees (so a test can assert on the exact method, URL, and
    JSON body the client built) and returns a canned :class:`httpx.Response`. The
    handler may instead raise to simulate a transport error.

    Attributes:
        handler: Maps a recorded request to its canned response (or raises).
        requests: Every :class:`httpx.Request` seen, in call order.
    """

    handler: Handler
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Build the wrapped :class:`httpx.MockTransport`."""
        self._mock = httpx.MockTransport(self._dispatch)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        """Record the request, then delegate to the test's handler."""
        # Read the body now so ``request.content`` is materialised for later assertions.
        _ = request.content
        self.requests.append(request)
        return self.handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        """The wrapped transport to hand :class:`Hindsight`."""
        return self._mock

    @property
    def last(self) -> httpx.Request:
        """The most recently recorded request."""
        return self.requests[-1]

    @property
    def last_json(self) -> object:
        """The decoded JSON body of the most recently recorded request."""
        return json.loads(self.last.content)


def _ok(payload: object | None = None, *, status: int = 200) -> Handler:
    """A handler that always returns ``status`` with an optional JSON ``payload``."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else {})

    return handler


def _status(code: int, *, body: str = "boom") -> Handler:
    """A handler that always returns ``code`` with a plain-text ``body``."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(code, text=body)

    return handler


@dataclass
class ScriptedHandler:
    """Replay a scripted sequence of outcomes, recording the request count.

    Each entry is either an ``int`` HTTP status (returned with a plain-text body) or an
    :class:`Exception` instance (raised, to simulate a transport error). The last entry
    is reused once the script is exhausted, so a single outcome can repeat indefinitely.

    Attributes:
        script: The outcomes to replay in order (HTTP status or exception to raise).
        json_body: The JSON body returned with a 2xx status outcome.
        count: How many requests have been dispatched.
    """

    script: list[int | Exception]
    json_body: object = field(default_factory=dict)
    count: int = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Replay the next scripted outcome (or raise it)."""
        index = min(self.count, len(self.script) - 1)
        self.count += 1
        outcome = self.script[index]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome < 400:
            return httpx.Response(outcome, json=self.json_body)
        return httpx.Response(outcome, text="boom")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A minimal :class:`Config` (only the required vault path is needed here)."""
    return load_config({"PKM_VAULT": str(tmp_path)})


def _make(
    config: Config,
    handler: Handler,
    *,
    timeout: float = 120.0,
    retries: int = 3,
) -> tuple[Hindsight, RecordingTransport]:
    """Build a :class:`Hindsight` on a recording transport (zero backoff).

    Returns the client and the recording transport so a test can assert on the requests.
    """
    recorder = RecordingTransport(handler)
    hs = Hindsight(
        config,
        transport=recorder.transport,
        timeout=timeout,
        retries=retries,
        retry_wait_initial=0.0,
        retry_wait_max=0.0,
    )
    return hs, recorder


def _bank_prefix(bank: str = DEFAULT_BANK) -> str:
    """The expected ``/v1/default/banks/{bank}`` URL path prefix."""
    return f"/v1/default/banks/{bank}"


# --------------------------------------------------------------------------- #
# Construction / defaults.
# --------------------------------------------------------------------------- #


def test_default_base_url_and_bank(config: Config) -> None:
    """The base URL defaults to the standalone server and the bank to ``thoth``."""
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8888"
    assert DEFAULT_BANK == "thoth"
    hs = Hindsight(config)
    assert hs.base_url == config.hindsight_base_url
    assert hs.bank == DEFAULT_BANK


def test_bank_is_env_overridable(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THOTH_HINDSIGHT_BANK overrides the bank path segment."""
    monkeypatch.setenv("THOTH_HINDSIGHT_BANK", "otherbank")
    hs = Hindsight(config)
    assert hs.bank == "otherbank"


def test_base_url_and_bank_overrides_are_honoured(config: Config) -> None:
    """Per-instance base_url + bank overrides re-point the request URL."""
    hs, recorder = _make(config, _ok({"results": []}))
    hs_override = Hindsight(
        config,
        base_url="http://example.test:9000",
        bank="b1",
        transport=recorder.transport,
        retry_wait_initial=0.0,
        retry_wait_max=0.0,
    )
    assert hs_override.base_url == "http://example.test:9000"
    assert hs_override.bank == "b1"
    hs_override.recall("anything")
    assert recorder.last.url.host == "example.test"
    assert recorder.last.url.path == f"{_bank_prefix('b1')}/memories/recall"


def test_context_manager_closes_client(config: Config) -> None:
    """The client is usable as a context manager and closes on exit."""
    recorder = RecordingTransport(_ok({"results": []}))
    with Hindsight(config, transport=recorder.transport) as hs:
        assert hs.recall("q") == []


# --------------------------------------------------------------------------- #
# parse_recall / retain_text (pure helpers operating on the parsed dict).
# --------------------------------------------------------------------------- #


def test_retain_text_prefixes_exactly_one_source_line() -> None:
    """retain_text emits one 'SOURCE: <path>' line, a blank line, then the facts."""
    blob = retain_text("entities/foo.md", "Foo is a thing.\nMore detail.")
    lines = blob.split("\n")
    assert lines[0] == f"{SOURCE_SENTINEL} entities/foo.md"
    assert lines[1] == ""
    assert lines[2] == "Foo is a thing."
    assert blob.count(SOURCE_SENTINEL) == 1


def test_parse_recall_recovers_path_from_document_id() -> None:
    """The PRIMARY channel: the hit's echoed ``document_id`` yields the vault path."""
    payload: dict[str, object] = {
        "results": [
            {"text": "Foo is a coordinator.", "document_id": "entities/foo.md"},
            {"text": "Bar relates to CAP.", "document_id": "concepts/bar.md"},
        ]
    }
    hits = parse_recall(payload)
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]
    assert all(isinstance(h, RecallHit) for h in hits)


def test_parse_recall_recovers_path_from_chunk_map() -> None:
    """Channel 2: the hit's ``chunk_id`` resolves through the top-level ``chunks{}``."""
    payload: dict[str, object] = {
        "results": [{"text": "atomic fact", "chunk_id": "c1"}],
        "chunks": {"c1": {"document_id": "entities/foo.md"}},
    }
    assert [h.path for h in parse_recall(payload)] == ["entities/foo.md"]


def test_parse_recall_chunk_map_falls_back_to_context() -> None:
    """The chunk entry's ``context`` resolves the path absent a ``document_id``."""
    payload: dict[str, object] = {
        "results": [{"text": "fact", "chunk_id": "c1"}],
        "chunks": {"c1": {"context": "concepts/bar.md"}},
    }
    assert [h.path for h in parse_recall(payload)] == ["concepts/bar.md"]


def test_parse_recall_recovers_path_from_context() -> None:
    """Channel 3: the hit's echoed ``context`` yields the vault path."""
    payload: dict[str, object] = {
        "results": [{"text": "fact", "context": "memories/wifi.md"}],
    }
    assert [h.path for h in parse_recall(payload)] == ["memories/wifi.md"]


def test_parse_recall_falls_back_to_sentinel_in_text() -> None:
    """Channel 4: the ``SOURCE:`` line surviving in the hit text recovers the path."""
    payload: dict[str, object] = {
        "results": [
            {"text": "SOURCE: entities/foo.md\n\nFoo is a thing."},
            {"text": "no channel at all -> dropped"},
        ]
    }
    assert [h.path for h in parse_recall(payload)] == ["entities/foo.md"]


def test_parse_recall_channel_preference_order() -> None:
    """``document_id`` wins over chunk-map, context, and the sentinel on one hit."""
    payload: dict[str, object] = {
        "results": [
            {
                "text": "SOURCE: from/text.md\n\nfact",
                "document_id": "from/docid.md",
                "context": "from/context.md",
                "chunk_id": "c1",
            }
        ],
        "chunks": {"c1": {"document_id": "from/chunk.md"}},
    }
    assert [h.path for h in parse_recall(payload)] == ["from/docid.md"]


def test_parse_recall_recovers_page_type_from_hit_tags() -> None:
    """The page type round-trips on the hit's ``document_tags`` (first token wins)."""
    payload: dict[str, object] = {
        "results": [
            {"document_id": "entities/foo.md", "document_tags": ["entity"]},
            {"document_id": "memories/wifi.md", "document_tags": ["memory"]},
        ]
    }
    hits = parse_recall(payload)
    assert [(h.path, h.page_type) for h in hits] == [
        ("entities/foo.md", "entity"),
        ("memories/wifi.md", "memory"),
    ]


def test_parse_recall_recovers_page_type_from_chunk_entry() -> None:
    """The page type falls back to the hit's ``chunks{}`` entry ``document_tags``."""
    payload: dict[str, object] = {
        "results": [{"chunk_id": "c1"}],
        "chunks": {
            "c1": {"document_id": "entities/foo.md", "document_tags": ["entity"]}
        },
    }
    hits = parse_recall(payload)
    assert [(h.path, h.page_type) for h in hits] == [("entities/foo.md", "entity")]


def test_parse_recall_page_type_empty_when_no_tags() -> None:
    """A hit with no ``document_tags`` anywhere leaves ``page_type`` empty."""
    payload: dict[str, object] = {"results": [{"document_id": "entities/foo.md"}]}
    assert parse_recall(payload)[0].page_type == ""


def test_parse_recall_dedupes_preserving_first_seen_order() -> None:
    """Duplicate paths collapse to the first occurrence, preserving order."""
    payload: dict[str, object] = {
        "results": [
            {"document_id": "entities/foo.md", "text": "a"},
            {"document_id": "concepts/bar.md", "text": "b"},
            {"text": "SOURCE: entities/foo.md\n\nrepeat"},  # duplicate via sentinel
        ]
    }
    assert [h.path for h in parse_recall(payload)] == [
        "entities/foo.md",
        "concepts/bar.md",
    ]


def test_parse_recall_accepts_hits_alias() -> None:
    """A ``hits`` list is honoured as an alias for ``results``."""
    payload: dict[str, object] = {
        "hits": [{"document_id": "entities/foo.md"}],
    }
    assert [h.path for h in parse_recall(payload)] == ["entities/foo.md"]


def test_parse_recall_empty_payload_returns_empty_list() -> None:
    """An empty payload (or an envelope with no hits) yields [] without raising."""
    assert parse_recall({}) == []
    assert parse_recall({"results": []}) == []


def test_parse_recall_skips_unrecoverable_and_malformed_hits() -> None:
    """Non-dict records and hits with no recoverable path are skipped."""
    payload: dict[str, object] = {
        "results": [
            "not a dict",
            {"text": "no provenance"},
            {"document_id": "entities/foo.md"},
        ]
    }
    assert [h.path for h in parse_recall(payload)] == ["entities/foo.md"]


# --------------------------------------------------------------------------- #
# retain()
# --------------------------------------------------------------------------- #


def test_retain_posts_to_memories_with_expected_body(config: Config) -> None:
    """retain POSTs the item to ``.../memories`` with content/document_id/context."""
    hs, recorder = _make(config, _ok())
    hs.retain("entities/foo.md", "Foo facts.", tags=["entity"])

    assert recorder.last.method == "POST"
    assert recorder.last.url.path == f"{_bank_prefix()}/memories"
    body = recorder.last_json
    assert isinstance(body, dict)
    assert body["async"] is False
    items = body["items"]
    assert isinstance(items, list)
    item = items[0]
    assert item["document_id"] == "entities/foo.md"
    assert item["context"] == "entities/foo.md"
    assert item["content"].startswith(f"{SOURCE_SENTINEL} entities/foo.md")
    assert "Foo facts." in item["content"]
    assert item["document_tags"] == ["entity"]


def test_retain_document_tags_excludes_rel_path(config: Config) -> None:
    """The vault path is never put into ``document_tags`` (page-type axis only)."""
    hs, recorder = _make(config, _ok())
    hs.retain("entities/foo.md", "facts", tags=["entity", "entities/foo.md"])
    item = recorder.last_json["items"][0]  # type: ignore[index]
    assert item["document_tags"] == ["entity"]


def test_retain_omits_document_tags_when_empty(config: Config) -> None:
    """No ``document_tags`` key is sent when only the rel path / blanks were passed."""
    hs, recorder = _make(config, _ok())
    hs.retain("concepts/bar.md", "facts", tags=["", "concepts/bar.md"])
    item = recorder.last_json["items"][0]  # type: ignore[index]
    assert "document_tags" not in item

    hs.retain("concepts/baz.md", "facts")
    item = recorder.last_json["items"][0]  # type: ignore[index]
    assert "document_tags" not in item


def test_retain_raises_on_permanent_4xx_without_retry(config: Config) -> None:
    """A 4xx (permanent) retain failure raises HindsightError and is NOT retried."""
    handler = ScriptedHandler(script=[400])
    recorder = RecordingTransport(handler)
    hs = Hindsight(
        config,
        transport=recorder.transport,
        retries=3,
        retry_wait_initial=0.0,
        retry_wait_max=0.0,
    )
    with pytest.raises(HindsightError) as exc_info:
        hs.retain("entities/foo.md", "facts")
    msg = str(exc_info.value)
    assert "retain" in msg
    assert "entities/foo.md" in msg
    assert not isinstance(exc_info.value, HindsightTransientError)
    assert handler.count == 1


# --------------------------------------------------------------------------- #
# recall()
# --------------------------------------------------------------------------- #


def test_recall_posts_query_and_parses_document_id_paths(config: Config) -> None:
    """recall POSTs ``{"query": ...}`` (no tags filter) and maps hit paths."""
    payload = {
        "results": [
            {
                "text": "fact",
                "document_id": "entities/foo.md",
                "document_tags": ["entity"],
            },
            {
                "text": "fact2",
                "document_id": "concepts/bar.md",
                "document_tags": ["concept"],
            },
        ]
    }
    hs, recorder = _make(config, _ok(payload))
    hits = hs.recall("how does foo work", limit=3)

    assert recorder.last.method == "POST"
    assert recorder.last.url.path == f"{_bank_prefix()}/memories/recall"
    body = recorder.last_json
    assert body == {"query": "how does foo work"}
    assert [h.path for h in hits] == ["entities/foo.md", "concepts/bar.md"]


def test_recall_caps_results_client_side_to_limit(config: Config) -> None:
    """recall truncates the parsed hits to ``limit`` client-side."""
    payload = {
        "results": [
            {"text": "a", "document_id": "entities/a.md"},
            {"text": "b", "document_id": "concepts/b.md"},
            {"text": "c", "document_id": "entities/c.md"},
        ]
    }
    hs, _ = _make(config, _ok(payload))
    hits = hs.recall("everything", limit=2)
    assert [h.path for h in hits] == ["entities/a.md", "concepts/b.md"]


def test_recall_scopes_by_page_type_when_types_given(config: Config) -> None:
    """``types`` keeps only hits whose page_type tag is in the set (ADR 0004, #40)."""
    payload = {
        "results": [
            {"text": "a", "document_id": "entities/a.md", "document_tags": ["entity"]},
            {"text": "m", "document_id": "memories/m.md", "document_tags": ["memory"]},
            {"text": "c", "document_id": "concepts/c.md", "document_tags": ["concept"]},
        ]
    }
    hs, _ = _make(config, _ok(payload))

    knowledge = hs.recall("q", types=frozenset({"entity", "concept"}))
    assert [h.path for h in knowledge] == ["entities/a.md", "concepts/c.md"]

    memories = hs.recall("q", types=frozenset({"memory"}))
    assert [h.path for h in memories] == ["memories/m.md"]

    assert [h.path for h in hs.recall("q")] == [
        "entities/a.md",
        "memories/m.md",
        "concepts/c.md",
    ]


def test_recall_filters_by_type_before_the_limit_cap(config: Config) -> None:
    """The type scope is applied before truncation, so the cap counts kept hits only."""
    payload = {
        "results": [
            {
                "text": "m1",
                "document_id": "memories/m1.md",
                "document_tags": ["memory"],
            },
            {"text": "e", "document_id": "entities/e.md", "document_tags": ["entity"]},
            {
                "text": "m2",
                "document_id": "memories/m2.md",
                "document_tags": ["memory"],
            },
        ]
    }
    hs, _ = _make(config, _ok(payload))
    hits = hs.recall("q", limit=2, types=frozenset({"memory"}))
    assert [h.path for h in hits] == ["memories/m1.md", "memories/m2.md"]


def test_recall_empty_results_returns_empty_and_does_not_raise(config: Config) -> None:
    """A 2xx with an empty result set yields [] (no results is normal)."""
    hs, _ = _make(config, _ok({"results": []}))
    assert hs.recall("nothing matches") == []


def test_recall_undecodable_body_returns_empty(config: Config) -> None:
    """A 2xx with a non-JSON body yields [] rather than raising."""
    hs, _ = _make(config, _status(200, body="not json at all"))
    assert hs.recall("q") == []


def test_recall_raises_on_permanent_4xx(config: Config) -> None:
    """A 4xx recall failure raises HindsightError, fail-fast."""
    handler = ScriptedHandler(script=[400])
    recorder = RecordingTransport(handler)
    hs = Hindsight(
        config,
        transport=recorder.transport,
        retry_wait_initial=0.0,
        retry_wait_max=0.0,
    )
    with pytest.raises(HindsightError) as exc_info:
        hs.recall("q")
    assert "recall" in str(exc_info.value)
    assert handler.count == 1


# --------------------------------------------------------------------------- #
# Bounded retry around the checked calls (#11).
# --------------------------------------------------------------------------- #


def _scripted(
    config: Config,
    script: list[int | Exception],
    *,
    json_body: object | None = None,
    retries: int = 3,
) -> tuple[Hindsight, ScriptedHandler]:
    """Build a :class:`Hindsight` on a :class:`ScriptedHandler` (zero backoff)."""
    handler = ScriptedHandler(
        script=script, json_body={} if json_body is None else json_body
    )
    recorder = RecordingTransport(handler)
    hs = Hindsight(
        config,
        transport=recorder.transport,
        retries=retries,
        retry_wait_initial=0.0,
        retry_wait_max=0.0,
    )
    return hs, handler


def test_retain_retries_5xx_then_succeeds(config: Config) -> None:
    """A transient 5xx is retried and the eventual 2xx is accepted."""
    hs, handler = _scripted(config, [500, 503, 200])
    hs.retain("entities/foo.md", "facts")  # must not raise
    assert handler.count == 3


def test_recall_retries_5xx_then_parses_success(config: Config) -> None:
    """recall retries a 5xx and parses the successful attempt's body."""
    hs, handler = _scripted(
        config,
        [500, 200],
        json_body={"results": [{"document_id": "entities/foo.md"}]},
    )
    hits = hs.recall("q")
    assert [h.path for h in hits] == ["entities/foo.md"]
    assert handler.count == 2


def test_retain_retries_connect_error_then_succeeds(config: Config) -> None:
    """An httpx.ConnectError (server socket not up) is transient and retried."""
    hs, handler = _scripted(
        config, [httpx.ConnectError("no route"), httpx.ConnectError("nope"), 200]
    )
    hs.retain("entities/foo.md", "facts")
    assert handler.count == 3


def test_retain_retries_timeout_then_succeeds(config: Config) -> None:
    """An httpx timeout is a transport error: transient and retried."""
    hs, handler = _scripted(config, [httpx.ReadTimeout("slow"), 200])
    hs.retain("entities/foo.md", "facts")
    assert handler.count == 2


def test_retain_exhausts_retries_then_raises_transient_error(config: Config) -> None:
    """A persistent 5xx raises HindsightTransientError after exactly ``retries``."""
    hs, handler = _scripted(config, [500], retries=3)
    with pytest.raises(HindsightTransientError):
        hs.retain("entities/foo.md", "facts")
    assert handler.count == 3


def test_connect_error_exhausts_retries_as_transient(config: Config) -> None:
    """A persistent transport error raises HindsightTransientError after retries."""
    hs, handler = _scripted(config, [httpx.ConnectError("down")], retries=3)
    with pytest.raises(HindsightTransientError):
        hs.retain("entities/foo.md", "facts")
    assert handler.count == 3


def test_permanent_4xx_fails_fast_without_retry(config: Config) -> None:
    """A 4xx raises immediately, with no second request even when a retry would win."""
    hs, handler = _scripted(config, [400, 200], retries=5)
    with pytest.raises(HindsightError) as exc_info:
        hs.retain("entities/foo.md", "facts")
    assert not isinstance(exc_info.value, HindsightTransientError)
    assert handler.count == 1


def test_retry_count_is_configurable_at_construction(config: Config) -> None:
    """The attempt count is configurable; retries=1 disables retry entirely."""
    hs, handler = _scripted(config, [500], retries=1)
    with pytest.raises(HindsightTransientError):
        hs.retain("entities/foo.md", "facts")
    assert handler.count == 1


def test_transient_error_is_a_hindsight_error_subclass() -> None:
    """HindsightTransientError subclasses HindsightError so handlers still catch it."""
    assert issubclass(HindsightTransientError, HindsightError)


# --------------------------------------------------------------------------- #
# reset_bank()  (DELETE the bank, checked).
# --------------------------------------------------------------------------- #


def test_reset_bank_deletes_the_bank_url(config: Config) -> None:
    """reset_bank issues a DELETE to the bank segment itself."""
    hs, recorder = _make(config, _ok())
    hs.reset_bank()
    assert recorder.last.method == "DELETE"
    assert recorder.last.url.path == _bank_prefix()


def test_reset_bank_raises_on_4xx(config: Config) -> None:
    """A non-2xx reset_bank raises HindsightError (4xx fails fast)."""
    hs, handler = _scripted(config, [404])
    with pytest.raises(HindsightError) as exc_info:
        hs.reset_bank()
    assert "reset_bank" in str(exc_info.value)
    assert handler.count == 1


def test_reset_bank_retries_5xx_then_raises_transient(config: Config) -> None:
    """A persistent 5xx reset_bank retries then raises HindsightTransientError."""
    hs, handler = _scripted(config, [500], retries=3)
    with pytest.raises(HindsightTransientError):
        hs.reset_bank()
    assert handler.count == 3


# --------------------------------------------------------------------------- #
# forget()  (best-effort DELETE, NO retry, swallows everything).
# --------------------------------------------------------------------------- #


def test_forget_targets_the_document_url(config: Config) -> None:
    """forget DELETEs ``.../documents/{rel_path}`` keeping the path separators."""
    hs, recorder = _make(config, _ok())
    hs.forget("entities/foo.md")
    assert recorder.last.method == "DELETE"
    assert recorder.last.url.path == f"{_bank_prefix()}/documents/entities/foo.md"


def test_forget_swallows_non_2xx_and_does_not_retry(config: Config) -> None:
    """forget swallows a 5xx (would-be-transient for a checked call) with no retry."""
    hs, handler = _scripted(config, [500])
    hs.forget("entities/missing.md")  # must not raise
    assert handler.count == 1


def test_forget_swallows_4xx(config: Config) -> None:
    """forget swallows a 4xx too (best-effort delete)."""
    hs, handler = _scripted(config, [404])
    hs.forget("entities/missing.md")  # must not raise
    assert handler.count == 1


def test_forget_swallows_transport_errors(config: Config) -> None:
    """forget swallows a transport error (server socket not up) too."""
    hs, handler = _scripted(config, [httpx.ConnectError("down")])
    hs.forget("entities/missing.md")  # must not raise
    assert handler.count == 1


# --------------------------------------------------------------------------- #
# probe()  ("did it land?")
# --------------------------------------------------------------------------- #


def test_probe_true_when_path_among_hits(config: Config) -> None:
    """probe returns True when the recalled hits include the path."""
    payload = {
        "results": [
            {"text": "fact", "document_id": "entities/foo.md"},
            {"text": "fact2", "document_id": "concepts/bar.md"},
        ]
    }
    hs, _ = _make(config, _ok(payload))
    assert hs.probe("concepts/bar.md", "anything") is True


def test_probe_false_when_path_absent(config: Config) -> None:
    """probe returns False when the path is not among the recalled hits."""
    payload = {"results": [{"text": "fact", "document_id": "entities/foo.md"}]}
    hs, _ = _make(config, _ok(payload))
    assert hs.probe("entities/missing.md", "anything") is False


def test_probe_false_on_empty_recall(config: Config) -> None:
    """probe on an empty recall result is False (and issues exactly one request)."""
    handler = ScriptedHandler(script=[200], json_body={"results": []})
    recorder = RecordingTransport(handler)
    hs = Hindsight(config, transport=recorder.transport)
    assert hs.probe("entities/foo.md", "q") is False
    assert len(recorder.requests) == 1


# --------------------------------------------------------------------------- #
# Import safety (no hindsight package pulled in at module import).
# --------------------------------------------------------------------------- #


def test_module_import_pulls_in_no_hindsight_package() -> None:
    """Importing thoth.hindsight must not import any 'hindsight' Python package.

    The client is pure httpx; only stdlib, httpx, tenacity, and thoth.config may appear
    at top level. A stray ``import hindsight`` would break collection in CI where the
    package is absent.
    """
    import sys

    import thoth.hindsight  # noqa: F401  (already imported; asserts on sys.modules)

    leaked = [
        name
        for name in sys.modules
        if name == "hindsight" or name.startswith("hindsight.")
    ]
    assert leaked == []
