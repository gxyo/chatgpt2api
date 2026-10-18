from __future__ import annotations

import unittest
from unittest import mock

from services.config import config
from services.openai_backend_api import ImagePollTimeoutError
from services.protocol import conversation
from services.protocol.conversation import (
    ConversationRequest,
    ImageUnsupportedAppResponseError,
    UNSUPPORTED_APP_RESPONSE_CODE,
    UNSUPPORTED_APP_RESPONSE_MESSAGE,
    is_unsupported_app_response,
    unsupported_app_response_message,
)

REFUSAL_TEXTS = (
    "当前聊天中我无法直接生成图片，但可以先为你整理好适用于图像生成工具的最终提示词。",
    "我目前无法在这个聊天中直接生成图片，但可以帮你整理好最终提示词。",
    "目前我无法在这个对话中直接生成图片，但可以先帮你整理好最终生成提示词。",
    "我目前无法在这个聊天中直接生成图片，但可以帮你整理好可直接用于图像生成工具的最终提示词。",
    "我无法在这个聊天中直接生成图片，但可以帮你整理好最终提示词，保留你要求的构图和尺寸。",
)

DIL_TEXT = (
    "## Saved in Library\n\n<row align=center gap={3}>\n"
    "  <AsyncImage query=\"tiny chibi anime girl long dark navy hair\" aspectRatio=\"3:4\" />\n"
    "  **Mika — source reference**\n"
)


class UnsupportedAppResponseDetectionTests(unittest.TestCase):
    def test_refusal_texts_are_detected(self):
        for text in REFUSAL_TEXTS:
            with self.subTest(text=text[:20]):
                self.assertTrue(is_unsupported_app_response(text))

    def test_create_pet_dil_text_is_detected(self):
        self.assertTrue(is_unsupported_app_response(DIL_TEXT))
        self.assertTrue(is_unsupported_app_response(
            "Saved in Library <AsyncImage query=\"pixel art\" aspectRatio=\"1:1\" />"
        ))

    def test_content_policy_rejection_is_not_detected(self):
        self.assertFalse(is_unsupported_app_response(
            "非常抱歉，生成的图片可能违反了关于裸露、色情或情色内容的防护限制。"
            "如果你认为此判断有误，请重试或修改提示语。"
        ))
        # 内容审核拒绝也会说“无法生成”，但它没有“这个聊天/提示词”上下文。
        self.assertFalse(is_unsupported_app_response(
            "抱歉，我无法生成这张图片，因为它可能违反了内容政策。"
        ))

    def test_normal_text_reply_is_not_detected(self):
        self.assertFalse(is_unsupported_app_response(
            "已将圣诞镂空桌布融入美式客厅。你希望洞里的宠物保持这只博美犬吗？"
        ))


class FakeBackend:
    access_token = "token-a"

    def __init__(self):
        self.image_stream_tail = []


def _event(**overrides):
    event = {
        "type": "conversation.done",
        "text": "",
        "conversation_id": "conv-1",
        "file_ids": [],
        "sediment_ids": [],
        "blocked": False,
        "tool_invoked": False,
        "turn_use_case": "text",
        "model_slug": "gpt-6",
        "app_ui_marker": False,
        "done": True,
    }
    event.update(overrides)
    return event


class StreamUnsupportedAppResponseTests(unittest.TestCase):
    def _run(self, events):
        with mock.patch.object(conversation, "conversation_events", lambda *args, **kwargs: iter(events)):
            return list(conversation.stream_image_outputs(
                FakeBackend(),
                ConversationRequest(model="gpt-image-2", prompt="draw a cat"),
            ))

    def test_refusal_message_raises_with_its_own_code(self):
        with self.assertRaises(ImageUnsupportedAppResponseError) as raised:
            self._run([_event(text=REFUSAL_TEXTS[0])])

        self.assertEqual(raised.exception.code, UNSUPPORTED_APP_RESPONSE_CODE)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("upstream_text", raised.exception.body or {})

    def test_dil_library_message_raises_without_polling(self):
        with self.assertRaises(ImageUnsupportedAppResponseError):
            self._run([_event(text=DIL_TEXT, turn_use_case="multimodal")])

    def test_mid_stream_refusal_aborts_immediately(self):
        events = [
            {
                "type": "conversation.delta",
                "text": "我目前无法在这个聊天中直接生成图片，但可以帮你整理好最终提示词。",
                "delta": "我目前无法在这个聊天中直接生成图片，",
                "conversation_id": "conv-1",
                "file_ids": [],
                "sediment_ids": [],
                "app_ui_marker": False,
            },
        ]

        with self.assertRaises(ImageUnsupportedAppResponseError):
            self._run(events)

    def test_timeout_with_app_ui_marker_is_reported_as_unsupported(self):
        def events():
            yield {
                "type": "conversation.delta",
                "text": "working on the pet...",
                "delta": "working on the pet...",
                "conversation_id": "conv-1",
                "file_ids": [],
                "sediment_ids": [],
                "turn_use_case": "text",
                "app_ui_marker": True,
            }
            raise ImagePollTimeoutError("image request deadline exceeded")

        with self.assertRaises(ImageUnsupportedAppResponseError):
            self._run(events())

    def test_image_gen_timeout_with_app_marker_is_not_reclassified(self):
        # turn_use_case=image gen 是真正的生图轮次：不能因为出现 opGenui 就把
        # 一次普通的生图超时算成应用响应。
        def events():
            yield {
                "type": "conversation.delta",
                "text": "working...",
                "delta": "working...",
                "conversation_id": "conv-1",
                "file_ids": [],
                "sediment_ids": [],
                "turn_use_case": "image gen",
                "app_ui_marker": True,
            }
            raise ImagePollTimeoutError("image request deadline exceeded")

        with self.assertRaises(ImagePollTimeoutError):
            self._run(events())

    def test_timeout_without_app_marker_keeps_the_original_error(self):
        def events():
            yield {
                "type": "conversation.delta",
                "text": "working...",
                "delta": "working...",
                "conversation_id": "conv-1",
                "file_ids": [],
                "sediment_ids": [],
                "app_ui_marker": False,
            }
            raise ImagePollTimeoutError("image request deadline exceeded")

        with self.assertRaises(ImagePollTimeoutError):
            self._run(events())

    def test_normal_text_reply_still_becomes_a_message(self):
        outputs = self._run([_event(text="已将圣诞镂空桌布融入美式客厅。")])

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, "message")
        self.assertIn("圣诞", outputs[0].text)


class UnsupportedResponseMessageConfigTests(unittest.TestCase):
    def _with_message(self, value: str):
        # config 是全局单例，patch.dict 只改内存、不落盘。
        return mock.patch.dict(config.data, {"image_unsupported_response_message": value})

    def test_unset_uses_the_builtin_message(self):
        with self._with_message(""):
            self.assertEqual(unsupported_app_response_message(), UNSUPPORTED_APP_RESPONSE_MESSAGE)

    def test_configured_message_is_returned_to_the_client(self):
        custom = "当前账号暂不支持生图，请联系管理员"
        with self._with_message(custom):
            exc = conversation._unsupported_app_response_error("upstream text")
            error = exc.to_openai_error()["error"]

        self.assertEqual(error["message"], custom)
        self.assertEqual(error["code"], UNSUPPORTED_APP_RESPONSE_CODE)

    def test_configured_message_is_used_for_message_outputs(self):
        custom = "上游没有返回图片，请稍后重试"

        def stream_fn(backend, request, index=1, total=1):
            yield conversation.ImageOutput(
                kind="message", model=request.model, index=index, total=total,
                text=REFUSAL_TEXTS[0], conversation_id="conv-refusal",
            )

        fake_accounts = mock.Mock()
        fake_accounts.get_available_access_token.return_value = "token-a"
        fake_accounts.has_available_image_account.return_value = True
        fake_accounts.acquire_image_handshake.return_value = True
        fake_accounts.get_account.return_value = {"email": "a@example.test"}
        fake_accounts.image_inflight_count.return_value = 0
        fake_accounts.image_handshake_diagnostics.return_value = {}
        fake_accounts.release_image_slot.return_value = None
        fake_accounts.mark_image_result.return_value = None

        with self._with_message(custom), mock.patch.multiple(
            conversation,
            account_service=fake_accounts,
            stream_image_outputs=stream_fn,
        ):
            with self.assertRaises(ImageUnsupportedAppResponseError) as raised:
                list(conversation.stream_image_outputs_with_pool(
                    ConversationRequest(model="gpt-image-2", prompt="x", message_as_error=True)
                ))

        self.assertEqual(raised.exception.to_openai_error()["error"]["message"], custom)


if __name__ == "__main__":
    unittest.main()
