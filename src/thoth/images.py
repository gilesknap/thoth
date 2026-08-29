"""Downscale oversized image bytes before storage and before any vision call (#108).

A captured image is both committed into the vault git repo, where an over-large binary
bloats the two-way sync forever, and sent to a multimodal model, where we pay tokens for
resolution the model discards: Claude's vision API downsamples anything whose longest
edge exceeds about 1568px anyway. So an image over a configurable threshold is
downscaled once, before the bytes are hashed, stored or base64-encoded into a vision
block, and the reduced bytes become the asset and the analysis payload.

Pillow is a runtime-only dependency, absent in CI, so it is imported lazily inside
:func:`downscale_if_oversized`. If it is missing, or the bytes are not a decodable
raster image, the original bytes are returned unchanged: a resize is a best-effort
optimisation and never a capture-loss risk. The longest edge is capped at
:data:`MAX_LONGEST_EDGE_PX`, aspect ratio preserved, and the result re-encoded.
"""

from __future__ import annotations

import io
import logging

__all__ = ["MAX_LONGEST_EDGE_PX", "downscale_if_oversized"]

_log = logging.getLogger(__name__)

# The longest-edge cap in pixels. Above this Claude's vision API downsamples anyway, so
# capping here costs no accuracy and shrinks both the binary and the payload (#108)
MAX_LONGEST_EDGE_PX: int = 1568

# JPEG re-encode quality for a downscaled raster. PNGs stay lossless PNG, everything
# else re-encodes as JPEG so the size really drops
_JPEG_QUALITY: int = 85

# The source kinds kept lossless. Anything outside this set re-encodes as JPEG
_LOSSLESS_FORMATS: frozenset[str] = frozenset({"png", "gif", "webp"})


def downscale_if_oversized(
    image_bytes: bytes, *, ext: str, threshold_bytes: int
) -> bytes:
    """Downscales image bytes over ``threshold_bytes``, else returns the original.

    At or below the threshold, or when the threshold is non-positive and the feature is
    off, the exact original bytes come back with no decode and no re-encode, so a small
    image never picks up recompression artefacts and stays byte-identical for the
    SHA-256 idempotency the capture pipeline relies on.

    Above the threshold the image is decoded, scaled so its longest edge is at most
    :data:`MAX_LONGEST_EDGE_PX` and never scaled up, then re-encoded: a lossless source
    kind as PNG, anything else as JPEG. If the result is somehow not smaller than the
    input, the original is kept. Pillow is imported lazily, and if it is absent or the
    bytes are not a decodable raster the original is returned unchanged.

    Args:
        image_bytes: The raw image bytes
        ext: The bare image extension, used only to pick the re-encode format
        threshold_bytes: Images larger than this are downscaled, ``<= 0`` disables

    Returns:
        The possibly smaller bytes, or the original object when no resize applied
    """
    if threshold_bytes <= 0 or len(image_bytes) <= threshold_bytes:
        _log.debug(
            "downscale: under threshold (%d <= %d bytes) or disabled; kept original",
            len(image_bytes),
            threshold_bytes,
        )
        return image_bytes
    try:
        from PIL import Image
    except ImportError:
        _log.debug(
            "downscale: Pillow unavailable; kept original %d bytes", len(image_bytes)
        )
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            # An animated GIF or WebP would lose every frame but the first on
            # re-encode to a static PNG, so leave it alone: preserving the animation
            # beats shrinking it (#108)
            if getattr(image, "is_animated", False):
                _log.info(
                    "skipping downscale of animated image (%d frames) to preserve "
                    "animation",
                    getattr(image, "n_frames", 0),
                )
                return image_bytes
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
        # Pillow raises OSError on an undecodable or truncated image, and a
        # best-effort optimisation must never lose the capture
        return image_bytes
    # Only adopt the re-encode if it actually shrank the payload
    return reduced if len(reduced) < len(image_bytes) else image_bytes


def _encode(image: object, *, ext: str) -> bytes:
    """Re-encodes a Pillow image, keeping a lossless kind as PNG, else JPEG."""
    from PIL import Image

    assert isinstance(image, Image.Image)
    buffer = io.BytesIO()
    if ext.lower().lstrip(".") in _LOSSLESS_FORMATS:
        image.save(buffer, format="PNG", optimize=True)
    else:
        # JPEG has no alpha channel, so flatten any transparency first
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
