from __future__ import annotations

import io
import unittest
from unittest import mock

from PIL import Image

from services import image_upscale_service


def _png(width: int, height: int, mode: str = "RGB") -> bytes:
    image = Image.new(mode, (width, height))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _size(payload: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(payload)) as image:
        return image.size


def _patch_config(enabled: bool):
    config = mock.Mock()
    config.image_upscale_enabled = enabled
    return mock.patch.object(image_upscale_service, "config", config)


class UpscalePngTests(unittest.TestCase):
    def test_disabled_returns_original_bytes(self):
        source = _png(64, 64)
        with _patch_config(enabled=False):
            result = image_upscale_service.upscale_png(source, 256)
        self.assertIs(result, source)

    def test_upscales_long_edge_to_target(self):
        with _patch_config(enabled=True):
            result = image_upscale_service.upscale_png(_png(64, 64), 256)
        self.assertEqual(_size(result), (256, 256))

    def test_keeps_aspect_ratio_for_non_square(self):
        with _patch_config(enabled=True):
            result = image_upscale_service.upscale_png(_png(96, 64), 256)
        self.assertEqual(_size(result), (256, 171))

    def test_already_large_enough_is_untouched(self):
        source = _png(256, 256)
        with _patch_config(enabled=True):
            self.assertEqual(image_upscale_service.upscale_png(source, 256), source)

    def test_none_or_zero_target_is_untouched(self):
        source = _png(64, 64)
        with _patch_config(enabled=True):
            for target in [None, 0, -1]:
                with self.subTest(target=target):
                    self.assertIs(image_upscale_service.upscale_png(source, target), source)

    def test_keeps_alpha_channel(self):
        with _patch_config(enabled=True):
            result = image_upscale_service.upscale_png(_png(64, 64, mode="RGBA"), 256)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.mode, "RGBA")

    def test_invalid_payload_returns_original_instead_of_raising(self):
        source = b"not a png"
        with _patch_config(enabled=True):
            self.assertEqual(image_upscale_service.upscale_png(source, 256), source)


class ParseSizeLongEdgeTests(unittest.TestCase):
    def test_client_requested_size_is_the_target(self):
        # 传多少就是多少：1K 不放大，2K/4K 按需放大
        self.assertEqual(image_upscale_service.parse_size_long_edge("1024x1024"), 1024)
        self.assertEqual(image_upscale_service.parse_size_long_edge("2048x2048"), 2048)
        self.assertEqual(image_upscale_service.parse_size_long_edge("4096x4096"), 4096)

    def test_parses_lowercase_and_uppercase_x(self):
        self.assertEqual(image_upscale_service.parse_size_long_edge("2048x2048"), 2048)
        self.assertEqual(image_upscale_service.parse_size_long_edge("1024X1536"), 1536)

    def test_parses_fullwidth_multiplication_sign(self):
        self.assertEqual(image_upscale_service.parse_size_long_edge("2048×2048"), 2048)

    def test_returns_none_for_auto_and_empty(self):
        for value in ["auto", "", "  ", None, "large", "1792*1024"]:
            with self.subTest(value=value):
                self.assertIsNone(image_upscale_service.parse_size_long_edge(value))


if __name__ == "__main__":
    unittest.main()
