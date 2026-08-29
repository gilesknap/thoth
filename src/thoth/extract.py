"""URL, PDF, image and audio extraction, with an SSRF guard and injectable seams.

This module is the appliance's read-only window onto the outside world (SPEC
sections 6 and 7.1). It turns a URL into clean markdown through Firecrawl, fetches
a binary, a PDF or an image, to a temporary file *server-side* for
:meth:`thoth.vault.Vault.save_asset` and never as base64, and shells out to a local
``whisper`` CLI for optional speech-to-text.

Every network entry point passes through the single SSRF gate
:func:`assert_url_allowed` **before** any client or socket is touched: the URL
scheme must be ``http`` or ``https``, and every resolved IP must be public unless
``allow_private_urls`` is set. That blocks the ``file://``, ``data:`` and
``gopher://`` schemes, and loopback, private, link-local, reserved, multicast and
unspecified targets, such as the ``169.254.169.254`` cloud-metadata address.

Import safety, the pytest-collection trap: module top level imports only the
standard library, ``httpx`` and :mod:`thoth.config`. :attr:`Extractor.firecrawl`
imports the Firecrawl client **lazily**, and nothing imports ``whisper`` at all,
because it is a subprocess. Importing this module, and pytest collecting it,
therefore never needs a heavy or absent dependency. Every external boundary is
injectable, so a test does no real network, DNS or subprocess work.
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
    """Raised when the SSRF guard blocks a URL: a bad scheme, or a private IP."""


class FetchError(ExtractError):
    """Raised on a network error, a non-success HTTP status, or a size-cap breach."""


class TranscriptionError(ExtractError):
    """Raised when the ``whisper`` subprocess fails or is not installed."""


class FirecrawlLike(Protocol):
    """Structural type for the Firecrawl client :meth:`Extractor.web_extract` uses."""

    def scrape(self, url: str, *, formats: Any = ...) -> Any:
        """Scrape ``url`` and return a result carrying markdown (duck-typed).

        ``firecrawl-py`` 4.x replaced ``scrape_url(url, params={...})`` with
        ``scrape(url, formats=[...])``, which returns a ``Document`` carrying
        ``.markdown`` and ``.metadata``. ``formats`` is ``Any``, so the real client,
        whose parameter is typed ``list[FormatOption]``, satisfies this Protocol
        structurally.
        """
        ...


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """Clean markdown plus provenance for a fetched URL, feeding ``Vault.write_raw``."""

    source_url: str
    """The URL that was extracted."""
    title: str
    """The page title (empty string when the extractor returns none)."""
    markdown: str
    """The extracted clean-markdown body."""


@dataclass(frozen=True, slots=True)
class FetchedBinary:
    """A downloaded binary staged in a temp file, feeding ``Vault.save_asset``."""

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
    every returned ``sockaddr``. A test monkeypatches this function to return chosen
    IPs, so the SSRF guard runs without any real DNS lookup.

    Args:
        host: The hostname, or already-literal IP, to resolve.

    Returns:
        A list of resolved IP-address strings, de-duplicated in order.

    Raises:
        SsrfError: when the host cannot be resolved.
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

    Treats a loopback, private, link-local, reserved, multicast or unspecified
    address as non-public, for both IPv4 and IPv6. An unparseable string is
    non-public too.

    Args:
        ip_text: An IP-address string.

    Returns:
        ``True`` when the address is global, else ``False``.
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
    """Return ``True`` only when the URL is safe to fetch under the SSRF policy.

    The module docstring gives the policy. :func:`_resolve_ips` is the DNS seam it
    resolves through.

    Args:
        url: The URL to evaluate.
        allow_private_urls: When ``True``, skips the resolved-IP public check, though
            the scheme and host requirement still applies. Defaults to ``False``.

    Returns:
        ``True`` when the URL passes the policy, else ``False``.
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

    Args:
        url: The URL to validate.
        allow_private_urls: Forwarded to :func:`is_url_allowed`.

    Raises:
        SsrfError: when the scheme is neither ``http`` nor ``https``, or when a
            resolved IP is non-public and private URLs are not allowed.
    """
    if not is_url_allowed(url, allow_private_urls=allow_private_urls):
        raise SsrfError(f"URL blocked by SSRF guard: {url!r}")


def _content_type_to_ext(content_type: str) -> str:
    """Map a bare lowercased content type to a bare lowercase extension.

    :meth:`Extractor._stream_to_fd` owns the normalisation, stripping parameters and
    lowercasing, so this is a plain lookup in :data:`_IMAGE_EXT_BY_CONTENT_TYPE` that
    falls back to :data:`_DEFAULT_BINARY_EXT`.

    Args:
        content_type: The bare, lowercased content type, possibly empty.

    Returns:
        A bare lowercase extension, with no leading dot.
    """
    return _IMAGE_EXT_BY_CONTENT_TYPE.get(content_type, _DEFAULT_BINARY_EXT)


def _field(obj: Any, name: str) -> Any:
    """Return ``obj[name]`` for a dict or ``obj.name`` for an object, else ``None``."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class Extractor:
    """URL, PDF, image and speech extraction behind injected clients and the SSRF guard.

    Every external boundary is injectable: a :class:`FirecrawlLike` client, created
    lazily from :class:`~thoth.config.Config` keys only on first use, an
    :class:`httpx.Client`, which a test backs with :class:`httpx.MockTransport`, and
    the ``whisper`` CLI name, shelled out through :func:`subprocess.run`. The SSRF
    gate runs inside :meth:`web_extract` and :meth:`fetch_binary` before any of those
    boundaries is touched.
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
        """Build an :class:`Extractor`.

        Args:
            config: The frozen runtime config supplying the Firecrawl API key.
            firecrawl: An optional injected Firecrawl client. With ``None``,
                :attr:`firecrawl` creates one lazily on first use.
            http_client: An optional injected :class:`httpx.Client`. With ``None``,
                :attr:`http_client` creates a default client lazily on first use. A test
                injects one backed by :class:`httpx.MockTransport`.
            allow_private_urls: When ``True``, the SSRF guard skips the resolved-IP
                public check, though the scheme requirement still applies. Defaults to
                ``False``, per SPEC section 12.
            whisper_bin: The ``whisper`` executable name or path for :meth:`transcribe`.
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
        """The ``httpx`` client, created lazily on first use at the default timeout."""
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=_DEFAULT_HTTP_TIMEOUT)
        return self._http_client

    @property
    def firecrawl(self) -> FirecrawlLike:
        """The Firecrawl client, importing the SDK and key lazily on first use.

        Raises:
            ExtractError: when ``FIRECRAWL_API_KEY`` is not configured.
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
        """Fetch ``url`` and return its clean markdown through Firecrawl, SSRF-guarded.

        The Firecrawl result is duck-typed for ``markdown`` and for a title under
        ``metadata`` or ``title``.

        Args:
            url: The URL to extract.

        Returns:
            An :class:`ExtractedDoc` with ``source_url``, ``title`` and ``markdown``.

        Raises:
            SsrfError: when the SSRF guard blocks the URL.
            ExtractError: when extraction fails or returns no markdown.
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
        """Pull the ``markdown`` field out of a Firecrawl result, dict or object."""
        value = _field(result, "markdown")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _extract_title(result: Any) -> str:
        """Pull a title from a Firecrawl result's ``metadata.title`` or ``title``.

        ``firecrawl-py`` 4.x returns a ``Document`` whose ``metadata`` is a
        ``DocumentMetadata`` *object* carrying ``.title``, not a dict, so this accepts
        both shapes.
        """
        meta_title = _field(_field(result, "metadata"), "title")
        if isinstance(meta_title, str):
            return meta_title
        title = _field(result, "title")
        return title if isinstance(title, str) else ""

    def fetch_binary(self, url: str) -> FetchedBinary:
        """Stream ``url`` to a size-capped temp file server-side, SSRF-guarded.

        The body streams in chunks, and exceeding :data:`MAX_DOWNLOAD_BYTES` removes
        the partial temp file and raises :class:`FetchError`. The bytes are never
        base64-encoded, and the returned temp path goes straight to
        :meth:`thoth.vault.Vault.save_asset`.

        Args:
            url: The URL to download.

        Returns:
            A :class:`FetchedBinary` with the temp path, content type and a suggested
            extension.

        Raises:
            SsrfError: when the SSRF guard blocks the URL.
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
        """Stream ``url`` into the open descriptor ``fd``, and return the content type.

        Raises :class:`FetchError` on a non-success status, or when the running total
        exceeds :data:`MAX_DOWNLOAD_BYTES`. The descriptor is always closed.

        Args:
            url: The URL to download, already SSRF-checked by the caller.
            fd: An open, writable OS file descriptor for the temp file.

        Returns:
            The response ``Content-Type`` header, lowercased and without parameters.
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

        This invokes the binary named by ``whisper_bin`` with ``--model`` and
        ``--output_format txt`` into a throwaway ``--output_dir`` temp directory, then
        reads the transcript back from the ``<stem>.txt`` file it writes there. It does
        NOT scrape stdout, because the whisper CLI's stdout is the verbose timestamped
        segment dump and it always writes its real output to a file.

        Directing that file to a temp dir, rather than letting it default to the process
        cwd, is what makes this work on the appliance, where the systemd unit runs with
        ``WorkingDirectory`` under a ``ProtectSystem=strict`` read-only mount. A
        default-cwd write would fail with ``OSError: Read-only file system``, and
        whisper *catches* that, logs ``Skipping ...`` and still exits 0, so the failure
        would otherwise pass silently as an empty transcript.

        Nothing imports the ``whisper`` Python package, since it stays a subprocess, so
        this code path is import-safe in CI. A test monkeypatches
        :func:`subprocess.run`.

        Args:
            audio_path: Path to the local audio file to transcribe.
            model: The whisper model size to request, ``"base"`` by default.

        Returns:
            The transcript text from stdout, stripped of trailing whitespace.

        Raises:
            TranscriptionError: when ``whisper`` is not installed, raising
                ``FileNotFoundError``, or exits non-zero, with stderr in the message.
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
            # whisper writes ``<output_dir>/<audio-stem>.txt``. A missing file means
            # whisper skipped the input (e.g. it caught a write/decode error and still
            # exited 0); surface that as a failure rather than an empty transcript.
            transcript_file = Path(out_dir) / f"{audio_path.stem}.txt"
            try:
                return transcript_file.read_text(encoding="utf-8").rstrip()
            except FileNotFoundError as exc:
                raise TranscriptionError(
                    f"whisper wrote no transcript for {audio_path.name!r} "
                    f"(exit 0); stderr: {completed.stderr.strip()!r}"
                ) from exc
