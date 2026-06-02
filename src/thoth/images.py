"""Downscale oversized image bytes before storage and before any vision call (#108).

A captured image is both **committed into the vault git repo** (so an over-large binary
bloats the two-way sync forever) and **sent to a multimodal model** for OCR/analysis (so
we pay tokens for resolution the model discards -- Claude's vision API downsamples
anything whose longest edge exceeds ~1568px internally). So an image whose encoded size
exceeds a configurable threshold is downscaled **once**, before the bytes are hashed,
stored, or base64-encoded into a vision block -- the reduced bytes become *the* asset
and *the* analysis payload (see
:data:`thoth.config.Config.image_resize_threshold_bytes` and the hook in
:meth:`thoth.ingest.Ingestor._analyse_bytes`).

Pillow is a **runtime-only** dependency (absent in CI), so it is imported lazily inside
:func:`downscale_if_oversized`; if it is missing, or the bytes are not a decodable
raster image, the original bytes are returned unchanged (resize is a best-effort
optimisation, never a capture-loss risk). The longest edge is capped at
:data:`MAX_LONGEST_EDGE_PX` and the result is re-encoded; aspect ratio is preserved.
"""

from __future__ import annotations

import io

__all__ = ["MAX_LONGEST_EDGE_PX", "downscale_if_oversized"]

# The longest-edge cap, in pixels. Above this Claude's vision API downsamples
# internally, so capping here costs no OCR/understanding accuracy while shrinking both
# the stored binary and the analysis payload (issue #108).
MAX_LONGEST_EDGE_PX: int = 1568

# JPEG re-encode quality for a downscaled raster (a sensible visual/size trade-off).
# PNGs stay PNG (lossless); everything else re-encodes as JPEG so the size really drops.
_JPEG_QUALITY: int = 85

# Bare image extension -> the Pillow format name to re-encode with. An extension outside
# this map (or a transparent PNG/GIF/WebP that we keep lossless) re-encodes as PNG.
_LOSSLESS_FORMATS: frozenset[str] = frozenset({"png", "gif", "webp"})


def downscale_if_oversized(
    image_bytes: bytes, *, ext: str, threshold_bytes: int
) -> bytes:
    """Return downscaled image bytes when over ``threshold_bytes``, else the original.

    Below or at the threshold (or when the threshold is non-positive, disabling the
    feature) the **exact original bytes** are returned -- no decode, no re-encode, so a
    small image never picks up recompression artefacts and stays byte-identical for the
    SHA-256 idempotency the capture pipeline relies on.

    Above the threshold the image is decoded, scaled down so its longest edge is at most
    :data:`MAX_LONGEST_EDGE_PX` (aspect ratio preserved; never *up*-scaled), and
    re-encoded. A lossless source kind (PNG/GIF/WebP) re-encodes as PNG; anything else
    re-encodes as JPEG. If the re-encoded result is somehow not smaller than the input
    (e.g. an already-tiny-dimension but heavyweight blob), the original is kept.

    Pillow is imported lazily; if it is absent or the bytes are not a decodable raster
    image, the original bytes are returned unchanged -- resize is a best-effort
    optimisation and must never lose or corrupt a capture.

    Args:
        image_bytes: The raw image bytes.
        ext: The bare image extension (no dot), used only to pick the re-encode format.
        threshold_bytes: Images strictly larger than this are downscaled; ``<= 0``
            disables resizing entirely (always returns the original).

    Returns:
        The (possibly smaller) image bytes; the original object when no resize applied.
    """
    if threshold_bytes <= 0 or len(image_bytes) <= threshold_bytes:
        return image_bytes
    try:
        from PIL import Image
    except ImportError:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            longest = max(image.width, image.height)
            if longest > MAX_LONGEST_EDGE_PX:
                scale = MAX_LONGEST_EDGE_PX / longest
                new_size = (
                    max(1, round(image.width * scale)),
                    max(1, round(image.height * scale)),
                )
                resized = image.resize(new_size, Image.Resampling.LANCZOS)
            else:
                resized = image.copy()
            reduced = _encode(resized, ext=ext)
    except (OSError, ValueError):
        # Pillow raises OSError for an undecodable/truncated image; never lose the
        # capture over a best-effort optimisation -- keep the original bytes.
        return image_bytes
    # Only adopt the re-encode if it actually shrank the payload.
    return reduced if len(reduced) < len(image_bytes) else image_bytes


def _encode(image: object, *, ext: str) -> bytes:
    """Re-encode a Pillow image, keeping a lossless kind as PNG else JPEG."""
    from PIL import Image

    assert isinstance(image, Image.Image)
    buffer = io.BytesIO()
    if ext.lower().lstrip(".") in _LOSSLESS_FORMATS:
        image.save(buffer, format="PNG", optimize=True)
    else:
        # JPEG has no alpha channel; flatten any transparency onto white first.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
