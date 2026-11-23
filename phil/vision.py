# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Optional, in-memory screenshot preparation for vision-capable model calls."""

from __future__ import annotations

import importlib.util
import io
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Screenshot:
    """A prepared image and the coordinate transform back to the phone display."""

    bytes: bytes
    media_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    scale_x: float
    scale_y: float


def pillow_available() -> bool:
    """Check for Pillow without importing it during normal application startup."""
    return importlib.util.find_spec("PIL") is not None


def prepare_screenshot(
    png_bytes: bytes, *, max_size: int = 1024, jpeg_quality: int = 70
) -> tuple[Screenshot | None, str | None]:
    """Resize and JPEG-encode a PNG, or explain why it is not useful to send.

    Pillow is deliberately imported only when vision is enabled and a screenshot
    is being prepared.
    """
    if not png_bytes.startswith(PNG_SIGNATURE):
        raise ValueError("Screenshot data is not a PNG image.")

    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(png_bytes)) as source:
        image = source.convert("RGB")
    original_width, original_height = image.size
    if original_width <= 0 or original_height <= 0:
        raise ValueError("Screenshot has invalid dimensions.")

    stats = ImageStat.Stat(image)
    if max(stats.mean) <= 12 or all(high - low <= 2 for low, high in stats.extrema):
        return None, "blank or near-black screen"

    longest = max(image.size)
    if longest > max_size:
        ratio = max_size / longest
        image = image.resize(
            (max(1, round(original_width * ratio)), max(1, round(original_height * ratio))),
            Image.Resampling.LANCZOS,
        )

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=jpeg_quality)
    width, height = image.size
    return (
        Screenshot(
            bytes=output.getvalue(),
            media_type="image/jpeg",
            width=width,
            height=height,
            original_width=original_width,
            original_height=original_height,
            scale_x=original_width / width,
            scale_y=original_height / height,
        ),
        None,
    )


def save_screenshot(
    screenshot: Screenshot, directory: str | Path, step: int, ordinal: int = 1
) -> Path:
    """Save a prepared image only when an explicit directory was configured."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    destination = path / f"step-{step:03d}-shot-{ordinal:02d}.jpg"
    destination.write_bytes(screenshot.bytes)
    return destination
