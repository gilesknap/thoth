"""Downscale oversized image bytes before storage and before any vision call (#108).

A captured image is both **committed into the vault git repo**, where an over-large
binary bloats the two-way sync forever, and **sent to a multimodal model** for OCR and
analysis, where we pay tokens for resolution the model discards, since Claude's vision
API internally downsamples anything whose longest edge exceeds about 1568px. An image
whose encoded size exceeds a configurable threshold is therefore downscaled **once**,
before the bytes are hashed, stored or base64-encoded into a vision block, so the
reduced bytes become *the* asset and *the* analysis payload. See
:data:`thoth.config.Config.image_resize_threshold_bytes` and the hook in
:meth:`thoth.ingest.Ingestor._analyse_bytes`.

Pillow is a **runtime-only** dependency that CI lacks, so
:func:`downscale_if_oversized` imports it lazily and degrades to the original bytes.
"""

from __future__ import annotations

import io
import logging

__all__ = ["MAX_LONGEST_EDGE_PX", "downscale_if_oversized"]

_log = logging.getLogger(__name__)

# The longest-edge cap, in pixels. Above this Claude's vision API downsamples
# internally, so capping here costs no OCR or understanding accuracy while shrinking
# both the stored binary and the analysis payload (issue #108).
MAX_LONGEST_EDGE_PX: int = 1568

# JPEG re-encode quality for a downscaled raster, a sensible visual and size trade-off.
# A PNG stays PNG and lossless, and everything else re-encodes as JPEG so the size
# really drops.
_JPEG_QUALITY: int = 85

# Bare image extension to the Pillow format name to re-encode with. An extension outside
# this map, or a transparent PNG, GIF or WebP we keep lossless, re-encodes as PNG.
_LOSSLESS_FORMATS: frozenset[str] = frozenset({"png", "gif", "webp"})


def downscale_if_oversized(
    image_bytes: bytes, *, ext: str, threshold_bytes: int
) -> bytes:
    """Return downscaled image bytes when over ``threshold_bytes``, else the original.

    At or below the threshold, or when a non-positive threshold disables the feature,
    this returns the **exact original bytes** with no decode and no re-encode, so a
    small image never picks up recompression artefacts and stays byte-identical for the
    SHA-256 idempotency the capture pipeline relies on.

    Above the threshold the image is decoded, scaled down so its longest edge is at
    most :data:`MAX_LONGEST_EDGE_PX`, preserving aspect ratio and never scaling *up*,
    then re-encoded. A lossless source kind, PNG, GIF or WebP, re-encodes as PNG, and
    anything else as JPEG. Should the result somehow not be smaller than the input, as
    for an already-tiny but heavyweight blob, the original is kept.

    Pillow is imported lazily, and the original bytes come back unchanged when it is
    absent or the bytes are not a decodable raster image, because resize is a
    best-effort optimisation that must never lose or corrupt a capture.

    Args:
        image_bytes: The raw image bytes.
        ext: The bare image extension, with no dot, used only to pick the re-encode
            format.
        threshold_bytes: Downscales an image strictly larger than this. ``<= 0``
            disables resizing entirely and always returns the original.

    Returns:
        The possibly smaller image bytes, or the original object when no resize
        applied.
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
            # An animated GIF or WebP would lose every frame but the first on re-encode
            # to a static PNG, and its bytes would no longer match its `.gif` extension,
            # so leave it untouched: keeping the animation beats shrinking it (#108).
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
        # Pillow raises OSError for an undecodable or truncated image. Never lose the
        # capture over a best-effort optimisation, so keep the original bytes.
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
        # JPEG has no alpha channel, so flatten any transparency onto white first.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
