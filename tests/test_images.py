"""Tests for :mod:`thoth.images` -- threshold-triggered image downscaling (issue #108).

An over-threshold image is decoded, scaled so its longest edge is at most
:data:`thoth.images.MAX_LONGEST_EDGE_PX`, and re-encoded smaller; an at/under-threshold
image is returned byte-identical (no recompression artefacts, stable SHA-256). Pillow is
a runtime/dev dependency here so the *real* resize is exercised, not faked.
"""

from __future__ import annotations

import io

import pytest

from thoth.images import MAX_LONGEST_EDGE_PX, downscale_if_oversized

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402  (after importorskip)


def _png_bytes(width: int, height: int) -> bytes:
    """A real PNG of the given dimensions, noise-filled so it does not over-compress."""
    import os

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(width: int, height: int) -> bytes:
    """A real JPEG of the given dimensions filled with noise."""
    import os

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=100)
    return buffer.getvalue()


def test_under_threshold_returns_identical_bytes() -> None:
    """An image at/under the threshold is returned untouched (byte-identical)."""
    data = _png_bytes(50, 50)
    result = downscale_if_oversized(data, ext="png", threshold_bytes=10_000_000)
    assert result is data  # the SAME object: no decode, no re-encode


def test_over_threshold_downscales_longest_edge_and_shrinks() -> None:
    """An over-threshold large image is scaled to the longest-edge cap and shrinks."""
    data = _jpeg_bytes(4000, 2000)  # longest edge 4000 >> 1568
    assert len(data) > 1_000_000
    result = downscale_if_oversized(data, ext="jpg", threshold_bytes=1_000_000)

    assert len(result) < len(data)
    with Image.open(io.BytesIO(result)) as out:
        assert max(out.width, out.height) == MAX_LONGEST_EDGE_PX
        # Aspect ratio (2:1) preserved.
        assert out.width == MAX_LONGEST_EDGE_PX
        assert out.height == MAX_LONGEST_EDGE_PX // 2


def test_aspect_ratio_preserved_for_portrait() -> None:
    """A tall image caps its *height* (the longest edge), keeping the ratio."""
    data = _jpeg_bytes(1000, 3136)  # 3136 = 2 * 1568; width scales by the same factor
    result = downscale_if_oversized(data, ext="jpg", threshold_bytes=1_000)
    with Image.open(io.BytesIO(result)) as out:
        assert out.height == MAX_LONGEST_EDGE_PX
        assert out.width == round(1000 * MAX_LONGEST_EDGE_PX / 3136)  # 500


def test_threshold_zero_disables_resize() -> None:
    """A non-positive threshold disables resizing entirely (original returned)."""
    data = _jpeg_bytes(4000, 4000)
    assert downscale_if_oversized(data, ext="jpg", threshold_bytes=0) is data
    assert downscale_if_oversized(data, ext="jpg", threshold_bytes=-1) is data


def test_undecodable_bytes_returned_unchanged() -> None:
    """Bytes that are not a decodable image are returned unchanged (never lost)."""
    data = b"not-an-image" * 200_000  # over the threshold but not an image
    assert len(data) > 1_000_000
    result = downscale_if_oversized(data, ext="png", threshold_bytes=1_000_000)
    assert result is data


def test_missing_pillow_returns_original(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Pillow is unavailable the original bytes are returned (best-effort)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no pillow")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    data = _jpeg_bytes(4000, 4000)
    assert downscale_if_oversized(data, ext="jpg", threshold_bytes=1_000) is data
