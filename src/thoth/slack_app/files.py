"""Server-side staging of Slack uploads: pick, download, and spool to a temp path."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx

from thoth.filetypes import IMAGE_EXTS as _IMAGE_EXTS

from .responder import Responder, SlackClientLike


class SlackError(Exception):
    """Base error for the Slack surface (raised by the daemon factory wiring)."""


def _is_image_file(file_info: dict[str, Any]) -> bool:
    """Reports whether a Slack file object is an image (the issue #84 batch gate).

    Decides whether a multi-file upload is a homogeneous image batch, captured as one
    page. Slack's own ``mimetype`` is preferred, falling back to the filename extension
    so a file object without one still routes; both mirror the extensions the ingest
    pipeline recognises.
    """
    mimetype = file_info.get("mimetype")
    if isinstance(mimetype, str) and mimetype.lower().startswith("image/"):
        return True
    name = file_info.get("name")
    if isinstance(name, str) and "." in name:
        return name.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS
    return False


def _download_url(file_info: dict[str, Any]) -> str | None:
    """Picks the private download URL Slack exposes on a file object."""
    for key in ("url_private_download", "url_private"):
        value = file_info.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _download_to_tmp(
    file_info: dict[str, Any],
    client: SlackClientLike | None,
    responder: Responder,
) -> tuple[Path, str | None] | None:
    """Downloads one Slack file object to a temp path, or returns ``None``.

    Returns the staged ``(path, filename)`` on success. A missing download URL or a
    failed download warns and returns ``None``, so a batch keeps the rest of its files.
    """
    url = _download_url(file_info)
    if not url:
        responder.say(":warning: Could not find a downloadable URL for that file.")
        return None
    filename = file_info.get("name")
    suffix = Path(filename).suffix if isinstance(filename, str) and filename else ""
    try:
        data = _download_bytes(client, url)
    except SlackError as exc:
        responder.say(f":x: Could not download that file: {exc}")
        return None
    with tempfile.NamedTemporaryFile(
        prefix="thoth-upload-", suffix=suffix, delete=False
    ) as handle:
        handle.write(data)
        tmp_path = Path(handle.name)
    return tmp_path, filename if isinstance(filename, str) else None


def _download_bytes(client: SlackClientLike | None, url: str) -> bytes:
    """Downloads private Slack file bytes over an authenticated request.

    A test injects a fake client exposing ``download``, which is used directly with no
    network. The real ``slack_sdk.WebClient`` has no download helper, so the bytes come
    from an authenticated ``GET`` to the file's private URL carrying the client's bot
    token, which is the only way to read a ``url_private`` link. Raises
    :class:`SlackError` when there is no usable download path or the URL is not an https
    Slack URL.
    """
    downloader = getattr(client, "download", None)
    if callable(downloader):
        data: Any = downloader(url)
        return bytes(data)
    token = getattr(client, "token", None)
    if not token:
        raise SlackError("Slack client has no token to download the file")
    if not url.startswith("https://"):
        raise SlackError(f"refusing to download a non-https file URL: {url!r}")
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content
