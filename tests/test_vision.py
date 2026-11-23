# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import io

import pytest
from PIL import Image, ImageDraw

from phil.vision import prepare_screenshot


def _png(size: tuple[int, int], color: tuple[int, int, int] = (220, 220, 220)) -> bytes:
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).rectangle((0, 0, 10, 10), fill=(20, 80, 140))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_resize_preserves_aspect_ratio_and_scale():
    screenshot, reason = prepare_screenshot(_png((1600, 800)), max_size=800)

    assert reason is None and screenshot is not None
    assert screenshot.media_type == "image/jpeg"
    assert (screenshot.width, screenshot.height) == (800, 400)
    assert (screenshot.original_width, screenshot.original_height) == (1600, 800)
    assert screenshot.scale_x == screenshot.scale_y == 2
    assert screenshot.bytes.startswith(b"\xff\xd8")


def test_small_screenshot_is_never_upscaled():
    screenshot, _ = prepare_screenshot(_png((300, 120)), max_size=1024)
    assert screenshot is not None
    assert (screenshot.width, screenshot.height) == (300, 120)
    assert screenshot.scale_x == screenshot.scale_y == 1


@pytest.mark.parametrize("color", [(0, 0, 0), (245, 245, 245)])
def test_blank_and_near_black_images_are_skipped(color):
    image = Image.new("RGB", (40, 30), color)
    output = io.BytesIO()
    image.save(output, format="PNG")

    screenshot, reason = prepare_screenshot(output.getvalue())

    assert screenshot is None
    assert reason == "blank or near-black screen"
