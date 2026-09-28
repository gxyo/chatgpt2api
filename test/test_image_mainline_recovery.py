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
from utils.helper import CHANNEL_BUSY_MESSAGE, UpstreamHTTPError, ensure_ok, is_retriable_upstream_error


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


if __name__ == "__main__":
    unittest.main()
