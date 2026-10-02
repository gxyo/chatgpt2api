from __future__ import annotations

import unittest
from queue import Queue
from unittest import mock

from curl_cffi import requests
from curl_cffi.requests.models import STREAM_END

from services.openai_backend_api import (
    ChatRequirements,
    ImageMainlineStateError,
    ImageRequestTimeoutError,
    OpenAIBackendAPI,
    is_skipped_mainline_error,
)
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageGenerationError, ImageOutput
from test.test_upstream_retry import FakeAccountService, FakeResponse, FakeSession, FakeStreamResponse
from utils.helper import (
    CHANNEL_BUSY_MESSAGE,
    IMAGE_MAINLINE_REJECT_MESSAGE,
    IMAGE_RETRY_MESSAGE,
    UpstreamHTTPError,
    ensure_ok,
    is_retriable_upstream_error,
    sanitize_image_error_text,
    strip_plumbing_status_prefix,
)


def error_stream(status: int = 400, chunks: list | None = None) -> requests.Response:
    """Use curl_cffi's real lazy response reader without making network calls."""
    response = requests.Response(curl=mock.Mock())
    response.status_code = status
    response.queue = Queue()
    for chunk in chunks if chunks is not None else [b'{"skipped_', b'mainline":true}']:
        response.queue.put(chunk)
    response.queue.put(STREAM_END)
    return response


class ImageMainlineRecoveryTests(unittest.TestCase):
    def test_streaming_400_preserves_json_body_and_closes_response(self):
        api = OpenAIBackendAPI()
        response = error_stream()
        api.session = FakeSession([response])
        self.assertEqual(response.content, b"")

        with self.assertRaises(UpstreamHTTPError) as raised:
            api._start_image_generation("draw", ChatRequirements("req"), "conduit", "gpt-image-2")

        self.assertEqual(raised.exception.body, {"skipped_mainline": True})
        self.assertTrue(is_skipped_mainline_error(raised.exception))
        response.curl.close.assert_called_once()
        self.assertEqual(len(api.session.calls), 1)

    def test_successful_stream_is_left_unread_and_open(self):
        response = error_stream(200, [b'data: {"conversation_id":"ok"}\n\n'])
        ensure_ok(response, "conversation")
        response.curl.close.assert_not_called()
        self.assertEqual(next(response.iter_content()), b'data: {"conversation_id":"ok"}\n\n')
        response.close()

    def test_streaming_http_error_survives_body_read_failure(self):
        response = error_stream(503, [requests.exceptions.RequestException("stream interrupted")])
        response.headers["Retry-After"] = "2"
        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "conversation")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.retry_after, 2)
        response.curl.close.assert_called_once()

    def test_skipped_mainline_detection_handles_transport_error_formats(self):
        errors = [
            RuntimeError('status_code=400, {"skipped_mainline":true}'),
            RuntimeError('status=400, body={"skipped_mainline": true}'),
            requests.exceptions.HTTPError(
                "HTTP Error 400: Bad Request",
                response=FakeResponse(400, {"skipped_mainline": True}),
            ),
            UpstreamHTTPError("conversation", 400, '{"skipped_mainline":true}'),
        ]
        for error in errors:
            with self.subTest(error=error):
                self.assertTrue(is_skipped_mainline_error(error))

    def test_other_400_and_non_400_errors_are_not_mainline_rejections(self):
        errors = [
            UpstreamHTTPError("conversation", 400, {"skipped_mainline": False}),
            UpstreamHTTPError("conversation", 400, {"skipped_mainline": "true"}),
            UpstreamHTTPError("conversation", 403, {"skipped_mainline": True}),
            UpstreamHTTPError("conversation", 400, {"error": "invalid prompt"}),
            RuntimeError('status_code=400, {"skipped_mainline":false}'),
            RuntimeError('status_code=4000, {"skipped_mainline":true}'),
            RuntimeError('status_code=503, {"skipped_mainline":true}'),
            RuntimeError('{"skipped_mainline":true}'),
        ]
        for error in errors:
            with self.subTest(error=error):
                self.assertFalse(is_skipped_mainline_error(error))
        # A consumed conduit must never enter the generic same-POST retry loop.
        self.assertFalse(is_retriable_upstream_error(
            UpstreamHTTPError("conversation", 400, {"skipped_mainline": True})
        ))

    def test_lazy_400_rebuilds_complete_handshake_before_success(self):
        api = OpenAIBackendAPI("token-a")
        failed = error_stream()
        success = FakeStreamResponse([b'data: {"conversation_id":"ok"}', b'data: [DONE]'])
        api.session = FakeSession([
            FakeResponse(200, {"conduit_token": "conduit-one"}), failed,
            FakeResponse(200, {"conduit_token": "conduit-two"}), success,
        ])
        with mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", side_effect=[ChatRequirements("one"), ChatRequirements("two")]), \
             mock.patch.object(api, "_sleep_with_deadline"):
            payloads = list(api._stream_picture_conversation("draw", "gpt-image-2", []))

        self.assertEqual(payloads, ['{"conversation_id":"ok"}', "[DONE]"])
        calls = api.session.calls
        self.assertEqual(len(calls), 4)
        for prepare, mainline in ((calls[0][2], calls[1][2]), (calls[2][2], calls[3][2])):
            self.assertEqual(prepare["json"]["parent_message_id"], mainline["json"]["parent_message_id"])
            self.assertEqual(prepare["json"]["partial_query"]["id"], mainline["json"]["messages"][0]["id"])
        self.assertNotEqual(calls[0][2]["json"]["partial_query"]["id"], calls[2][2]["json"]["partial_query"]["id"])
        self.assertNotEqual(calls[1][2]["headers"]["X-Conduit-Token"], calls[3][2]["headers"]["X-Conduit-Token"])
        failed.curl.close.assert_called_once()
        self.assertTrue(success.closed)

    def test_text_error_rebuilds_handshake_without_reuploading_references(self):
        api = OpenAIBackendAPI("token-a")
        success = FakeStreamResponse([b'data: [DONE]'])
        reference = {"file_id": "file-ref"}
        with mock.patch.object(api, "_upload_image", return_value=reference) as upload, \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", side_effect=[ChatRequirements("one"), ChatRequirements("two")]) as requirements, \
             mock.patch.object(api, "_prepare_image_conversation", side_effect=["one", "two"]), \
             mock.patch.object(api, "_start_image_generation", side_effect=[RuntimeError('status_code=400, {"skipped_mainline":true}'), success]) as start, \
             mock.patch.object(api, "_sleep_with_deadline"):
            self.assertEqual(list(api._stream_picture_conversation("edit", "gpt-image-2", ["image"])), ["[DONE]"])
        upload.assert_called_once()
        self.assertEqual(requirements.call_count, 2)
        self.assertTrue(all(call.args[4] == [reference] for call in start.call_args_list))

    def test_mainline_recovery_stops_at_request_deadline(self):
        api = OpenAIBackendAPI("token-a")
        with mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", return_value=ChatRequirements("one")) as requirements, \
             mock.patch.object(api, "_prepare_image_conversation", return_value="one"), \
             mock.patch.object(api, "_start_image_generation", side_effect=UpstreamHTTPError("conversation", 400, {"skipped_mainline": True})), \
             mock.patch.object(api, "_sleep_with_deadline", side_effect=ImageRequestTimeoutError("deadline")):
            with self.assertRaises(ImageRequestTimeoutError):
                list(api._stream_picture_conversation("draw", "gpt-image-2", []))
        requirements.assert_called_once()

    def test_image_pool_recovers_after_local_handshake_attempts_exhausted(self):
        accounts = FakeAccountService(["token-a", "token-b"])
        starts = []
        success = FakeStreamResponse([b'data: [DONE]'])

        def start(api, *args, **kwargs):
            starts.append(api.access_token)
            if api.access_token == "token-a":
                raise UpstreamHTTPError("conversation", 400, {"skipped_mainline": True})
            return success

        def output(api, request, index=1, total=1):
            list(api._stream_picture_conversation(request.prompt, request.model, []))
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", accounts), \
             mock.patch.object(conversation, "stream_image_outputs", output), \
             mock.patch.object(OpenAIBackendAPI, "_bootstrap"), \
             mock.patch.object(OpenAIBackendAPI, "_get_chat_requirements", return_value=ChatRequirements("req")), \
             mock.patch.object(OpenAIBackendAPI, "_prepare_image_conversation", return_value="conduit"), \
             mock.patch.object(OpenAIBackendAPI, "_start_image_generation", start), \
             mock.patch.object(OpenAIBackendAPI, "_sleep_with_deadline"):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(model="gpt-image-2", prompt="draw")))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(starts, ["token-a", "token-a", "token-b"])
        self.assertEqual(accounts.image_results, [("token-a", False), ("token-b", True)])
        self.assertEqual(accounts.removed_invalid_tokens, [])

    def test_image_pool_mainline_failures_have_bounded_rescue(self):
        for error in (
            UpstreamHTTPError("conversation", 400, {"skipped_mainline": True}),
            RuntimeError('status_code=400, {"skipped_mainline":true}'),
            ImageMainlineStateError("image prepare returned no conduit token"),
        ):
            with self.subTest(error=error):
                accounts = FakeAccountService(["token-a", "token-b", "token-c", "token-d"])
                with mock.patch.object(conversation, "account_service", accounts), \
                     mock.patch.object(conversation, "stream_image_outputs", side_effect=error), \
                     mock.patch.object(conversation, "TRANSIENT_IMAGE_RESCUE_WINDOW_SECS", 0):
                    with self.assertRaises(ImageGenerationError) as raised:
                        list(conversation.stream_image_outputs_with_pool(ConversationRequest(model="gpt-image-2", prompt="draw")))
                self.assertEqual(str(raised.exception), CHANNEL_BUSY_MESSAGE)
                self.assertEqual(len(accounts.image_results), 3)
                self.assertEqual(accounts.removed_invalid_tokens, [])

    def test_mainline_failure_carries_diagnostics_onto_the_escaping_error(self):
        """失败诊断要挂在抛给上层的异常上：日志页（和一键导出）才能看到这些数。"""
        accounts = FakeAccountService(["token-a"])
        with mock.patch.object(conversation, "account_service", accounts), \
             mock.patch.object(
                 conversation, "stream_image_outputs",
                 side_effect=UpstreamHTTPError("conversation", 400, {"skipped_mainline": True}),
             ), \
             mock.patch.object(conversation, "TRANSIENT_IMAGE_RESCUE_WINDOW_SECS", 0):
            with self.assertRaises(ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(model="gpt-image-2", prompt="draw")))

        diagnostics = raised.exception.image_diagnostics
        self.assertEqual(diagnostics["trace_id"], raised.exception.image_trace)
        self.assertEqual(diagnostics["index"], 1)
        self.assertEqual(diagnostics["image_inflight"], 0)
        self.assertEqual(diagnostics["image_peers"], 0)
        self.assertTrue(diagnostics["mainline_rejected"])
        self.assertIn("rescue", diagnostics)
        # 诊断里不能出现完整 access_token
        self.assertNotIn("token-a", diagnostics["request_token"])

    def test_single_account_can_recover_with_a_new_backend_session(self):
        accounts = FakeAccountService(["token-a"])
        backends = []

        def output(api, request, index=1, total=1):
            backends.append(api)
            if len(backends) == 1:
                raise UpstreamHTTPError("conversation", 400, {"skipped_mainline": True})
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", accounts), \
             mock.patch.object(conversation, "stream_image_outputs", output):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(model="gpt-image-2", prompt="draw")))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.image_results, [("token-a", False), ("token-a", True)])
        self.assertIsNot(backends[0].session, backends[1].session)

    def test_image_pool_does_not_retry_ordinary_400_or_failure_after_output(self):
        for after_output in (False, True):
            with self.subTest(after_output=after_output):
                accounts = FakeAccountService(["token-a", "token-b"])
                def output(api, request, index=1, total=1):
                    if after_output:
                        yield ImageOutput(kind="progress", model=request.model, index=index, total=total)
                    body = {"skipped_mainline": True} if after_output else {"error": "invalid request"}
                    raise UpstreamHTTPError("conversation", 400, body)
                with mock.patch.object(conversation, "account_service", accounts), \
                     mock.patch.object(conversation, "stream_image_outputs", output):
                    with self.assertRaises(ImageGenerationError) as raised:
                        list(conversation.stream_image_outputs_with_pool(ConversationRequest(model="gpt-image-2", prompt="draw")))
                self.assertIsInstance(raised.exception.__cause__, UpstreamHTTPError)
                self.assertEqual(accounts.image_results, [("token-a", False)])


class ImageErrorSanitizerTests(unittest.TestCase):
    def test_skipped_mainline_forms_map_to_the_user_facing_message(self):
        cases = [
            'status_code=400, {"skipped_mainline":true}',
            '{"skipped_mainline": true}',
            'conversation failed: status=400, body={"skipped_mainline": true}',
            'status_code=400, {"skipped_mainline":TRUE}',
        ]
        for text in cases:
            with self.subTest(text=text):
                message = sanitize_image_error_text(text)
                self.assertEqual(message, "本次生图失败，请重试。")
                self.assertEqual(message, IMAGE_MAINLINE_REJECT_MESSAGE)
                # 日志卡片已经单独展示 status=400，文案里不能再带一遍状态码前缀。
                self.assertNotIn("status", message)

    def test_status_prefix_is_stripped_from_the_message(self):
        cases = {
            "status_code=400, 请稍后再试": "请稍后再试",
            "status=500, 请稍后再试": "请稍后再试",
            "status_code=400，请稍后再试": "请稍后再试",
            "status_code=400 请稍后再试": "请稍后再试",
            "status_code=400, status=400, 请稍后再试": "请稍后再试",
            "请稍后再试": "请稍后再试",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(strip_plumbing_status_prefix(text), expected)

    def test_upstream_plumbing_text_falls_back_to_the_generic_retry_message(self):
        cases = [
            "UpstreamHTTPError: /backend-api/f/conversation/prepare failed: status=500, body={}",
            "GET https://chatgpt.com/backend-api/me returned 403",
            "sentinel/chat-requirements failed",
            "UpstreamHTTPError: /backend-api/f/conversation failed",
            "",
            None,
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(sanitize_image_error_text(text), IMAGE_RETRY_MESSAGE)

    def test_business_messages_are_never_rewritten(self):
        for text in (
            CHANNEL_BUSY_MESSAGE,
            "Image generation was rejected by upstream policy.",
            "Image generation completed upstream but the result could not be retrieved.",
            "你上传的图片包含违规内容，请更换后重试。",
            IMAGE_RETRY_MESSAGE,
        ):
            with self.subTest(text=text):
                self.assertEqual(sanitize_image_error_text(text), text)


class ImageHandshakeLockTests(unittest.TestCase):
    class RecordingAccountService:
        def __init__(self, events: list[tuple[str, str]], acquire_result: bool = True):
            self.events = events
            self.acquire_result = acquire_result

        def get_account(self, access_token: str):
            return {"email": "a@example.test"}

        def acquire_image_handshake(self, access_token: str, timeout=None) -> bool:
            self.events.append(("acquire", access_token))
            return self.acquire_result

        def release_image_handshake(self, access_token: str) -> None:
            self.events.append(("release", access_token))

        def image_inflight_count(self, access_token: str) -> int:
            return 2

        def image_handshake_diagnostics(self, access_token: str) -> dict:
            return {}

        def mark_image_mainline_rejected(self, access_token: str) -> None:
            return None

    def test_picture_stream_holds_the_handshake_lock_only_until_the_stream_opens(self):
        events: list[tuple[str, str]] = []
        api = OpenAIBackendAPI("token-a")
        success = FakeStreamResponse([b'data: [DONE]'])
        with mock.patch("services.openai_backend_api.account_service", self.RecordingAccountService(events)), \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", return_value=ChatRequirements("one")), \
             mock.patch.object(api, "_prepare_image_conversation", return_value="one"), \
             mock.patch.object(api, "_start_image_generation", return_value=success):
            payloads = list(api._stream_picture_conversation("draw", "gpt-image-2", []))

        self.assertEqual(payloads, ["[DONE]"])
        self.assertEqual(events, [("acquire", "token-a"), ("release", "token-a")])

    def test_picture_stream_releases_the_handshake_lock_when_the_handshake_fails(self):
        events: list[tuple[str, str]] = []
        api = OpenAIBackendAPI("token-a")
        with mock.patch("services.openai_backend_api.account_service", self.RecordingAccountService(events)), \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", return_value=ChatRequirements("one")), \
             mock.patch.object(api, "_prepare_image_conversation", return_value="one"), \
             mock.patch.object(api, "_start_image_generation", side_effect=UpstreamHTTPError("conversation", 400, {"skipped_mainline": True})), \
             mock.patch.object(api, "_sleep_with_deadline"):
            with self.assertRaises(UpstreamHTTPError):
                list(api._stream_picture_conversation("draw", "gpt-image-2", []))

        self.assertEqual(events, [("acquire", "token-a"), ("release", "token-a")])


class ConduitWindowTests(unittest.TestCase):
    """搜索/可编辑文件与生图共用同一个账号 conduit 窗口。

    三者都是 prepare → mainline 两步，同账号并发时先到的 conduit 会被上游丢掉并回
    {"skipped_mainline": true}，所以窗口必须一起排队。
    """

    def _service(self, events, acquire_result: bool = True):
        return ImageHandshakeLockTests.RecordingAccountService(events, acquire_result=acquire_result)

    def test_search_holds_the_window_until_the_mainline_is_accepted(self):
        events: list[tuple[str, str]] = []
        api = OpenAIBackendAPI("token-a")

        def fake_prepare(prompt, model):
            events.append(("prepare", model))
            return "conduit"

        def fake_run(prompt, conduit_token, model, on_started=None):
            events.append(("mainline", conduit_token))
            if on_started is not None:
                on_started()
            events.append(("stream", model))
            return "conv-1"

        with mock.patch("services.openai_backend_api.account_service", self._service(events)), \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_prepare_search_conversation", side_effect=fake_prepare), \
             mock.patch.object(api, "_run_search_conversation", side_effect=fake_run), \
             mock.patch.object(api, "_wait_search_result", return_value={"answer": "ok"}):
            result = api.search("hi")

        self.assertEqual(result, {"answer": "ok"})
        names = [name for name, _ in events]
        # 窗口覆盖 prepare 和 mainline；mainline 被上游接收后立刻放锁，读流不占窗口
        self.assertEqual(names, ["acquire", "prepare", "mainline", "release", "stream"])

    def test_search_releases_the_window_when_prepare_fails(self):
        events: list[tuple[str, str]] = []
        api = OpenAIBackendAPI("token-a")
        with mock.patch("services.openai_backend_api.account_service", self._service(events)), \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_prepare_search_conversation", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                api.search("hi")

        self.assertEqual([name for name, _ in events], ["acquire", "release"])

    def test_window_timeout_proceeds_without_the_lock(self):
        events: list[tuple[str, str]] = []
        api = OpenAIBackendAPI("token-a")
        with mock.patch("services.openai_backend_api.account_service", self._service(events, acquire_result=False)):
            self.assertFalse(api._acquire_conduit_window())
            # 没拿到锁就不该放锁（否则会放掉别的线程持有的锁）
            api._release_conduit_window()

        self.assertEqual([name for name, _ in events], ["acquire"])


if __name__ == "__main__":
    unittest.main()
