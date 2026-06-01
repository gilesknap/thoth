"""Model-free document cleanup of captured images via OpenCV (issue #68).

When the analyse seam (:mod:`thoth.analyse`) tags an uploaded image as a ``document`` --
a phone snap or scan of a printed/handwritten page -- the raw photo is usually skewed,
cropped loosely against a desk, and unevenly lit. This module turns that photo into a
clean, top-down black-and-white "scan" the way a phone scanner app does: it finds the
page boundary, warps the quadrilateral flat, and adaptively thresholds it to crisp B/W.

The work is **model-free and local** -- pure OpenCV/NumPy, no Anthropic call and no
budget cost. The cleaned scan is saved *alongside* the original photo as an additional
asset; the original is always kept (the warp/threshold is a lossy idealisation, so the
faithful pixels stay available).

Lazy import + optional dependency. ``opencv-python-headless`` is a heavy wheel and is a
**runtime optional dependency** (the ``runtime`` extra in :file:`pyproject.toml`), not a
base dep, and it is *not* installed in CI. So ``cv2`` and ``numpy`` are imported
**lazily inside** :func:`clean_document` -- never at module top level -- exactly like
the ``exa_py`` / ``firecrawl`` / ``whisper`` seams in :mod:`thoth.extract`. That keeps
this module import-safe under pytest collection (``--doctest-modules`` imports every
``thoth.*`` module) and under the autosummary docs build, where OpenCV is absent. For
the same reason this docstring carries **no doctest** that touches ``cv2``.

Graceful degrade. :func:`clean_document` never raises to its caller for ordinary
"couldn't clean this" situations: an undecodable blob or an image with no
document-like quadrilateral returns ``None``. The ingest seam treats ``None`` (and any
exception) alike as "no cleaned scan", so a best-effort cleanup never loses or defers a
capture.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ScannerError",
    "clean_document",
]

# A detected page quad is only trusted to be the *page* when it covers a large
# fraction of the frame. Naive "largest 4-point contour" detection otherwise latches
# onto a small high-contrast sub-element (a logo / icon / boxed figure) and warps to
# *that*, producing a tiny nonsense crop -- far worse than no scan. So a quad must both
# enclose at least :data:`_MIN_PAGE_AREA_RATIO` of the image area AND span at least
# :data:`_MIN_PAGE_SPAN_RATIO` of the frame in each axis (corners near the edges);
# otherwise :func:`clean_document` degrades to ``None`` (no cleaned scan). Conservative
# by design (issue #68 live-verify): a missed page is cheap, a bad crop is not.
_MIN_PAGE_AREA_RATIO: float = 0.5
_MIN_PAGE_SPAN_RATIO: float = 0.6


class ScannerError(Exception):
    """Raised only for a genuinely broken OpenCV install, never for a "bad image".

    Ordinary failures -- an undecodable blob, or a photo with no page-like quadrilateral
    -- are *not* errors: :func:`clean_document` returns ``None`` for those so a
    best-effort cleanup degrades quietly. ``ScannerError`` is reserved for the rare case
    where ``cv2`` itself is unusable; the ingest seam treats it the same as ``None``
    (no cleaned scan), so surfacing it is informational rather than fatal.
    """


def clean_document(image_bytes: bytes, *, ext: str) -> bytes | None:
    """De-warp and threshold a document photo into a clean top-down B/W scan.

    Decodes ``image_bytes`` to grayscale, finds the largest four-point (quadrilateral)
    contour -- the page edge -- applies a perspective transform to warp it flat to a
    top-down view, then adaptively thresholds to a crisp black-and-white scan, and
    re-encodes the result as PNG.

    ``cv2`` (and ``numpy``) are imported **lazily inside this function** so the module
    stays import-safe where ``opencv-python-headless`` is absent (CI, the docs build);
    see the module docstring.

    Args:
        image_bytes: The raw encoded bytes of the captured image (PNG/JPEG/...).
        ext: The original file extension (e.g. ``"jpg"``). Accepted for symmetry with
            the ingest seam's ``(image_bytes, *, ext)`` scanner signature; decoding is
            driven by the bytes themselves, so the value is informational.

    Returns:
        The PNG bytes of the cleaned, de-warped, thresholded scan, or ``None`` when the
        input cannot be decoded or no document-like quadrilateral is found (graceful
        degrade -- the caller keeps the original and skips the scan asset).

    Raises:
        ScannerError: Only if OpenCV itself is unusable (a broken install). Ordinary
            "can't clean this image" outcomes return ``None`` instead of raising.
    """
    del ext  # informational only; decoding is driven by the bytes themselves.

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - exercised only without the extra.
        raise ScannerError(
            "opencv-python-headless is required for document cleanup; install the "
            "'runtime' optional dependency group"
        ) from exc

    # Decode the raw bytes straight to grayscale. A corrupt / non-image blob decodes to
    # None -> graceful degrade.
    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    gray = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None

    # Find the page boundary on a blurred, edge-detected copy. Blur suppresses paper
    # texture so Canny picks up the dominant page edge rather than the text.
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 75, 200)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # The page is the largest contour that approximates to a four-point polygon. Walk
    # contours by descending area and take the first quad we find.
    quad = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            quad = approx
            break
    if quad is None:
        return None

    # Conservative page gate: reject a quad that is too small or too central to be the
    # page (a logo/icon), so a low-confidence detection degrades to "no scan" rather
    # than a junk crop (issue #68). Both the enclosed area and the per-axis span must
    # clear the thresholds.
    points = np.asarray(quad, dtype="float32").reshape(4, 2)
    img_height, img_width = gray.shape[:2]
    area_ratio = float(cv2.contourArea(quad)) / float(img_height * img_width)
    span_width = float(points[:, 0].max() - points[:, 0].min()) / float(img_width)
    span_height = float(points[:, 1].max() - points[:, 1].min()) / float(img_height)
    if (
        area_ratio < _MIN_PAGE_AREA_RATIO
        or span_width < _MIN_PAGE_SPAN_RATIO
        or span_height < _MIN_PAGE_SPAN_RATIO
    ):
        return None

    # Order the four corners as top-left, top-right, bottom-right, bottom-left so the
    # warp maps them to a consistent, un-mirrored rectangle.
    ordered = _order_corners(np, points)
    top_left, top_right, bottom_right, bottom_left = (
        ordered[0],
        ordered[1],
        ordered[2],
        ordered[3],
    )

    # Size the destination rectangle from the max edge lengths of the detected quad so
    # the warped page keeps its real aspect ratio rather than being squashed.
    width_top = np.linalg.norm(top_right - top_left)
    width_bottom = np.linalg.norm(bottom_right - bottom_left)
    height_left = np.linalg.norm(bottom_left - top_left)
    height_right = np.linalg.norm(bottom_right - top_right)
    out_width = int(max(width_top, width_bottom))
    out_height = int(max(height_left, height_right))
    if out_width < 1 or out_height < 1:
        return None

    destination = np.array(
        [
            [0, 0],
            [out_width - 1, 0],
            [out_width - 1, out_height - 1],
            [0, out_height - 1],
        ],
        dtype="float32",
    )
    transform = cv2.getPerspectiveTransform(ordered, destination)
    warped = cv2.warpPerspective(gray, transform, (out_width, out_height))

    # Adaptive threshold copes with uneven lighting across a phone photo far better than
    # a single global cutoff, yielding the crisp B/W "scanned page" look.
    scanned = cv2.adaptiveThreshold(
        warped,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        11,
        10,
    )

    ok, encoded = cv2.imencode(".png", scanned)
    if not ok:
        return None
    return encoded.tobytes()


def _order_corners(np: Any, points: Any) -> Any:
    """Order four quad corners as top-left, top-right, bottom-right, bottom-left.

    Uses the classic coordinate-sum / coordinate-difference trick: the top-left corner
    has the smallest ``x + y`` and the bottom-right the largest; the top-right has the
    smallest ``y - x`` (largest ``x - y``) and the bottom-left the largest ``y - x``.

    Args:
        np: The lazily-imported ``numpy`` module (passed in to keep the import inside
            :func:`clean_document`).
        points: A ``(4, 2)`` array of the detected corner coordinates.

    Returns:
        A ``(4, 2)`` ``float32`` array of the corners in the canonical order.
    """
    ordered = np.zeros((4, 2), dtype="float32")
    coordinate_sum = points.sum(axis=1)
    coordinate_diff = np.diff(points, axis=1)
    ordered[0] = points[coordinate_sum.argmin()]  # top-left
    ordered[2] = points[coordinate_sum.argmax()]  # bottom-right
    ordered[1] = points[coordinate_diff.argmin()]  # top-right
    ordered[3] = points[coordinate_diff.argmax()]  # bottom-left
    return ordered
