"""URL/PDF/image/audio extraction with an SSRF guard and injectable boundaries.

This module is the appliance's read-only window onto the outside world (SPEC
sections 6 and 7.1). It turns a URL into clean markdown (Exa for semantic
discovery, Firecrawl for extraction), fetches a binary (a PDF or an image) to a
temporary file *server-side* for :meth:`thoth.vault.Vault.save_asset` (never
base64), and shells out to a local ``whisper`` CLI for optional speech-to-text.
The same side-effect-free :meth:`Extractor.web_search` / :meth:`Extractor.web_extract`
surface is reused by the Phase 3 ``pkm_ask`` research path.

Every network entry point passes through a single SSRF gate
(:func:`assert_url_allowed`) **before** any client or socket is touched: the URL
scheme must be ``http``/``https`` and every resolved IP must be public unless
``allow_private_urls`` is set. This blocks ``file://``/``data:``/``gopher://``
schemes and loopback/private/link-local/reserved/multicast/unspecified targets
(for example the ``169.254.169.254`` cloud-metadata address).

Import safety (the pytest-collection trap): only the standard library, ``httpx``,
and :mod:`thoth.config` are imported at module top level. ``exa_py`` and the
Firecrawl client are imported **lazily** inside :attr:`Extractor.exa` /
:attr:`Extractor.firecrawl`, and ``whisper`` is never imported at all — it is a
subprocess. So importing this module (and pytest collecting it) never requires a
heavy or absent dependency. All external boundaries are injectable so tests do
zero real network, DNS, or subprocess work.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from thoth.config import Config

__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "ExaLike",
    "ExtractError",
    "ExtractedDoc",
    "Extractor",
    "FetchError",
    "FetchedBinary",
    "FirecrawlLike",
    "SsrfError",
    "TranscriptionError",
    "WebHit",
    "assert_url_allowed",
    "is_url_allowed",
]

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
"""URL schemes the SSRF guard permits; everything else is rejected."""

MAX_DOWNLOAD_BYTES: int = 50 * 1024 * 1024
"""Hard cap (50 MiB) on one server-side binary fetch (:meth:`Extractor.fetch_binary`).
"""

_DEFAULT_HTTP_TIMEOUT: float = 30.0
"""Default per-request timeout (seconds) for the injected/owned ``httpx`` client."""

_STREAM_CHUNK_BYTES: int = 64 * 1024
"""Chunk size (bytes) used when streaming a binary body to the temp file."""

_IMAGE_EXT_BY_CONTENT_TYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
}
"""Maps a response ``Content-Type`` to the bare lowercase extension for an asset.

Extensions are alphanumeric-only so they satisfy
:data:`thoth.vault.ASSET_SLUG_RE` when an asset filename is later assembled. Used
by :meth:`Extractor.fetch_binary` to suggest a file extension; unknown types fall
back to :data:`_DEFAULT_BINARY_EXT`.
"""

_DEFAULT_BINARY_EXT: str = "bin"
"""Fallback extension for a fetched binary whose ``Content-Type`` is unknown."""


class ExtractError(Exception):
    """Base error for any extraction failure in this module."""


class SsrfError(ExtractError):
    """Raised when a URL is blocked by the SSRF guard (bad scheme or private IP)."""


class FetchError(ExtractError):
    """Raised on a network error, a non-success HTTP status, or a size-cap breach."""


class TranscriptionError(ExtractError):
    """Raised when the ``whisper`` subprocess fails or is not installed."""


class ExaLike(Protocol):
    """Structural type for the slice of ``exa_py`` :meth:`Extractor.web_search` uses."""

    def search_and_contents(self, query: str, *, num_results: int = ...) -> Any:
        """Run a semantic search and return results (shape duck-typed downstream)."""
        ...


class FirecrawlLike(Protocol):
    """Structural type for the Firecrawl client :meth:`Extractor.web_extract` uses."""

    def scrape_url(self, url: str, *, params: dict[str, Any] | None = ...) -> Any:
        """Scrape ``url`` and return a result carrying markdown (duck-typed)."""
        ...


@dataclass(frozen=True, slots=True)
class WebHit:
    """One Exa discovery result (a candidate page to read)."""

    url: str
    """The result's URL."""
    title: str
    """The result's title (empty string when Exa returns none)."""
    snippet: str
    """A short text snippet/highlight for the result (empty when absent)."""


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """Clean markdown plus provenance for a fetched URL (feeds ``Vault.write_raw``)."""

    source_url: str
    """The URL that was extracted."""
    title: str
    """The page title (empty string when the extractor returns none)."""
    markdown: str
    """The extracted clean-markdown body."""


@dataclass(frozen=True, slots=True)
class FetchedBinary:
    """A downloaded binary staged in a temp file (feeds ``Vault.save_asset``)."""

    source_url: str
    """The URL the bytes were fetched from."""
    tmp_path: Path
    """Absolute path to the temporary file holding the downloaded bytes."""
    content_type: str
    """The response ``Content-Type`` (without parameters), lowercased."""
    suggested_ext: str
    """Bare lowercase extension (no dot) derived from ``content_type``."""


def _resolve_ips(host: str) -> list[str]:
    """Resolve ``host`` to its IP-address strings (the monkeypatchable DNS seam).

    Wraps :func:`socket.getaddrinfo` and returns the unique address strings from
    every returned ``sockaddr``. Tests monkeypatch this function to return chosen
    IPs so the SSRF guard is exercised without any real DNS lookup.

    Args:
        host: The hostname (or already-literal IP) to resolve.

    Returns:
        A list of resolved IP-address strings (order preserved, de-duplicated).

    Raises:
        SsrfError: if the host cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SsrfError(f"cannot resolve host {host!r}: {exc}") from exc
    seen: list[str] = []
    for info in infos:
        sockaddr = info[4]
        # sockaddr[0] is the address string for both AF_INET and AF_INET6; the
        # IPv6 tuple types it as str|int in the stubs, so coerce to str.
        ip = str(sockaddr[0])
        if ip not in seen:
            seen.append(ip)
    return seen


def _ip_is_public(ip_text: str) -> bool:
    """Return ``True`` only if ``ip_text`` is a routable public address.

    Treats loopback, private, link-local, reserved, multicast, and unspecified
    addresses (IPv4 and IPv6) as non-public. An unparseable string is non-public.

    Args:
        ip_text: An IP-address string.

    Returns:
        ``True`` if the address is global/public, ``False`` otherwise.
    """
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_url_allowed(url: str, *, allow_private_urls: bool = False) -> bool:
    """Return ``True`` iff the URL is safe to fetch under the SSRF policy.

    The URL must use an ``http``/``https`` scheme and have a host. Unless
    ``allow_private_urls`` is set, every IP the host resolves to (via the
    :func:`_resolve_ips` seam) must be public; a single private/loopback/
    link-local/reserved/multicast/unspecified address fails the check.

    Args:
        url: The URL to evaluate.
        allow_private_urls: When ``True``, skip the resolved-IP public check (the
            scheme/host requirement still applies). Defaults to ``False``.

    Returns:
        ``True`` if the URL passes the policy, ``False`` otherwise.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = parsed.hostname
    if not host:
        return False
    if allow_private_urls:
        return True
    ips = _resolve_ips(host)
    if not ips:
        return False
    return all(_ip_is_public(ip) for ip in ips)


def assert_url_allowed(url: str, *, allow_private_urls: bool = False) -> None:
    """Raise :class:`SsrfError` unless ``url`` passes :func:`is_url_allowed`.

    This is the single guard gate every network entry point calls *before* any
    client or socket is touched, so a blocked URL never reaches Exa, Firecrawl, or
    ``httpx``.

    Args:
        url: The URL to validate.
        allow_private_urls: Forwarded to :func:`is_url_allowed`.

    Raises:
        SsrfError: if the scheme is not ``http``/``https`` or (unless allowed) any
            resolved IP is non-public.
    """
    if not is_url_allowed(url, allow_private_urls=allow_private_urls):
        raise SsrfError(f"URL blocked by SSRF guard: {url!r}")


def _content_type_to_ext(content_type: str) -> str:
    """Map a (possibly parameterised) ``Content-Type`` to a bare lowercase extension.

    Strips any ``; charset=...`` parameter, lowercases, and looks the bare type up
    in :data:`_IMAGE_EXT_BY_CONTENT_TYPE`, falling back to :data:`_DEFAULT_BINARY_EXT`.

    Args:
        content_type: The raw ``Content-Type`` header value (may be empty).

    Returns:
        A bare lowercase extension (no leading dot).
    """
    bare = content_type.split(";", 1)[0].strip().lower()
    return _IMAGE_EXT_BY_CONTENT_TYPE.get(bare, _DEFAULT_BINARY_EXT)


def _hit_attr(obj: Any, *names: str) -> str:
    """Return the first present attribute/key among ``names`` as a string, else ``''``.

    Tolerant of both attribute-style SDK objects and plain dicts so a test fake can
    use either shape.

    Args:
        obj: The Exa result object or dict.
        names: Candidate attribute/key names tried in order.

    Returns:
        The first found value coerced to ``str``, or ``''`` if none is present.
    """
    for name in names:
        value: Any = None
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return str(value)
    return ""


class Extractor:
    """URL/PDF/image/STT extraction behind injected clients and the SSRF guard.

    All external boundaries are injectable: an :class:`ExaLike` and a
    :class:`FirecrawlLike` client (created lazily from :class:`~thoth.config.Config`
    keys only when first used), an :class:`httpx.Client` (back it with
    :class:`httpx.MockTransport` in tests), and the ``whisper`` CLI name (shelled out
    via :func:`subprocess.run`). The SSRF gate runs inside :meth:`web_extract` and
    :meth:`fetch_binary` before any of those boundaries is touched.
    """

    def __init__(
        self,
        config: Config,
        *,
        exa: ExaLike | None = None,
        firecrawl: FirecrawlLike | None = None,
        http_client: httpx.Client | None = None,
        allow_private_urls: bool = False,
        whisper_bin: str = "whisper",
    ) -> None:
        """Build an :class:`Extractor`.

        Args:
            config: The frozen runtime config supplying the Exa/Firecrawl API keys.
            exa: An optional injected Exa client; created lazily on first use of
                :attr:`exa` when ``None``.
            firecrawl: An optional injected Firecrawl client; created lazily on first
                use of :attr:`firecrawl` when ``None``.
            http_client: An optional injected :class:`httpx.Client`; a default
                client is created lazily on first use of :attr:`http_client` when
                ``None``. Inject one backed by :class:`httpx.MockTransport` in tests.
            allow_private_urls: When ``True``, the SSRF guard skips the resolved-IP
                public check (the scheme requirement still applies). Defaults to
                ``False`` per SPEC section 12.
            whisper_bin: The ``whisper`` executable name/path for :meth:`transcribe`.
        """
        self._config = config
        self._exa = exa
        self._firecrawl = firecrawl
        self._http_client = http_client
        self._allow_private_urls = allow_private_urls
        self._whisper_bin = whisper_bin

    @property
    def allow_private_urls(self) -> bool:
        """Whether the SSRF guard's resolved-IP public check is bypassed."""
        return self._allow_private_urls

    @property
    def http_client(self) -> httpx.Client:
        """The ``httpx`` client, created lazily (default timeout) on first use."""
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=_DEFAULT_HTTP_TIMEOUT)
        return self._http_client

    @property
    def exa(self) -> ExaLike:
        """The Exa client, importing ``exa_py`` and reading the key lazily on first use.

        Raises:
            ExtractError: if ``EXA_API_KEY`` is not configured.
        """
        if self._exa is None:
            if self._config.exa_api_key is None:
                raise ExtractError(
                    "EXA_API_KEY is required for web search but is not set"
                )
            from exa_py import Exa

            self._exa = Exa(self._config.exa_api_key)
        return self._exa

    @property
    def firecrawl(self) -> FirecrawlLike:
        """The Firecrawl client, importing the SDK and key lazily on first use.

        Raises:
            ExtractError: if ``FIRECRAWL_API_KEY`` is not configured.
        """
        if self._firecrawl is None:
            if self._config.firecrawl_api_key is None:
                raise ExtractError(
                    "FIRECRAWL_API_KEY is required for web extraction but is not set"
                )
            from firecrawl import FirecrawlApp

            self._firecrawl = FirecrawlApp(api_key=self._config.firecrawl_api_key)
        return self._firecrawl

    def web_search(self, query: str, *, num_results: int = 5) -> list[WebHit]:
        """Discover candidate pages for ``query`` via Exa (semantic search).

        Side-effect-free and read-only (also reused by the Phase 3 research path).
        The Exa result is duck-typed: each item may expose attributes or dict keys
        for ``url``, ``title`` and a snippet/highlight, all coerced to strings.

        Args:
            query: The natural-language search query.
            num_results: How many results to request (default 5).

        Returns:
            A list of :class:`WebHit` (possibly empty).

        Raises:
            ExtractError: if the Exa client is unavailable or the call fails.
        """
        try:
            response = self.exa.search_and_contents(query, num_results=num_results)
        except ExtractError:
            raise
        except Exception as exc:
            raise ExtractError(f"exa search failed: {exc}") from exc
        results = getattr(response, "results", None)
        if results is None and isinstance(response, dict):
            results = response.get("results")
        if results is None:
            results = response if isinstance(response, list) else []
        hits: list[WebHit] = []
        for item in results:
            hits.append(
                WebHit(
                    url=_hit_attr(item, "url"),
                    title=_hit_attr(item, "title"),
                    snippet=_hit_attr(item, "snippet", "highlights", "text"),
                )
            )
        return hits

    def web_extract(self, url: str) -> ExtractedDoc:
        """Fetch ``url`` and return its clean markdown via Firecrawl (SSRF-guarded).

        :func:`assert_url_allowed` runs first, so a blocked URL never reaches the
        Firecrawl client. The Firecrawl result is duck-typed for ``markdown`` and a
        title under ``metadata``/``title``.

        Args:
            url: The URL to extract.

        Returns:
            An :class:`ExtractedDoc` with ``source_url``, ``title`` and ``markdown``.

        Raises:
            SsrfError: if the URL is blocked by the SSRF guard.
            ExtractError: if extraction fails or returns no markdown.
        """
        assert_url_allowed(url, allow_private_urls=self._allow_private_urls)
        try:
            result = self.firecrawl.scrape_url(url, params={"formats": ["markdown"]})
        except ExtractError:
            raise
        except Exception as exc:
            raise ExtractError(
                f"firecrawl extraction failed for {url!r}: {exc}"
            ) from exc
        markdown = self._extract_markdown(result)
        if not markdown:
            raise ExtractError(f"firecrawl returned no markdown for {url!r}")
        title = self._extract_title(result)
        return ExtractedDoc(source_url=url, title=title, markdown=markdown)

    @staticmethod
    def _extract_markdown(result: Any) -> str:
        """Pull the ``markdown`` field out of a Firecrawl result (dict or object)."""
        if isinstance(result, dict):
            value = result.get("markdown")
        else:
            value = getattr(result, "markdown", None)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _extract_title(result: Any) -> str:
        """Pull a title from a Firecrawl result's ``metadata.title`` or ``title``."""
        metadata: Any = (
            result.get("metadata")
            if isinstance(result, dict)
            else getattr(result, "metadata", None)
        )
        if isinstance(metadata, dict) and isinstance(metadata.get("title"), str):
            return metadata["title"]
        title = (
            result.get("title")
            if isinstance(result, dict)
            else getattr(result, "title", None)
        )
        return title if isinstance(title, str) else ""

    def fetch_binary(self, url: str) -> FetchedBinary:
        """Stream ``url`` to a temp file server-side, size-capped (SSRF-guarded).

        :func:`assert_url_allowed` runs first, so a blocked URL never issues an
        ``httpx`` request. The body is streamed in chunks; if it exceeds
        :data:`MAX_DOWNLOAD_BYTES` the partial temp file is removed and
        :class:`FetchError` is raised. The bytes are never base64-encoded; the
        returned temp path is handed straight to :meth:`thoth.vault.Vault.save_asset`.

        Args:
            url: The URL to download.

        Returns:
            A :class:`FetchedBinary` with the temp path, content type, and a
            suggested extension.

        Raises:
            SsrfError: if the URL is blocked by the SSRF guard.
            FetchError: on a network error, a non-success status, or a size-cap breach.
        """
        assert_url_allowed(url, allow_private_urls=self._allow_private_urls)
        fd, tmp_name = tempfile.mkstemp(prefix="thoth-fetch-")
        tmp_path = Path(tmp_name)
        try:
            content_type = self._stream_to_fd(url, fd)
        except FetchError:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise FetchError(f"failed to fetch {url!r}: {exc}") from exc
        return FetchedBinary(
            source_url=url,
            tmp_path=tmp_path,
            content_type=content_type,
            suggested_ext=_content_type_to_ext(content_type),
        )

    def _stream_to_fd(self, url: str, fd: int) -> str:
        """Stream ``url`` into the open file descriptor ``fd``; return the content type.

        Raises :class:`FetchError` on a non-success status or when the running total
        exceeds :data:`MAX_DOWNLOAD_BYTES`. The descriptor is always closed.

        Args:
            url: The URL to download (already SSRF-checked by the caller).
            fd: An open, writable OS file descriptor for the temp file.

        Returns:
            The response ``Content-Type`` header (without parameters), lowercased.
        """
        total = 0
        content_type = ""
        try:
            with self.http_client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise FetchError(
                        f"fetch of {url!r} returned HTTP {response.status_code}"
                    )
                raw_ct = response.headers.get("content-type", "")
                content_type = raw_ct.split(";", 1)[0].strip().lower()
                for chunk in response.iter_bytes(_STREAM_CHUNK_BYTES):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise FetchError(
                            f"download of {url!r} exceeded the "
                            f"{MAX_DOWNLOAD_BYTES}-byte cap"
                        )
                    os.write(fd, chunk)
        except httpx.HTTPError as exc:
            raise FetchError(f"network error fetching {url!r}: {exc}") from exc
        finally:
            os.close(fd)
        return content_type

    def transcribe(self, audio_path: Path, *, model: str = "base") -> str:
        """Transcribe an audio file by shelling out to the ``whisper`` CLI.

        The binary named by ``whisper_bin`` is invoked with ``--model`` and
        ``--output_format txt``; its stdout is returned as the transcript text. No
        ``whisper`` Python package is imported (it stays a subprocess), so this code
        path is import-safe in CI. Tests monkeypatch :func:`subprocess.run`.

        Args:
            audio_path: Path to the local audio file to transcribe.
            model: The whisper model size to request (default ``"base"``).

        Returns:
            The transcript text (stdout, stripped of trailing whitespace).

        Raises:
            TranscriptionError: if ``whisper`` is not installed (``FileNotFoundError``)
                or exits non-zero (stderr surfaced in the message).
        """
        argv = [
            self._whisper_bin,
            str(audio_path),
            "--model",
            model,
            "--output_format",
            "txt",
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TranscriptionError(
                f"whisper binary {self._whisper_bin!r} not found: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise TranscriptionError(
                f"whisper failed (exit {completed.returncode}): "
                f"{completed.stderr.strip()!r}"
            )
        return completed.stdout.rstrip()
