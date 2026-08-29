"""URL, PDF, image and audio extraction, SSRF-guarded with injectable boundaries.

This is the appliance's read-only window onto the outside world (SPEC sections 6 and
7.1). It turns a URL into clean markdown, fetches a binary to a temp file server-side
for :meth:`thoth.vault.Vault.save_asset` rather than base64, and shells out to a local
``whisper`` CLI for speech-to-text.

Every network entry point passes :func:`assert_url_allowed` before any client or socket
is touched. The scheme must be http or https and every resolved IP must be public unless
``allow_private_urls`` is set, which blocks ``file://`` and friends along with loopback,
private and link-local targets such as the ``169.254.169.254`` cloud-metadata address.

Import safety matters here, and it is the pytest-collection trap. Only the standard
library, ``httpx`` and :mod:`thoth.config` are imported at module level. Firecrawl is
imported lazily inside :attr:`Extractor.firecrawl` and ``whisper`` is never imported at
all, because it is a subprocess.

Every external boundary is injectable, so tests do no real network, DNS or subprocess
work.
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
    "ExtractError",
    "ExtractedDoc",
    "Extractor",
    "FetchError",
    "FetchedBinary",
    "FirecrawlLike",
    "SsrfError",
    "TranscriptionError",
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


class FirecrawlLike(Protocol):
    """Structural type for the Firecrawl client :meth:`Extractor.web_extract` uses."""

    def scrape(self, url: str, *, formats: Any = ...) -> Any:
        """Scrapes ``url`` and returns a duck-typed result carrying markdown.

        ``firecrawl-py`` 4.x replaced ``scrape_url`` with ``scrape``, returning a
        ``Document``. ``formats`` is typed ``Any`` so the real client, whose parameter
        is a list of format options, satisfies this protocol structurally.
        """
        ...


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """Clean markdown plus provenance for a fetched URL, feeding ``write_raw``."""

    source_url: str
    """The URL that was extracted."""
    title: str
    """The page title (empty string when the extractor returns none)."""
    markdown: str
    """The extracted clean-markdown body."""


@dataclass(frozen=True, slots=True)
class FetchedBinary:
    """A downloaded binary staged in a temp file, feeding ``save_asset``."""

    source_url: str
    """The URL the bytes were fetched from."""
    tmp_path: Path
    """Absolute path to the temporary file holding the downloaded bytes."""
    content_type: str
    """The response ``Content-Type`` (without parameters), lowercased."""
    suggested_ext: str
    """Bare lowercase extension (no dot) derived from ``content_type``."""


def _resolve_ips(host: str) -> list[str]:
    """Resolves a host to its IP strings, and is the monkeypatchable DNS seam.

    Tests monkeypatch this to return chosen IPs, so the SSRF guard is exercised with no
    real DNS lookup.

    Args:
        host: The hostname, or an already-literal IP, to resolve.

    Returns:
        The resolved IP strings, de-duplicated and in order.

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
        # Sockaddr[0] is the address string for both AF_INET and AF_INET6. The IPv6
        # tuple types it as str|int in the stubs, so coerce it
        ip = str(sockaddr[0])
        if ip not in seen:
            seen.append(ip)
    return seen


def _ip_is_public(ip_text: str) -> bool:
    """Reports whether an address is routable and public.

    Loopback, private, link-local, reserved, multicast and unspecified addresses are all
    non-public, for both IPv4 and IPv6, and so is an unparseable string.

    Args:
        ip_text: An IP-address string.

    Returns:
        True when the address is public, otherwise False.
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
    """Reports whether a URL is safe to fetch under the SSRF policy.

    The URL must use an http or https scheme and carry a host. Unless private URLs are
    allowed, every IP the host resolves to must be public, so one private or loopback
    address fails the whole check.

    Args:
        url: The URL to evaluate.
        allow_private_urls: Skip the resolved-IP check. The scheme and host
            requirements still apply.

    Returns:
        True when the URL passes the policy, otherwise False.
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
    """Raises unless a URL passes :func:`is_url_allowed`.

    This is the single gate every network entry point calls before any client or socket
    is touched, so a blocked URL never reaches Firecrawl or ``httpx``.

    Args:
        url: The URL to validate.
        allow_private_urls: Forwarded to :func:`is_url_allowed`.

    Raises:
        SsrfError: if the scheme is wrong or a resolved IP is non-public.
    """
    if not is_url_allowed(url, allow_private_urls=allow_private_urls):
        raise SsrfError(f"URL blocked by SSRF guard: {url!r}")


def _content_type_to_ext(content_type: str) -> str:
    """Maps a bare lowercased content type to a bare lowercase extension.

    Normalisation is owned by :meth:`Extractor._stream_to_fd`, so this is a plain lookup
    with a fallback.

    Args:
        content_type: The bare, lowercased content type, possibly empty.

    Returns:
        A bare lowercase extension, with no leading dot.
    """
    return _IMAGE_EXT_BY_CONTENT_TYPE.get(content_type, _DEFAULT_BINARY_EXT)


def _field(obj: Any, name: str) -> Any:
    """Reads a field from either a dict or an object."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class Extractor:
    """Extraction behind injected clients and the SSRF guard.

    Every external boundary is injectable: the Firecrawl client, created lazily from the
    config keys only when first used, the ``httpx`` client, which tests back with a mock
    transport, and the ``whisper`` CLI name, which is shelled out. The SSRF gate runs
    inside :meth:`web_extract` and :meth:`fetch_binary` before any of those boundaries
    is touched.
    """

    def __init__(
        self,
        config: Config,
        *,
        firecrawl: FirecrawlLike | None = None,
        http_client: httpx.Client | None = None,
        allow_private_urls: bool = False,
        whisper_bin: str = "whisper",
    ) -> None:
        """Builds an extractor.

        Args:
            config: Frozen runtime config supplying the Firecrawl API key.
            firecrawl: Injected Firecrawl client, or None to create one lazily.
            http_client: Injected client, or None to create one lazily. Tests inject
                one backed by :class:`httpx.MockTransport`.
            allow_private_urls: Skip the SSRF resolved-IP check. The scheme
                requirement still applies, and the default is False per SPEC
                section 12.
            whisper_bin: The ``whisper`` executable for :meth:`transcribe`.
        """
        self._config = config
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
        """The ``httpx`` client, created lazily on first use with a default timeout."""
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=_DEFAULT_HTTP_TIMEOUT)
        return self._http_client

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
            from firecrawl import Firecrawl

            self._firecrawl = Firecrawl(api_key=self._config.firecrawl_api_key)
        return self._firecrawl

    def web_extract(self, url: str) -> ExtractedDoc:
        """Fetches a URL and returns its clean markdown, SSRF-guarded.

        The guard runs first, so a blocked URL never reaches the Firecrawl client. The
        result is duck-typed for markdown and a title.

        Args:
            url: The URL to extract.

        Returns:
            The extracted document with its provenance.

        Raises:
            SsrfError: if the URL is blocked by the guard.
            ExtractError: if extraction fails or returns no markdown.
        """
        assert_url_allowed(url, allow_private_urls=self._allow_private_urls)
        try:
            result = self.firecrawl.scrape(url, formats=["markdown"])
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
        """Pulls the ``markdown`` field out of a Firecrawl result."""
        value = _field(result, "markdown")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _extract_title(result: Any) -> str:
        """Pulls a title from a Firecrawl result's metadata or top level.

        ``firecrawl-py`` 4.x returns a document whose metadata is an object rather than
        a dict, so both shapes are accepted.
        """
        meta_title = _field(_field(result, "metadata"), "title")
        if isinstance(meta_title, str):
            return meta_title
        title = _field(result, "title")
        return title if isinstance(title, str) else ""

    def fetch_binary(self, url: str) -> FetchedBinary:
        """Streams a URL to a temp file server-side, size-capped and SSRF-guarded.

        The guard runs first, so a blocked URL never issues a request. The body streams
        in chunks, and breaching :data:`MAX_DOWNLOAD_BYTES` removes the partial temp
        file and raises. The bytes are never base64-encoded, and the temp path goes
        straight to :meth:`thoth.vault.Vault.save_asset`.

        Args:
            url: The URL to download.

        Returns:
            The staged binary, with its temp path, content type and suggested
            extension.

        Raises:
            SsrfError: if the URL is blocked by the guard.
            FetchError: on a network error, a non-success status, or a size breach.
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
        """Streams a URL into an open file descriptor and returns the content type.

        The descriptor is always closed.

        Args:
            url: The URL to download, already SSRF-checked by the caller.
            fd: An open, writable descriptor for the temp file.

        Returns:
            The response content type, without parameters and lowercased.

        Raises:
            FetchError: on a non-success status or a size-cap breach.
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
        """Transcribes an audio file by shelling out to the ``whisper`` CLI.

        The transcript is read back from the ``<stem>.txt`` file whisper writes into a
        throwaway output directory. Stdout is deliberately not scraped, because it is
        the verbose timestamped segment dump and the real output always goes to a file.

        Directing that file to a temp dir rather than the process cwd is what makes this
        work on the appliance, where the unit runs under a read-only mount. A
        default-cwd write fails with a read-only filesystem error, and whisper catches
        it, logs a skip and still exits 0, so the failure would otherwise pass silently
        as an empty transcript.

        No ``whisper`` package is imported, since it stays a subprocess, so this path is
        import-safe in CI.

        Args:
            audio_path: Path to the local audio file.
            model: The whisper model size to request.

        Returns:
            The transcript text, stripped of trailing whitespace.

        Raises:
            TranscriptionError: if whisper is missing, exits non-zero, or wrote no
                transcript.
        """
        with tempfile.TemporaryDirectory(prefix="thoth-whisper-") as out_dir:
            argv = [
                self._whisper_bin,
                str(audio_path),
                "--model",
                model,
                "--output_format",
                "txt",
                "--output_dir",
                out_dir,
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
            # whisper writes <output_dir>/<audio-stem>.txt. A missing file means it
            # skipped the input, having caught a write or decode error and still exited
            # 0, so surface that as a failure rather than an empty transcript
            transcript_file = Path(out_dir) / f"{audio_path.stem}.txt"
            try:
                return transcript_file.read_text(encoding="utf-8").rstrip()
            except FileNotFoundError as exc:
                raise TranscriptionError(
                    f"whisper wrote no transcript for {audio_path.name!r} "
                    f"(exit 0); stderr: {completed.stderr.strip()!r}"
                ) from exc
