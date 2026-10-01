from __future__ import annotations

import unittest
from unittest import mock

from services.config import config
from services.log_service import _image_error_response
from utils.helper import IMAGE_RETRY_MESSAGE, sanitize_image_error_text

CUSTOM_MESSAGE = "请联系管理员补号"
PLUMBING_ERROR = "backend-api/foo status_code=500"


class ImageQuotaMessageTests(unittest.TestCase):
    """号池无可用额度时的用户可见文案：留空返回原文，填了就返回填的文字。"""

    def _with_quota_message(self, value: str):
        # config 是全局单例，patch.dict 只改内存、不落盘。
        return mock.patch.dict(config.data, {"image_quota_error_message": value})

    def test_unset_keeps_the_upstream_text(self):
        with self._with_quota_message(""):
            self.assertEqual(sanitize_image_error_text("no available image quota"), "no available image quota")

    def test_unset_normalizes_the_plan_specific_variant(self):
        with self._with_quota_message(""):
            self.assertEqual(sanitize_image_error_text("no available plus image quota"), "no available image quota")

    def test_configured_message_replaces_every_quota_variant(self):
        with self._with_quota_message(CUSTOM_MESSAGE):
            for text in (
                "no available image quota",
                "no available plus image quota",
                "status_code=429 no available image quota",
            ):
                with self.subTest(text=text):
                    self.assertEqual(sanitize_image_error_text(text), CUSTOM_MESSAGE)

    def test_unrelated_errors_are_untouched_by_the_setting(self):
        with self._with_quota_message(CUSTOM_MESSAGE):
            self.assertEqual(sanitize_image_error_text(PLUMBING_ERROR), IMAGE_RETRY_MESSAGE)
            self.assertEqual(sanitize_image_error_text("image account slot wait timed out"), "image account slot wait timed out")

    def test_quota_error_response_is_429_with_the_configured_message(self):
        with self._with_quota_message(CUSTOM_MESSAGE):
            response = _image_error_response(RuntimeError("status_code=429 no available image quota"))

        self.assertEqual(response.status_code, 429)
        body = response.body.decode()
        self.assertIn(CUSTOM_MESSAGE, body)
        self.assertIn('"code":"insufficient_quota"', body)

    def test_wrapped_quota_error_is_still_429(self):
        """并行生图会把额度错误重包装成 ImageGenerationError 并洗掉文案，状态码仍须是 429。"""
        from services.protocol.conversation import ImageGenerationError

        try:
            raise RuntimeError("no available image quota")
        except RuntimeError as exc:
            wrapped = ImageGenerationError(CUSTOM_MESSAGE, conversation_id="")
            wrapped.__cause__ = exc

        with self._with_quota_message(CUSTOM_MESSAGE):
            response = _image_error_response(wrapped)

        self.assertEqual(response.status_code, 429)
        self.assertIn('"code":"insufficient_quota"', response.body.decode())

    def test_quota_error_response_falls_back_when_unset(self):
        with self._with_quota_message(""):
            response = _image_error_response(RuntimeError("no available image quota"))

        self.assertEqual(response.status_code, 429)
        self.assertIn("no available image quota", response.body.decode())


if __name__ == "__main__":
    unittest.main()
