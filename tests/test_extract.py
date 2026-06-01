"""Tests for :mod:`thoth.extract`.

Every external boundary is isolated: DNS resolution is replaced by monkeypatching
the :func:`thoth.extract._resolve_ips` seam (no real lookups), ``httpx`` is driven by
:class:`httpx.MockTransport` (no real network), Exa/Firecrawl are injected fakes, and
``whisper`` is exercised by monkeypatching :func:`subprocess.run`. The SSRF guard is
proven to run *before* any client/socket is touched. The ``exa_py``/``firecrawl``/
``whisper`` packages are never imported (an import-safety test asserts this).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from thoth.config import Config, load_config
from thoth.extract import (
    MAX_DOWNLOAD_BYTES,
    ExtractedDoc,
    ExtractError,
    Extractor,
    FetchedBinary,
    FetchError,
    SsrfError,
    TranscriptionError,
    WebHit,
    assert_url_allowed,
    is_url_allowed,
)


@pytest.fixture
def config() -> Config:
    """A frozen Config with fake Exa/Firecrawl keys (no disk, no network)."""
    return load_config(
        {
            "PKM_VAULT": "/x",
            "EXA_API_KEY": "test-token",
            "FIRECRAWL_API_KEY": "test-token",
        }
    )


def _force_resolver(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]
) -> None:
    """Replace the DNS seam so a host resolves to a chosen list of IPs (no real DNS)."""

    def fake_resolve(host: str) -> list[str]:
        if host not in mapping:
            raise SsrfError(f"cannot resolve host {host!r}: forced miss")
        return mapping[host]

    monkeypatch.setattr("thoth.extract._resolve_ips", fake_resolve)


# --- injectable fakes -------------------------------------------------------- #


class _ExaResponse:
    """A minimal stand-in for an Exa response object exposing ``.results``."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results


class _FakeExa:
    """Records the query and returns canned results for :meth:`search`."""

    def __init__(self, results: list[Any]) -> None:
        self.results_payload = results
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, num_results: int = 5) -> Any:
        """Record the call and return an object exposing ``.results`` (exa_py 2.x)."""
        self.calls.append((query, num_results))
        return _ExaResponse(self.results_payload)


class _FakeFirecrawl:
    """Records scraped URLs and returns a canned markdown result (or raises)."""

    def __init__(
        self, result: Any | None = None, *, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.urls: list[str] = []
        self.formats: list[list[str] | None] = []

    def scrape(self, url: str, *, formats: list[str] | None = None) -> Any:
        """Record URL/formats, return the canned result or raise (firecrawl 4.x)."""
        self.urls.append(url)
        self.formats.append(formats)
        if self.error is not None:
            raise self.error
        return self.result


def _mock_client(handler: Any) -> httpx.Client:
    """Build an ``httpx.Client`` backed by a MockTransport (zero real network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# is_url_allowed / assert_url_allowed — scheme rejection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "data:text/plain;base64,QQ==",
        "gopher://example.com/1",
        "",
        "example.com/no-scheme",
    ],
)
def test_assert_url_allowed_rejects_non_http_schemes(url: str) -> None:
    """Non-http(s) schemes (and a schemeless string) raise SsrfError, no DNS needed."""
    assert is_url_allowed(url) is False
    with pytest.raises(SsrfError):
        assert_url_allowed(url)


# --------------------------------------------------------------------------- #
# is_url_allowed / assert_url_allowed — private/loopback/etc IP rejection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (cloud metadata)
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 unique-local (private)
        "0.0.0.0",  # unspecified
    ],
)
def test_assert_url_allowed_rejects_private_ips(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    """A host resolving to any non-public IP is blocked before any fetch."""
    _force_resolver(monkeypatch, {"evil.example": [ip]})
    url = "https://evil.example/path"
    assert is_url_allowed(url) is False
    with pytest.raises(SsrfError):
        assert_url_allowed(url)


def test_is_url_allowed_true_for_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host resolving only to a public IP passes the guard."""
    _force_resolver(monkeypatch, {"good.example": ["8.8.8.8"]})
    assert is_url_allowed("https://good.example/x") is True
    assert_url_allowed("https://good.example/x")  # does not raise


def test_allow_private_urls_bypasses_ip_check_but_keeps_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allow_private_urls=True skips the IP check yet still requires http(s)."""

    # The resolver must never be consulted when private URLs are allowed.
    def boom(host: str) -> list[str]:
        raise AssertionError("resolver must not be called when private is allowed")

    monkeypatch.setattr("thoth.extract._resolve_ips", boom)
    assert is_url_allowed("https://127.0.0.1/x", allow_private_urls=True) is True
    # Scheme requirement still applies even with private allowed.
    assert is_url_allowed("file:///etc/passwd", allow_private_urls=True) is False


def test_is_url_allowed_false_when_any_resolved_ip_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mix of one public and one private resolved IP fails (all must be public)."""
    _force_resolver(monkeypatch, {"mixed.example": ["8.8.8.8", "10.0.0.1"]})
    assert is_url_allowed("https://mixed.example") is False


def test_is_url_allowed_false_when_resolution_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that resolves to nothing is not allowed."""
    monkeypatch.setattr("thoth.extract._resolve_ips", lambda host: [])
    assert is_url_allowed("https://nothing.example") is False


def test_resolve_ips_raises_ssrf_on_gaierror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real resolver seam maps a DNS failure to SsrfError."""

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SsrfError):
        is_url_allowed("https://does-not-resolve.example")


# --------------------------------------------------------------------------- #
# web_search
# --------------------------------------------------------------------------- #


def test_web_search_maps_results_to_webhits(config: Config) -> None:
    """Exa results (attribute objects) map into WebHit url/title/snippet."""

    class _Item:
        def __init__(self, url: str, title: str, text: str) -> None:
            self.url = url
            self.title = title
            self.text = text

    fake = _FakeExa(
        [
            _Item("https://a.example", "Alpha", "first snippet"),
            _Item("https://b.example", "Beta", "second snippet"),
        ]
    )
    extractor = Extractor(config, exa=fake)

    hits = extractor.web_search("controllers", num_results=2)

    assert hits == [
        WebHit("https://a.example", "Alpha", "first snippet"),
        WebHit("https://b.example", "Beta", "second snippet"),
    ]
    assert fake.calls == [("controllers", 2)]


def test_web_search_tolerates_dict_results(config: Config) -> None:
    """Exa results given as dicts are mapped just as well as objects."""
    fake = _FakeExa([{"url": "https://c.example", "title": "Gamma"}])
    extractor = Extractor(config, exa=fake)
    hits = extractor.web_search("q")
    assert hits == [WebHit("https://c.example", "Gamma", "")]


def test_web_search_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no EXA_API_KEY and no injected client, accessing exa raises ExtractError."""
    cfg = load_config({"PKM_VAULT": "/x"})  # no EXA_API_KEY
    extractor = Extractor(cfg)
    with pytest.raises(ExtractError):
        extractor.web_search("q")


# --------------------------------------------------------------------------- #
# web_extract
# --------------------------------------------------------------------------- #


def test_web_extract_returns_doc(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public URL is extracted into an ExtractedDoc with markdown/title/source."""
    _force_resolver(monkeypatch, {"pub.example": ["8.8.8.8"]})
    fake = _FakeFirecrawl(
        {"markdown": "# Hello\n\nbody", "metadata": {"title": "Hello Page"}}
    )
    extractor = Extractor(config, firecrawl=fake)

    doc = extractor.web_extract("https://pub.example/article")

    assert isinstance(doc, ExtractedDoc)
    assert doc.source_url == "https://pub.example/article"
    assert doc.title == "Hello Page"
    assert doc.markdown == "# Hello\n\nbody"
    assert fake.urls == ["https://pub.example/article"]
    assert fake.formats == [["markdown"]]


def test_web_extract_reads_document_object_metadata(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A firecrawl 4.x ``Document``: object ``.markdown`` + object ``.metadata``."""
    _force_resolver(monkeypatch, {"pub.example": ["8.8.8.8"]})

    class _Metadata:
        title = "Doc Title"

    class _Document:
        markdown = "# Doc\n\nbody"
        metadata = _Metadata()

    extractor = Extractor(config, firecrawl=_FakeFirecrawl(_Document()))

    doc = extractor.web_extract("https://pub.example/article")

    assert doc.title == "Doc Title"
    assert doc.markdown == "# Doc\n\nbody"


def test_web_extract_runs_ssrf_guard_before_firecrawl(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private-IP URL raises SsrfError and never reaches the firecrawl fake."""
    _force_resolver(monkeypatch, {"intranet.example": ["10.1.2.3"]})
    fake = _FakeFirecrawl({"markdown": "should not be returned"})
    extractor = Extractor(config, firecrawl=fake)

    with pytest.raises(SsrfError):
        extractor.web_extract("https://intranet.example/secret")

    assert fake.urls == []  # the guard fired before the client was touched


def test_web_extract_raises_when_no_markdown(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firecrawl returning no markdown is an ExtractError."""
    _force_resolver(monkeypatch, {"pub.example": ["8.8.8.8"]})
    extractor = Extractor(config, firecrawl=_FakeFirecrawl({"metadata": {}}))
    with pytest.raises(ExtractError):
        extractor.web_extract("https://pub.example/x")


def test_web_extract_normalises_client_error(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising firecrawl client surfaces as ExtractError (not the raw exception)."""
    _force_resolver(monkeypatch, {"pub.example": ["8.8.8.8"]})
    extractor = Extractor(config, firecrawl=_FakeFirecrawl(error=RuntimeError("boom")))
    with pytest.raises(ExtractError):
        extractor.web_extract("https://pub.example/x")


# --------------------------------------------------------------------------- #
# fetch_binary
# --------------------------------------------------------------------------- #


def test_fetch_binary_streams_and_derives_ext(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch_binary streams to a temp file and derives the ext from Content-Type."""
    _force_resolver(monkeypatch, {"img.example": ["8.8.8.8"]})
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://img.example/p.png"
        return httpx.Response(
            200, content=payload, headers={"content-type": "image/png"}
        )

    extractor = Extractor(config, http_client=_mock_client(handler))

    fetched = extractor.fetch_binary("https://img.example/p.png")

    assert isinstance(fetched, FetchedBinary)
    assert fetched.source_url == "https://img.example/p.png"
    assert fetched.content_type == "image/png"
    assert fetched.suggested_ext == "png"
    try:
        assert fetched.tmp_path.read_bytes() == payload
    finally:
        fetched.tmp_path.unlink(missing_ok=True)


def test_fetch_binary_pdf_content_type(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF Content-Type yields the 'pdf' suggested extension."""
    _force_resolver(monkeypatch, {"paper.example": ["8.8.8.8"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4 ...",
            headers={"content-type": "application/pdf; charset=binary"},
        )

    extractor = Extractor(config, http_client=_mock_client(handler))
    fetched = extractor.fetch_binary("https://paper.example/a.pdf")
    try:
        assert fetched.content_type == "application/pdf"
        assert fetched.suggested_ext == "pdf"
    finally:
        fetched.tmp_path.unlink(missing_ok=True)


def test_fetch_binary_unknown_content_type_falls_back(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown Content-Type falls back to the 'bin' extension."""
    _force_resolver(monkeypatch, {"x.example": ["8.8.8.8"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"data", headers={"content-type": "application/x-weird"}
        )

    extractor = Extractor(config, http_client=_mock_client(handler))
    fetched = extractor.fetch_binary("https://x.example/x")
    try:
        assert fetched.suggested_ext == "bin"
    finally:
        fetched.tmp_path.unlink(missing_ok=True)


def test_fetch_binary_size_cap_raises_and_cleans_up(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body over the cap raises FetchError and removes the partial temp file."""
    _force_resolver(monkeypatch, {"big.example": ["8.8.8.8"]})
    monkeypatch.setattr("thoth.extract.MAX_DOWNLOAD_BYTES", 16)
    monkeypatch.setattr("thoth.extract._STREAM_CHUNK_BYTES", 4)

    captured: dict[str, Path] = {}
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["path"] = Path(name)
        return fd, name

    monkeypatch.setattr("thoth.extract.tempfile.mkstemp", tracking_mkstemp)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"y" * 64, headers={"content-type": "image/png"}
        )

    extractor = Extractor(config, http_client=_mock_client(handler))

    with pytest.raises(FetchError):
        extractor.fetch_binary("https://big.example/huge.png")

    # The partial temp file was cleaned up on the size-cap path.
    assert "path" in captured
    assert not captured["path"].exists()


def test_fetch_binary_http_error_status_raises(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4xx/5xx response raises FetchError and leaves no temp file behind."""
    _force_resolver(monkeypatch, {"missing.example": ["8.8.8.8"]})

    captured: dict[str, Path] = {}
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["path"] = Path(name)
        return fd, name

    monkeypatch.setattr("thoth.extract.tempfile.mkstemp", tracking_mkstemp)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    extractor = Extractor(config, http_client=_mock_client(handler))
    with pytest.raises(FetchError):
        extractor.fetch_binary("https://missing.example/x")
    assert not captured["path"].exists()


def test_fetch_binary_runs_ssrf_guard_before_httpx(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private-IP URL raises SsrfError before any httpx request is issued."""
    _force_resolver(monkeypatch, {"local.example": ["127.0.0.1"]})

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("httpx must not be called for a blocked URL")

    extractor = Extractor(config, http_client=_mock_client(handler))
    with pytest.raises(SsrfError):
        extractor.fetch_binary("https://local.example/x")


def test_max_download_bytes_constant() -> None:
    """The documented 50 MiB cap is pinned (callers rely on the value)."""
    assert MAX_DOWNLOAD_BYTES == 50 * 1024 * 1024


# --------------------------------------------------------------------------- #
# transcribe (whisper via subprocess)
# --------------------------------------------------------------------------- #


def test_transcribe_reads_output_file(
    config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """transcribe shells whisper (mocked) and returns the written ``.txt`` file.

    The real CLI writes ``<output_dir>/<stem>.txt``; the fake honours the
    ``--output_dir`` argv passed by :meth:`transcribe` rather than relying on stdout
    (which is the verbose timestamped dump, not the clean transcript).
    """
    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"fake-audio")
    recorded: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded["argv"] = argv
        out_dir = Path(argv[argv.index("--output_dir") + 1])
        (out_dir / f"{audio.stem}.txt").write_text("hello world\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="[00:00] noise\n", stderr="")

    monkeypatch.setattr("thoth.extract.subprocess.run", fake_run)
    extractor = Extractor(config, whisper_bin="whisper")

    text = extractor.transcribe(audio, model="small")

    assert text == "hello world"
    assert recorded["argv"][0] == "whisper"
    assert str(audio) in recorded["argv"]
    assert "--model" in recorded["argv"]
    assert "small" in recorded["argv"]
    assert "--output_dir" in recorded["argv"]


def test_transcribe_no_output_file_raises(
    config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 0-exit that writes no transcript file raises (the silent-skip case).

    Whisper catches a write/decode error, logs ``Skipping ...`` and exits 0; without a
    produced ``.txt`` we must surface a failure rather than return an empty transcript.
    """
    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"x")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="Skipping memo.wav due to OSError"
        )

    monkeypatch.setattr("thoth.extract.subprocess.run", fake_run)
    extractor = Extractor(config, whisper_bin="whisper")
    with pytest.raises(TranscriptionError) as exc_info:
        extractor.transcribe(audio)
    assert "no transcript" in str(exc_info.value)


def test_transcribe_nonzero_exit_raises(
    config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-zero whisper exit raises TranscriptionError with stderr surfaced."""
    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"x")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad codec")

    monkeypatch.setattr("thoth.extract.subprocess.run", fake_run)
    extractor = Extractor(config)
    with pytest.raises(TranscriptionError) as exc_info:
        extractor.transcribe(audio)
    assert "bad codec" in str(exc_info.value)


def test_transcribe_missing_binary_raises(
    config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing whisper binary (FileNotFoundError) raises TranscriptionError."""
    audio = tmp_path / "memo.wav"
    audio.write_bytes(b"x")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("no whisper here")

    monkeypatch.setattr("thoth.extract.subprocess.run", fake_run)
    extractor = Extractor(config, whisper_bin="whisper")
    with pytest.raises(TranscriptionError):
        extractor.transcribe(audio)


# --------------------------------------------------------------------------- #
# import safety
# --------------------------------------------------------------------------- #


def test_module_import_pulls_no_heavy_deps() -> None:
    """Importing thoth.extract must not import exa_py/firecrawl/whisper.

    These packages are absent in CI; the module must only need stdlib + httpx +
    thoth.config at import time (lazy/subprocess for everything else).
    """
    import thoth.extract  # noqa: F401 - ensure it is imported

    for forbidden in ("exa_py", "firecrawl", "whisper"):
        assert forbidden not in sys.modules
