from __future__ import annotations

import unittest
import time
from types import SimpleNamespace
from unittest import mock

from services.openai_backend_api import (
    ACCOUNT_INFO_RETRY_ATTEMPTS,
    ACCOUNT_INFO_TIMEOUT_SECS,
    FAST_UPSTREAM_RETRY_ATTEMPTS,
    FAST_UPSTREAM_TIMEOUT_SECS,
    ImageRequestTimeoutError,
    ImagePollTimeoutError,
    OpenAIBackendAPI,
    config as backend_config,
)
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageGenerationError, ImageOutput
from utils.helper import CHANNEL_BUSY_MESSAGE, UpstreamHTTPError, is_retriable_upstream_error, public_error_message


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {}
        self.content = text.encode("utf-8")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict[str, str] = {}

    def post(self, url: str, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.responses.pop(0)


class FakeUrlopenResponse:
    status = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload: bytes = b'{"type":"response.completed"}'):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class FakeAccountService:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.image_results: list[tuple[str, bool]] = []
        self.text_used: list[str] = []
        self.selected_image_tokens: list[str] = []
        self.removed_invalid_tokens: list[tuple[str, str]] = []

    def get_available_access_token(self, excluded_tokens=None, deadline=None, **kwargs):
        excluded = set(excluded_tokens or set())
        for token in self.tokens:
            if token not in excluded:
                self.selected_image_tokens.append(token)
                return token
        raise RuntimeError("no available image quota")

    def get_account(self, access_token: str):
        return {"email": f"{access_token}@example.test"}

    def mark_image_result(self, access_token: str, success: bool):
        self.image_results.append((access_token, success))

    def remove_invalid_token(self, access_token: str, event: str):
        self.removed_invalid_tokens.append((access_token, event))
        self.tokens = [token for token in self.tokens if token != access_token]
        return False

    def refresh_access_token(self, access_token: str, *, force: bool = False, event: str = "refresh_access_token"):
        return access_token

    def get_text_access_token(self, excluded_tokens=None):
        excluded = set(excluded_tokens or set())
        for token in self.tokens:
            if token not in excluded:
                return token
        return ""

    def mark_text_used(self, access_token: str):
        self.text_used.append(access_token)


class UpstreamRetryTests(unittest.TestCase):
    def test_chat_requirements_retries_503(self):
        api = OpenAIBackendAPI()
        api.session = FakeSession([
            FakeResponse(503, text=""),
            FakeResponse(200, {"token": "sentinel-token"}),
        ])

        with mock.patch("services.openai_backend_api.time.sleep", lambda _: None):
            requirements = api._get_chat_requirements()

        self.assertEqual(requirements.token, "sentinel-token")
        self.assertEqual(len(api.session.calls), 2)
        self.assertTrue(all(call[2].get("timeout") == FAST_UPSTREAM_TIMEOUT_SECS for call in api.session.calls))

    def test_chat_requirements_uses_fast_attempt_limit(self):
        api = OpenAIBackendAPI()
        api.session = FakeSession([
            FakeResponse(503, text=""),
            FakeResponse(503, text=""),
            FakeResponse(200, {"token": "sentinel-token"}),
        ])

        with mock.patch("services.openai_backend_api.time.sleep", lambda _: None):
            with self.assertRaises(UpstreamHTTPError):
                api._get_chat_requirements()

        self.assertEqual(len(api.session.calls), FAST_UPSTREAM_RETRY_ATTEMPTS)

    def test_account_info_retries_transient_failures_with_longer_timeout(self):
        api = OpenAIBackendAPI("token-a")
        api.session = FakeSession([
            FakeResponse(503, text=""),
            FakeResponse(200, {"email": "alice@example.com"}),
        ])

        with mock.patch("services.openai_backend_api.time.sleep", lambda _: None):
            payload = api._get_me()

        self.assertEqual(payload["email"], "alice@example.com")
        self.assertEqual(len(api.session.calls), ACCOUNT_INFO_RETRY_ATTEMPTS)
        self.assertTrue(all(call[2].get("timeout") == ACCOUNT_INFO_TIMEOUT_SECS for call in api.session.calls))

    def test_codex_image_response_timeout_honors_deadline(self):
        captured: dict[str, float] = {}
        fake_account_service = SimpleNamespace(
            get_account=lambda _token: {"source_type": "codex"},
            _decode_jwt_payload=lambda _token: {},
        )

        def fake_urlopen(_request, timeout):
            captured["timeout"] = timeout
            return FakeUrlopenResponse()

        with mock.patch("services.openai_backend_api.account_service", fake_account_service):
            api = OpenAIBackendAPI("token-a", deadline=time.monotonic() + 100.0)
            with mock.patch("services.openai_backend_api.urllib.request.urlopen", fake_urlopen):
                events = list(api.iter_codex_image_response_events("draw"))

        self.assertEqual(events, [{"type": "response.completed"}])
        self.assertGreater(captured["timeout"], 90.0)
        self.assertLessEqual(captured["timeout"], 100.0)

    def test_codex_image_response_does_not_start_after_deadline(self):
        fake_account_service = SimpleNamespace(
            get_account=lambda _token: {"source_type": "codex"},
            _decode_jwt_payload=lambda _token: {},
        )

        with mock.patch("services.openai_backend_api.account_service", fake_account_service):
            api = OpenAIBackendAPI("token-a", deadline=time.monotonic() - 1.0)
            with mock.patch("services.openai_backend_api.urllib.request.urlopen") as urlopen:
                with self.assertRaises(ImageRequestTimeoutError):
                    list(api.iter_codex_image_response_events("draw"))

        urlopen.assert_not_called()

    def test_image_pool_switches_account_on_transient_503(self):
        fake_accounts = FakeAccountService(["token-a", "token-b"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise UpstreamHTTPError("auth_chat_requirements", 503, "")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="test",
            )))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(fake_accounts.image_results, [("token-a", False), ("token-b", True)])

    def test_image_pool_switches_account_on_invalid_token_before_output(self):
        fake_accounts = FakeAccountService(["token-a", "token-b"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise RuntimeError("token invalidated (/backend-api/conversation)")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="test",
            )))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(fake_accounts.image_results, [("token-a", False), ("token-b", True)])
        self.assertEqual(fake_accounts.removed_invalid_tokens, [("token-a", "image_stream")])

    def test_image_pool_switches_account_on_wrapped_invalid_token_error(self):
        fake_accounts = FakeAccountService(["token-a", "token-b"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise ImageGenerationError(
                    "upstream authentication token is invalid",
                    status_code=401,
                    error_type="invalid_request_error",
                    code="invalid_token",
                )
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="test",
            )))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(fake_accounts.image_results, [("token-a", False), ("token-b", True)])
        self.assertEqual(fake_accounts.removed_invalid_tokens, [("token-a", "image_stream")])

    def test_image_pool_returns_busy_message_when_all_accounts_503(self):
        fake_accounts = FakeAccountService(["token-a"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            raise UpstreamHTTPError("/backend-api/conversation/test", 503, "")
            yield

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs), \
             mock.patch.object(conversation, "TRANSIENT_IMAGE_RESCUE_WINDOW_SECS", 0):
            with self.assertRaises(ImageGenerationError) as ctx:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                    model="gpt-image-2",
                    prompt="test",
                )))

        self.assertEqual(str(ctx.exception), CHANNEL_BUSY_MESSAGE)

    def test_bad_response_status_code_504_is_transient(self):
        exc = RuntimeError("status_code=504, bad response status code 504")

        self.assertTrue(is_retriable_upstream_error(exc))
        self.assertEqual(public_error_message(exc), CHANNEL_BUSY_MESSAGE)

    def test_image_poll_fast_fails_repeated_504(self):
        api = OpenAIBackendAPI("token-a")
        api.session = FakeSession([
            FakeResponse(200, {"tasks": []}),
            FakeResponse(504, text=""),
            FakeResponse(200, {"tasks": []}),
            FakeResponse(504, text=""),
        ])
        original_initial_wait = backend_config.data.get("image_poll_initial_wait_secs")
        original_interval = backend_config.data.get("image_poll_interval_secs")
        backend_config.data["image_poll_initial_wait_secs"] = 0
        backend_config.data["image_poll_interval_secs"] = 0.5
        try:
            with mock.patch("services.openai_backend_api.time.sleep", lambda _: None):
                with self.assertRaises(ImagePollTimeoutError) as ctx:
                    api._poll_image_results("conv-504", timeout_secs=120)
        finally:
            if original_initial_wait is None:
                backend_config.data.pop("image_poll_initial_wait_secs", None)
            else:
                backend_config.data["image_poll_initial_wait_secs"] = original_initial_wait
            if original_interval is None:
                backend_config.data.pop("image_poll_interval_secs", None)
            else:
                backend_config.data["image_poll_interval_secs"] = original_interval

        self.assertIn("HTTP 504", str(ctx.exception))
        self.assertEqual(getattr(ctx.exception, "conversation_id", ""), "conv-504")
        conversation_calls = [
            call for call in api.session.calls
            if "/backend-api/conversation/conv-504" in call[1]
        ]
        self.assertEqual(len(conversation_calls), 2)

    def test_image_pool_retries_poll_timeout_after_progress_only(self):
        fake_accounts = FakeAccountService(["token-a", "token-b"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                yield ImageOutput(kind="progress", model=request.model, index=index, total=total, text="starting")
                raise ImagePollTimeoutError("ChatGPT 生图轮询连续返回 HTTP 504，已提前超时。")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="test",
            )))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(fake_accounts.image_results, [("token-a", False), ("token-b", True)])

    def test_image_text_reply_poll_uses_configured_timeout(self):
        timeouts: list[float] = []

        class FakeBackend:
            def _poll_image_results(self, conversation_id, timeout_secs, initial_file_ids=None, initial_sediment_ids=None):
                timeouts.append(timeout_secs)
                raise ImagePollTimeoutError("poll timeout")

            def resolve_conversation_image_urls(self, *args, **kwargs):
                return []

        request = ConversationRequest(model="gpt-image-2", prompt="test")
        original_timeout = conversation.config.data.get("image_poll_timeout_secs")
        conversation.config.data["image_poll_timeout_secs"] = 100
        try:
            with mock.patch.object(
                conversation,
                "conversation_events",
                lambda *args, **kwargs: iter([
                    {
                        "conversation_id": "conv-1",
                        "file_ids": [],
                        "sediment_ids": [],
                        "text": '{"referenced_image_ids":["file-a"]}',
                        "turn_use_case": "image gen",
                    }
                ]),
            ):
                outputs = list(conversation.stream_image_outputs(FakeBackend(), request))
        finally:
            if original_timeout is None:
                conversation.config.data.pop("image_poll_timeout_secs", None)
            else:
                conversation.config.data["image_poll_timeout_secs"] = original_timeout

        self.assertEqual(timeouts, [100])
        self.assertEqual(outputs[0].kind, "message")

    def test_image_pool_releases_account_and_sanitizes_poll_timeout(self):
        fake_accounts = FakeAccountService(["token-a"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            raise ImagePollTimeoutError("ChatGPT 生图超时（已等待 120 秒）。")
            yield

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs):
            with self.assertRaises(ImageGenerationError) as ctx:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                    model="gpt-image-2",
                    prompt="test",
                )))

        self.assertEqual(str(ctx.exception), conversation.IMAGE_POLL_TIMEOUT_MESSAGE)
        self.assertEqual(ctx.exception.code, "upstream_timeout")
        self.assertEqual(fake_accounts.image_results, [("token-a", False)])

    def test_image_pool_deadline_stops_before_account_lookup(self):
        fake_accounts = FakeAccountService(["token-a"])

        with mock.patch.object(conversation, "account_service", fake_accounts):
            with self.assertRaises(ImageGenerationError) as ctx:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                    model="gpt-image-2",
                    prompt="test",
                    deadline=time.monotonic() - 1,
                )))

        self.assertEqual(str(ctx.exception), conversation.IMAGE_POLL_TIMEOUT_MESSAGE)
        self.assertEqual(ctx.exception.code, "upstream_timeout")
        self.assertEqual(fake_accounts.selected_image_tokens, [])

    def test_image_pool_enters_rescue_after_initial_probe_limit(self):
        fake_accounts = FakeAccountService(["token-a", "token-b", "token-c"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            raise UpstreamHTTPError("auth_chat_requirements", 503, "")
            yield

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs), \
             mock.patch.object(conversation, "TRANSIENT_IMAGE_RESCUE_WINDOW_SECS", 0):
            with self.assertRaises(ImageGenerationError) as ctx:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                    model="gpt-image-2",
                    prompt="test",
                )))

        self.assertEqual(str(ctx.exception), CHANNEL_BUSY_MESSAGE)
        self.assertEqual(fake_accounts.image_results, [("token-a", False), ("token-b", False), ("token-c", False)])

    def test_image_pool_rescue_window_can_recover_after_initial_failures(self):
        fake_accounts = FakeAccountService(["token-a", "token-b", "token-c"])
        calls = 0

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise UpstreamHTTPError("auth_chat_requirements", 503, "")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs), \
             mock.patch.object(conversation, "time") as fake_time:
            fake_time.time.return_value = 1000.0
            fake_time.sleep.side_effect = lambda _: None
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="test",
            )))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(calls, 4)
        self.assertEqual(fake_accounts.image_results[-1], ("token-a", True))

    def test_image_pool_sanitizes_transient_account_lookup_failure(self):
        fake_accounts = SimpleNamespace(
            get_available_access_token=lambda excluded_tokens=None, deadline=None, **kwargs: (_ for _ in ()).throw(
                UpstreamHTTPError("/backend-api/me", 503, "")
            )
        )

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "TRANSIENT_IMAGE_RESCUE_WINDOW_SECS", 0):
            with self.assertRaises(ImageGenerationError) as ctx:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                    model="gpt-image-2",
                    prompt="test",
                )))

        self.assertEqual(str(ctx.exception), CHANNEL_BUSY_MESSAGE)

    def test_text_stream_switches_account_on_transient_503(self):
        fake_accounts = FakeAccountService(["token-a", "token-b"])

        def fake_conversation_events(backend, **kwargs):
            if backend.access_token == "token-a":
                raise UpstreamHTTPError("/backend-api/conversation", 503, "")
            yield {"type": "conversation.delta", "delta": "ok"}

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "conversation_events", fake_conversation_events):
            chunks = list(conversation.stream_text_deltas(
                SimpleNamespace(access_token="token-a"),
                ConversationRequest(model="auto", messages=[{"role": "user", "content": "hi"}]),
            ))

        self.assertEqual(chunks, ["ok"])
        self.assertEqual(fake_accounts.text_used, ["token-b"])


if __name__ == "__main__":
    unittest.main()
