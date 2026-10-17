from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import services.log_service as log_service_module
from services.config import ConfigStore
from services.log_service import LoggedCall, LogService
from services.openai_backend_api import ImagePollTimeoutError
from services.stats_range import beijing_today
from services.stats_service import ImageStatsService
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
            '{"skipped_mainline":true}',
            'status_code=400, {"skipped_mainline": true}',
        ):
            with self.subTest(message=message):
                self.assertTrue(is_retryable_image_failure(message))

    def test_content_policy_and_other_failures_are_not_retryable(self):
        for message in (
            CONTENT_POLICY_MESSAGE,
            CHANNEL_BUSY_MESSAGE,
            "no available image quota",
            '{"skipped_mainline":false}',
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

    def test_success_result_carries_account_trail(self):
        outputs, _ = self._run(["token-a", "token-b"], self._message_then_result(IMAGE_RETRY_MESSAGE), 1)

        result = conversation.collect_image_outputs(outputs)

        self.assertEqual(
            result["_account_trail"],
            ["token-a@example.test", "token-b@example.test"],
        )
        self.assertIn("#1 token-a@example.test message_retry", result["_attempts_summary"])
        self.assertIn("#2 token-b@example.test success", result["_attempts_summary"])

    def test_collect_image_outputs_keeps_upscale_postprocess(self):
        outputs = [conversation.ImageOutput(
            kind="result", model="gpt-image-2", index=1, total=1, data=[{"url": "ok"}],
            postprocess={"upscale_target": 4096, "upscale_to_size": "4096x4096",
                         "upscale_ms": 800, "upscale_images": 1},
        )]

        result = conversation.collect_image_outputs(outputs)

        self.assertEqual(result["_postprocess"]["upscale_ms"], 800)
        self.assertEqual(result["_postprocess"]["upscale_to_size"], "4096x4096")

    def test_no_retry_result_keeps_the_old_shape(self):
        outputs, _ = self._run(["token-a"], self._message_then_result(IMAGE_RETRY_MESSAGE), 0,
                               message_as_error=False)

        result = conversation.collect_image_outputs(outputs)

        self.assertNotIn("_account_trail", result)
        self.assertNotIn("_attempts_summary", result)

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

    def test_retry_switches_account_on_skipped_mainline(self):
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise ImageGenerationError(
                    'status_code=400, {"skipped_mainline":true}',
                    status_code=400,
                    code="skipped_mainline",
                )
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        outputs, accounts = self._run(["token-a", "token-b"], stream_fn, 1)

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])
        self.assertEqual(accounts.image_results, [("token-a", False), ("token-b", True)])

    def test_skipped_mainline_does_not_retry_when_retry_count_is_zero(self):
        def stream_fn(backend, request, index=1, total=1):
            raise ImageGenerationError('{"skipped_mainline":true}', status_code=400, code="skipped_mainline")
            yield  # pragma: no cover

        failure, accounts = self._run_expecting_error(["token-a", "token-b"], stream_fn, 0)

        self.assertEqual(accounts.selected_image_tokens, ["token-a"])
        self.assertIn("skipped_mainline", str(failure))

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

    def test_poll_timeout_after_deadline_expires_retries_with_a_fresh_budget(self):
        # 轮询超时通常正好把原始预算耗尽。配置了「生成失败重试次数」时，
        # 换号重试必须能拿到一份新预算，否则 deadline 检查会把它拦死。
        import time as _time

        seen_deadlines: dict[str, float] = {}

        def stream_fn(backend, request, index=1, total=1):
            seen_deadlines[backend.access_token] = request.deadline
            if backend.access_token == "token-a":
                _time.sleep(0.2)
                raise ImagePollTimeoutError("poll timed out")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher, accounts = self._patches(["token-a", "token-b"], stream_fn, 1)
        deadline = _time.monotonic() + 0.05
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
                deadline=deadline,
            )))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])
        self.assertGreater(seen_deadlines["token-b"], deadline)

    def test_retry_deadline_extension_is_recorded_in_diagnostics(self):
        import time as _time

        def stream_fn(backend, request, index=1, total=1):
            _time.sleep(0.2)
            raise ImagePollTimeoutError("poll timed out")

        patcher, _ = self._patches(["token-a", "token-b"], stream_fn, 1)
        with patcher, self.assertRaises(ImageGenerationError) as raised:
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
                deadline=_time.monotonic() + 0.05,
            )))

        diagnostics = raised.exception.image_diagnostics
        self.assertIn("retry_deadline_extended_secs", diagnostics)

    def test_result_is_accepted_after_deadline_expires_during_postprocess(self):
        # 4K 超分在本地 CPU 上跑，可能一直跑到上游轮询预算用完。图片已经拿到手了，
        # 不能因为本地后处理超时就报「生图任务处理超时」、还把结果丢掉。
        import time as _time

        def stream_fn(backend, request, index=1, total=1):
            _time.sleep(0.2)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher, _ = self._patches(["token-a"], stream_fn, 0)
        deadline = _time.monotonic() + 0.05
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
                deadline=deadline,
            )))

        self.assertEqual(outputs[0].kind, "result")

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


class AttemptTrailLogTests(unittest.TestCase):
    """一次调用只落一条日志：重试过的把账号轨迹写进这条记录，只记最终结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = LogService(Path(self._tmp.name) / "logs.jsonl")
        patcher = mock.patch.object(log_service_module, "log_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _run_retry(tokens: list[str], retry_count: int):
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == tokens[-1]:
                yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])
                return
            yield ImageOutput(
                kind="message", model=request.model, index=index, total=total, text=IMAGE_RETRY_MESSAGE,
            )

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(tokens),
            config=SimpleNamespace(image_generation_retry_count=retry_count),
            stream_image_outputs=stream_fn,
        )
        with patcher:
            return list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
            )))

    def _log_call(self, suffix: str, *args, **kwargs):
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/generations", "gpt-image-2", "文生图",
        )
        call.log(suffix, *args, **kwargs)
        items = self.service.list()
        self.assertEqual(len(items), 1)
        return items[0]["detail"]

    def test_retry_success_is_one_record_with_ordered_accounts(self):
        outputs = self._run_retry(["token-a", "token-b"], 1)

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))

        self.assertEqual(detail["status"], "success")
        self.assertEqual(detail["account_trail"], "token-a@example.test → token-b@example.test")
        self.assertIn("#1 token-a@example.test message_retry", detail["attempts_summary"])
        self.assertIn("#2 token-b@example.test success", detail["attempts_summary"])

    def test_retry_failure_is_one_record_with_ordered_accounts(self):
        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=IMAGE_RETRY_MESSAGE)

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a", "token-b", "token-c"]),
            config=SimpleNamespace(image_generation_retry_count=2),
            stream_image_outputs=stream_fn,
        )
        with patcher, self.assertRaises(ImageGenerationError) as raised:
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))
        failure = raised.exception

        detail = self._log_call("调用失败", status="failed", error=str(failure), exc=failure)

        self.assertEqual(detail["status"], "failed")
        self.assertEqual(
            detail["account_trail"],
            "token-a@example.test → token-b@example.test → token-c@example.test",
        )
        self.assertIn("message_retry", detail["attempts_summary"])

    def test_stream_chunks_yield_one_record_and_hide_internal_keys(self):
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/generations", "gpt-image-2", "文生图",
        )
        chunks = [
            {"object": "image.generation.result", "data": [{"url": "ok"}],
             "_account_trail": ["token-a@example.test", "token-b@example.test"],
             "_attempts_summary": "#1 token-a@example.test poll_timeout → #2 token-b@example.test success"},
        ] * 2

        yielded = list(call.stream(iter(chunks)))

        self.assertTrue(all("_account_trail" not in item for item in yielded))
        items = self.service.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"]["account_trail"],
                         "token-a@example.test → token-b@example.test")

    def _stats(self):
        today = beijing_today()
        return ImageStatsService(self.service.path).summary(today, today)["totals"]

    def test_retry_success_counts_once_as_success(self):
        outputs = self._run_retry(["token-a", "token-b"], 1)
        self._log_call("调用完成", conversation.collect_image_outputs(outputs))

        totals = self._stats()

        self.assertEqual((totals["requests"], totals["success"], totals["failed"]), (1, 1, 0))

    def test_retry_failure_counts_once_as_failure(self):
        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=IMAGE_RETRY_MESSAGE)

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a", "token-b"]),
            config=SimpleNamespace(image_generation_retry_count=1),
            stream_image_outputs=stream_fn,
        )
        with patcher, self.assertRaises(ImageGenerationError) as raised:
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))
        failure = raised.exception
        self._log_call("调用失败", status="failed", error=str(failure), exc=failure)

        totals = self._stats()

        self.assertEqual((totals["requests"], totals["success"], totals["failed"]), (1, 0, 1))

    def test_no_retry_log_has_no_trail(self):
        outputs = self._run_retry(["token-a"], 0)

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))

        self.assertNotIn("account_trail", detail)
        self.assertNotIn("attempts_summary", detail)


if __name__ == "__main__":
    unittest.main()
