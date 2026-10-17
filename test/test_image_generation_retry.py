from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.config import ConfigStore
from services.openai_backend_api import ImagePollTimeoutError
from services.protocol import conversation
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    is_retryable_image_failure,
)
from utils.helper import CHANNEL_BUSY_MESSAGE, IMAGE_RETRY_MESSAGE

CONTENT_POLICY_MESSAGE = (
    "非常抱歉，生成的图片可能违反了关于裸露、色情或情色内容的防护限制。"
    "如果你认为此判断有误，请重试或修改提示语。"
)


class FakeAccountService:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.image_results: list[tuple[str, bool]] = []
        self.selected_image_tokens: list[str] = []

    def get_available_access_token(self, excluded_tokens=None, deadline=None, **kwargs):
        excluded = set(excluded_tokens or set())
        for token in self.tokens:
            if token not in excluded:
                self.selected_image_tokens.append(token)
                return token
        raise RuntimeError("no available image quota")

    def has_available_image_account(self, excluded_tokens=None, **kwargs):
        excluded = set(excluded_tokens or set())
        return any(token not in excluded for token in self.tokens)

    def image_inflight_count(self, access_token: str) -> int:
        return 0

    def image_handshake_diagnostics(self, access_token: str) -> dict:
        return {}

    def mark_image_mainline_rejected(self, access_token: str) -> None:
        return None

    def get_account(self, access_token: str):
        return {"email": f"{access_token}@example.test"}

    def mark_image_result(self, access_token: str, success: bool):
        self.image_results.append((access_token, success))

    def remove_invalid_token(self, access_token: str, event: str):
        self.tokens = [token for token in self.tokens if token != access_token]
        return False

    def refresh_access_token(self, access_token: str, *, force: bool = False, event: str = "refresh_access_token"):
        return access_token


class RetryableFailureClassifierTests(unittest.TestCase):
    def test_known_failures_are_retryable(self):
        for message in (
            IMAGE_RETRY_MESSAGE,
            "upstream authentication token is invalid",
            "当前生图任务处理超时，请稍后重试",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_retryable_image_failure(message))

    def test_content_policy_and_other_failures_are_not_retryable(self):
        for message in (
            CONTENT_POLICY_MESSAGE,
            CHANNEL_BUSY_MESSAGE,
            "no available image quota",
            "",
            None,
        ):
            with self.subTest(message=message):
                self.assertFalse(is_retryable_image_failure(message))


class ConfigRetryCountTests(unittest.TestCase):
    def test_retry_count_defaults_to_disabled_and_normalizes_on_save(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            path.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = ConfigStore(path)

            self.assertEqual(store.image_generation_retry_count, 0)
            self.assertEqual(store.get()["image_generation_retry_count"], 0)

            store.update({"image_generation_retry_count": ""})
            self.assertEqual(store.image_generation_retry_count, 0)

            store.update({"image_generation_retry_count": "3"})
            self.assertEqual(store.image_generation_retry_count, 3)

            store.update({"image_generation_retry_count": -2})
            self.assertEqual(store.image_generation_retry_count, 0)


class GenerationRetryTests(unittest.TestCase):
    def _patches(self, tokens: list[str], stream_fn, retry_count: int):
        fake_accounts = FakeAccountService(tokens)
        fake_config = SimpleNamespace(image_generation_retry_count=retry_count)
        patcher = mock.patch.multiple(
            conversation,
            account_service=fake_accounts,
            config=fake_config,
            stream_image_outputs=stream_fn,
        )
        return patcher, fake_accounts

    def _run(self, tokens: list[str], stream_fn, retry_count: int, *, message_as_error: bool = True):
        patcher, fake_accounts = self._patches(tokens, stream_fn, retry_count)
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=message_as_error,
            )))
        return outputs, fake_accounts

    def _run_expecting_error(self, tokens: list[str], stream_fn, retry_count: int):
        patcher, fake_accounts = self._patches(tokens, stream_fn, retry_count)
        with patcher, self.assertRaises(ImageGenerationError) as raised:
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
            )))
        return raised.exception, fake_accounts

    @staticmethod
    def _message_then_result(failure_text: str):
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                yield ImageOutput(
                    kind="message",
                    model=request.model,
                    index=index,
                    total=total,
                    text=failure_text,
                )
                return
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])
        return stream_fn

    def test_retry_switches_account_on_generic_generation_failure(self):
        outputs, accounts = self._run(["token-a", "token-b"], self._message_then_result(IMAGE_RETRY_MESSAGE), 1)

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])
        self.assertEqual(accounts.image_results, [("token-a", False), ("token-b", True)])

    def test_empty_retry_count_does_not_retry(self):
        failure, accounts = self._run_expecting_error(
            ["token-a", "token-b"], self._message_then_result(IMAGE_RETRY_MESSAGE), 0,
        )

        self.assertEqual(str(failure), IMAGE_RETRY_MESSAGE)
        self.assertEqual(failure.status_code, 400)
        self.assertEqual(accounts.selected_image_tokens, ["token-a"])

    def test_content_policy_message_never_retries(self):
        failure, accounts = self._run_expecting_error(
            ["token-a", "token-b"], self._message_then_result(CONTENT_POLICY_MESSAGE), 3,
        )

        self.assertEqual(str(failure), CONTENT_POLICY_MESSAGE)
        self.assertEqual(failure.code, "content_policy_violation")
        self.assertEqual(accounts.selected_image_tokens, ["token-a"])

    def test_retry_switches_account_on_poll_timeout(self):
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise ImagePollTimeoutError("poll timed out")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        # 把内置的轮询超时换号次数归零，单独验证新配置项的重试行为。
        with mock.patch.object(conversation, "MAX_POLL_TIMEOUT_RETRIES", 0):
            outputs, accounts = self._run(["token-a", "token-b"], stream_fn, 1)

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])

    def test_retry_is_bounded_by_configured_count(self):
        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(
                kind="message",
                model=request.model,
                index=index,
                total=total,
                text=IMAGE_RETRY_MESSAGE,
            )

        failure, accounts = self._run_expecting_error(["token-a", "token-b", "token-c"], stream_fn, 2)

        self.assertEqual(str(failure), IMAGE_RETRY_MESSAGE)
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b", "token-c"])
        self.assertEqual(failure.image_diagnostics["attempts"][-1]["account_email"], "token-c@example.test")


if __name__ == "__main__":
    unittest.main()
