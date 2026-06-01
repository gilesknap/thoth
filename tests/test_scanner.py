"""Tests for :mod:`thoth.scanner` -- the model-free OpenCV document cleanup (issue #68).

OpenCV (``opencv-python-headless``) is a runtime *optional* dependency that is **not**
installed in the CI gate, so this whole module is guarded by
``pytest.importorskip("cv2")``: it SKIPS where OpenCV is absent and RUNS (exercising the
real de-warp/threshold logic) when OpenCV is installed. Inputs are built with NumPy/cv2
so no fixture image files are needed.
"""

from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from thoth.scanner import clean_document  # noqa: E402  (after importorskip guard)


def _document_photo() -> bytes:
    """Encode a synthetic photo: a bright tilted quadrilateral on a dark background.

    The bright quad stands in for a page against a dark desk, which is exactly the
    largest four-point contour :func:`clean_document` hunts for.
    """
    canvas = np.zeros((400, 400, 3), dtype=np.uint8)
    # A clearly four-cornered, slightly skewed bright page.
    corners = np.array([[60, 40], [350, 70], [330, 360], [40, 330]], dtype=np.int32)
    cv2.fillConvexPoly(canvas, corners, (255, 255, 255))
    # Some darker "text" marks so the thresholded scan has structure.
    cv2.putText(canvas, "TEXT", (120, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    return encoded.tobytes()


def test_clean_document_returns_decodable_scan() -> None:
    """A photo with a clear page quad yields non-None PNG bytes that decode."""
    cleaned = clean_document(_document_photo(), ext="png")

    assert cleaned is not None
    assert isinstance(cleaned, bytes)
    decoded = cv2.imdecode(np.frombuffer(cleaned, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded is not None
    assert decoded.size > 0


def test_clean_document_small_icon_quad_returns_none() -> None:
    """A small high-contrast quad (a logo/icon, not the page) is rejected -> None.

    Guards the conservative page gate: without it, "largest 4-point contour" warps to
    the icon and emits a tiny junk crop (the issue #68 live-verify failure)."""
    canvas = np.zeros((400, 400, 3), dtype=np.uint8)
    # A crisp 50x50 bright square in a corner -- ~1.5% of the frame, well under the
    # page-area threshold and nowhere near spanning the edges.
    cv2.rectangle(canvas, (30, 30), (80, 80), (255, 255, 255), -1)

    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    assert clean_document(encoded.tobytes(), ext="png") is None


def test_clean_document_blank_image_returns_none() -> None:
    """A uniform image has no document-like quadrilateral -> graceful None."""
    blank = np.full((300, 300, 3), 127, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", blank)
    assert ok

    assert clean_document(encoded.tobytes(), ext="png") is None


def test_clean_document_corrupt_bytes_returns_none() -> None:
    """Non-image bytes can't be decoded -> graceful None (never raises)."""
    assert clean_document(b"not an image at all", ext="png") is None
