from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import services.log_service as log_service_module
from services.config import ConfigStore
from services.log_service import LoggedCall, LogService
from services.openai_backend_api import ImagePollTimeoutError, is_skipped_mainline_error
from services.stats_range import beijing_today
from services.stats_service import ImageStatsService
from services.protocol import conversation
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    is_retryable_image_failure,
)
from utils.helper import CHANNEL_BUSY_MESSAGE, IMAGE_RETRY_MESSAGE, UpstreamHTTPError

CONTENT_POLICY_MESSAGE = (
    "非常抱歉，生成的图片可能违反了关于裸露、色情或情色内容的防护限制。"
    "如果你认为此判断有误，请重试或修改提示语。"
)


class FakeAccountService:
    def __init__(self, tokens: list[str], conduit_busy: set[str] | None = None):
        self.tokens = tokens
        self.image_results: list[tuple[str, bool]] = []
        self.selected_image_tokens: list[str] = []
        # 模拟「这个号的 conduit 窗口正被别的请求占着」。
        self.conduit_busy = set(conduit_busy or set())
        self.handshake_acquired: list[str] = []
        self.handshake_released: list[str] = []
        self.released_slots: list[str] = []

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

    def acquire_image_handshake(self, access_token: str, timeout: float | None = None) -> bool:
        if access_token in self.conduit_busy:
            return False
        self.handshake_acquired.append(access_token)
        return True

    def release_image_handshake(self, access_token: str) -> None:
        if access_token:
            self.handshake_released.append(access_token)

    def release_image_slot(self, access_token: str) -> None:
        if access_token:
            self.released_slots.append(access_token)

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

    def test_collect_does_not_repeat_chunk_trails(self):
        # 每张图的每个 chunk（progress / result）都带一份完整轨迹。以前按 chunk
        # 逐个拼接，同一张图 A→B 的轨迹会被拼成 A→B→A→B…，日志里一次请求能
        # 冒出几十个「账号」。这里模拟两张图各有若干 chunk，只应留下 4 个账号。
        outputs = []
        for index, trail in (
            (1, ["a1@example.test", "b1@example.test"]),
            (2, ["a2@example.test", "b2@example.test"]),
        ):
            for _ in range(5):
                outputs.append(ImageOutput(
                    kind="progress", model="gpt-image-2", index=index, total=2,
                    account_trail=list(trail),
                ))
            outputs.append(ImageOutput(
                kind="result", model="gpt-image-2", index=index, total=2,
                data=[{"url": f"u{index}"}], account_trail=list(trail),
            ))

        result = conversation.collect_image_outputs(outputs)

        self.assertEqual(
            result["_account_trail"],
            ["a1@example.test", "b1@example.test", "a2@example.test", "b2@example.test"],
        )

    def test_collect_image_outputs_keeps_upstream_model_slug(self):
        outputs = [conversation.ImageOutput(
            kind="result", model="gpt-image-2", index=1, total=1, data=[{"url": "ok"}],
            upstream_model_slug="i-mini-m",
        )]

        result = conversation.collect_image_outputs(outputs)

        self.assertEqual(result["_upstream_model_slug"], "i-mini-m")

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

    def test_unsupported_app_response_is_not_retried_and_does_not_mark_account_failed(self):
        def stream_fn(backend, request, index=1, total=1):
            raise conversation.ImageUnsupportedAppResponseError(
                conversation.UNSUPPORTED_APP_RESPONSE_MESSAGE,
                code=conversation.UNSUPPORTED_APP_RESPONSE_CODE,
            )
            yield  # pragma: no cover

        failure, accounts = self._run_expecting_error(["token-a", "token-b"], stream_fn, 3)

        self.assertEqual(failure.code, conversation.UNSUPPORTED_APP_RESPONSE_CODE)
        # 应用响应不是账号故障：不换号、不增加失败计数，只把槽位还回去。
        self.assertEqual(accounts.selected_image_tokens, ["token-a"])
        self.assertEqual(accounts.image_results, [])
        self.assertEqual(accounts.released_slots, ["token-a"])
        self.assertEqual(failure.image_diagnostics["attempts"][0]["outcome"], "unsupported_app_response")

    def test_unsupported_app_message_is_not_retried(self):
        refusal = (
            "当前聊天中我无法直接生成图片，但可以先为你整理好适用于图像生成工具的最终提示词。"
        )

        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(
                kind="message", model=request.model, index=index, total=total,
                text=refusal, conversation_id="conv-refusal",
            )

        failure, accounts = self._run_expecting_error(["token-a", "token-b"], stream_fn, 3)

        self.assertEqual(failure.code, "unsupported_app_response")
        self.assertEqual(str(failure), conversation.UNSUPPORTED_APP_RESPONSE_MESSAGE)
        self.assertEqual(accounts.selected_image_tokens, ["token-a"])
        self.assertEqual(accounts.image_results, [])
        self.assertEqual(accounts.released_slots, ["token-a"])
        self.assertEqual((failure.body or {}).get("upstream_text"), refusal)

    def test_text_reply_is_not_reported_as_content_policy_violation(self):
        prose = "已将圣诞镂空桌布融入美式客厅。你希望洞里的宠物保持这只博美犬吗？"

        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(
                kind="message",
                model=request.model,
                index=index,
                total=total,
                text=prose,
                text_reply=True,
                conversation_id="conv-text",
            )

        # 文本回复会换号重试（最多 3 次），所以号池里要留够 4 个号，
        # 否则会先撞到「no available image quota」而不是文本回复的最终错误。
        failure, _ = self._run_expecting_error(
            ["token-a", "token-b", "token-c", "token-d"], stream_fn, 0,
        )

        # 模型正文不是错误文案：对外给明确分类，原文只留在日志里（upstream_error.body）
        self.assertEqual(failure.code, "upstream_text_reply")
        self.assertEqual(failure.status_code, 502)
        self.assertNotIn(prose, str(failure))
        self.assertEqual((failure.body or {}).get("upstream_text"), prose)

    def test_text_reply_attempt_is_labelled_as_text_reply_not_generation_error(self):
        prose = "已将圣诞镂空桌布融入美式客厅。"

        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(
                kind="message",
                model=request.model,
                index=index,
                total=total,
                text=prose,
                text_reply=True,
                conversation_id="conv-text",
            )

        failure, _ = self._run_expecting_error(
            ["token-a", "token-b", "token-c", "token-d"], stream_fn, 0,
        )

        # 「只回了文本」不是生成失败：尝试卡片要看到具体是哪一步、原话预览。
        attempts = (failure.image_diagnostics or {})["attempts"]
        for attempt in attempts:
            self.assertEqual(attempt["outcome"], "text_reply")
            self.assertIn("上游只回了文本", attempt["error"])
            self.assertIn(prose, attempt["error"])
        summary = (failure.image_diagnostics or {})["attempts_summary"]
        self.assertIn("text_reply", summary)
        self.assertNotIn("generation_error", summary)

    def test_text_reply_retries_after_progress_events(self):
        prose = "已将圣诞镂空桌布融入美式客厅。"

        def stream_fn(backend, request, index=1, total=1):
            # 真实上游流在最终正文之前会先吐一串 conversation.delta/event（progress）。
            # progress 只是缓冲的增量，失败时整段丢弃，不能拿它拦住换号重试。
            yield ImageOutput(
                kind="progress", model=request.model, index=index, total=total,
                text="", upstream_event_type="conversation.delta",
            )
            if backend.access_token == "token-a":
                yield ImageOutput(
                    kind="message", model=request.model, index=index, total=total,
                    text=prose, text_reply=True, conversation_id="conv-text",
                )
                return
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        outputs, accounts = self._run(["token-a", "token-b"], stream_fn, 0)

        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])

    def test_text_reply_retry_reuses_request_budget(self):
        seen_deadlines = []

        def stream_fn(backend, request, index=1, total=1):
            seen_deadlines.append(request.deadline)
            if backend.access_token == "token-a":
                yield ImageOutput(
                    kind="message", model=request.model, index=index, total=total,
                    text="已生成。", text_reply=True, conversation_id="conv-text",
                )
                return
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher, _ = self._patches(["token-a", "token-b"], stream_fn, 0)
        deadline = time.monotonic() + 0.5
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True, deadline=deadline,
            )))

        # 重试只花本次请求的剩余预算，不再续期：第二次尝试拿到的是同一个 deadline。
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(len(seen_deadlines), 2)
        self.assertEqual(seen_deadlines[1], deadline)

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

    def test_skipped_mainline_text_is_not_reported_as_text_reply(self):
        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(
                kind="message",
                model=request.model,
                index=index,
                total=total,
                text='{"skipped_mainline":true}',
                conversation_id="conv-1",
                plumbing_error=True,
            )

        failure, _ = self._run_expecting_error(["token-a"], stream_fn, 0)

        # 管道错误伪装成正文：不能报成「模型只回了文字」，更不能报成内容违规
        self.assertEqual(failure.code, "upstream_error")
        self.assertEqual(failure.status_code, 502)
        self.assertEqual(str(failure), IMAGE_RETRY_MESSAGE)
        self.assertIs((failure.body or {}).get("skipped_mainline"), True)

    def test_plumbing_detection_keeps_tool_params_as_text_reply(self):
        self.assertIsNotNone(conversation._plumbing_message_body('{"skipped_mainline":true}'))
        self.assertIsNone(conversation._plumbing_message_body('{"size":"1024x1024","n":1}'))
        self.assertIsNone(conversation._plumbing_message_body("已将圣诞镂空桌布融入美式客厅。"))

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

    def test_invalid_token_attempt_is_labelled_invalid_token(self):
        # 401/token 失效不能再落到通用的 upstream_error 标签上。
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise ImageGenerationError(
                    "token invalidated (/backend-api/files)",
                    status_code=401,
                    error_type="invalid_request_error",
                    code="invalid_token",
                )
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        outputs, accounts = self._run(["token-a", "token-b"], stream_fn, 0)
        attempts = conversation.collect_image_outputs(outputs)["_attempts"]

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])
        self.assertEqual(accounts.image_results, [("token-a", False), ("token-b", True)])
        self.assertEqual(attempts[0]["outcome"], "invalid_token")
        self.assertEqual(attempts[1]["outcome"], "success")

    def test_no_tool_event_switches_account_without_polling_budget(self):
        # 主链路没起图（SSE 没 id、也没图片工具事件）和轮询超时共用换号路径，
        # 但不该把预算花在轮询上；不续预算也要能立刻换到下一个号。
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise conversation.ImageNoToolEventError("no image tool event")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        outputs, accounts = self._run(["token-a", "token-b"], stream_fn, 0)

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(accounts.selected_image_tokens, ["token-a", "token-b"])
        timeline = conversation.collect_image_outputs(outputs)["_image_timeline"]
        retry_steps = [step for group in timeline for step in group["steps"] if step["step"] == "retry"]
        self.assertTrue(any(step.get("label") == "主链路未出图换号" for step in retry_steps))
        self.assertTrue(any(step.get("reason") == "no_image_tool_event" for step in retry_steps))

    def test_local_timeout_retry_is_labelled_as_timeout_not_upstream_error(self):
        # 本地预算耗尽时长这样说「本次尝试超时」：它和上游真的返回错误是两回事，
        # 时间线里混着写会让人以为渠道故障。
        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise ImagePollTimeoutError("image request deadline exceeded")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        # 内置的轮询换号会先接管；这里归零，直接验证配置的「生成失败重试」文案。
        with mock.patch.object(conversation, "MAX_POLL_TIMEOUT_RETRIES", 0):
            outputs, _ = self._run(["token-a", "token-b"], stream_fn, 1)

        timeline = conversation.collect_image_outputs(outputs)["_image_timeline"]
        retry_steps = [step for group in timeline for step in group["steps"] if step["step"] == "retry"]
        self.assertEqual(len(retry_steps), 1)
        self.assertIn("本次尝试超时", retry_steps[0]["detail"])
        self.assertNotIn("上游返回", retry_steps[0]["detail"])

    def test_poll_timeout_after_deadline_expires_does_not_retry(self):
        # 轮询超时通常正好把原始预算耗尽。以前会续一份新预算再换号重试，
        # 单次 120s + 重试 3 次最坏要等 480s；现在预算耗尽就不再重试。
        import time as _time

        def stream_fn(backend, request, index=1, total=1):
            _time.sleep(0.2)
            raise ImagePollTimeoutError("poll timed out")

        patcher, accounts = self._patches(["token-a", "token-b"], stream_fn, 1)
        deadline = _time.monotonic() + 0.05
        with patcher, self.assertRaises(ImageGenerationError):
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
                deadline=deadline,
            )))

        self.assertEqual(accounts.selected_image_tokens, ["token-a"])

    def test_timeout_retry_stops_when_budget_is_exhausted(self):
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
        self.assertEqual(diagnostics.get("retry_stopped"), "request_deadline")
        self.assertNotIn("retry_deadline_extended_secs", diagnostics)

    def test_attempt_end_reports_budget_exhausted_for_timeout(self):
        # 超时后预算耗尽、不再重试时，尝试结束必须写「预算已耗尽」，
        # 而不是一个虚假的「预算剩余」。
        import time as _time

        def stream_fn(backend, request, index=1, total=1):
            _time.sleep(0.2)
            raise ImagePollTimeoutError("poll timed out")

        patcher, _ = self._patches(["token-a", "token-b"], stream_fn, 1)
        with mock.patch.object(conversation, "MAX_POLL_TIMEOUT_RETRIES", 0), patcher, \
                self.assertRaises(ImageGenerationError) as raised:
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                message_as_error=True,
                deadline=_time.monotonic() + 0.05,
            )))

        timeline = (raised.exception.image_diagnostics or {}).get("timeline") or []
        attempt_ends = [step for step in timeline if step.get("step") == "attempt_end"]

        self.assertEqual(len(attempt_ends), 1)
        self.assertIn("预算已耗尽", attempt_ends[0]["detail"])
        self.assertNotIn("预算剩余", attempt_ends[0]["detail"])

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

    def test_conduit_window_busy_switches_account_immediately(self):
        # 同号并发时 conduit 窗口（prepare→mainline）被别的请求占着：直接换号，
        # 不排队等待，也不能把这个号记成失败（它压根没发出去请求）。
        started = time.monotonic()
        stream_tokens: list[str] = []

        def stream_fn(backend, request, index=1, total=1):
            stream_tokens.append(backend.access_token)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        fake_accounts = FakeAccountService(["token-a", "token-b"], conduit_busy={"token-a"})
        with mock.patch.multiple(
            conversation,
            account_service=fake_accounts,
            config=SimpleNamespace(image_generation_retry_count=0),
            stream_image_outputs=stream_fn,
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))
        elapsed = time.monotonic() - started

        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(fake_accounts.selected_image_tokens, ["token-a", "token-b"])
        # 没有在 token-a 上起 backend，也没有把它记成失败；只把槽位还回去。
        self.assertEqual(stream_tokens, ["token-b"])
        self.assertEqual(fake_accounts.released_slots, ["token-a"])
        self.assertEqual(fake_accounts.handshake_acquired, ["token-b"])
        self.assertEqual(fake_accounts.image_results, [("token-b", True)])
        self.assertLess(elapsed, 1.0)

    def test_conduit_window_busy_everywhere_falls_back_to_waiting(self):
        # 号池里所有可用号的窗口都忙：换一圈拿不到就别再空转，回到原来的排队行为。
        stream_tokens: list[str] = []

        def stream_fn(backend, request, index=1, total=1):
            stream_tokens.append(backend.access_token)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        tokens = ["token-a", "token-b", "token-c"]
        fake_accounts = FakeAccountService(tokens, conduit_busy=set(tokens))
        with mock.patch.multiple(
            conversation,
            account_service=fake_accounts,
            config=SimpleNamespace(image_generation_retry_count=0),
            stream_image_outputs=stream_fn,
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))

        self.assertEqual(outputs[-1].kind, "result")
        # 每个号只在「换号」阶段被选中一次，之后停止探测、在其中一个号上正常尝试。
        self.assertEqual(sorted(set(fake_accounts.selected_image_tokens)), tokens)
        self.assertEqual(len(stream_tokens), 1)
        self.assertEqual(len(fake_accounts.released_slots), len(tokens))

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


class SkippedMainlineTextStreamTests(unittest.TestCase):
    def test_skipped_mainline_text_becomes_upstream_400_with_raw_stream(self):
        """上游 400 {"skipped_mainline":true} 被 SSE 塞进正文时，要按 400 失败，
        并把最近的上游 SSE 原文带进异常，日志详情里能看到上游到底回了什么。"""

        request = ConversationRequest(
            model="gpt-image-2",
            prompt="draw a cat",
            images=["data:image/png;base64,AA=="],
        )
        event = {
            "type": "conversation.done",
            "text": '{"skipped_mainline":true}',
            "conversation_id": "conv-1",
            "file_ids": [],
            "sediment_ids": [],
            "blocked": False,
            "tool_invoked": True,
            "turn_use_case": "image gen",
            "raw": {"type": "error", "message": '{"skipped_mainline":true}'},
        }

        with mock.patch.object(conversation, "conversation_events", return_value=iter([event])):
            with self.assertRaises(UpstreamHTTPError) as raised:
                list(conversation.stream_image_outputs(SimpleNamespace(), request))

        error = raised.exception
        self.assertTrue(is_skipped_mainline_error(error))
        self.assertEqual(error.status_code, 400)
        self.assertIn("skipped_mainline", error.body["stream_tail"][0]["payload"])


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

    def test_upstream_model_requested_and_actual_are_logged(self):
        outputs = [ImageOutput(
            kind="result", model="gpt-image-2", index=1, total=1, data=[{"url": "ok"}],
            upstream_model_slug="gpt-5-5",
        )]
        with mock.patch("services.log_service.config",
                        SimpleNamespace(default_upstream_model_name="gpt-5-5")):
            detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))

        self.assertEqual(detail["upstream_model_slug"], "gpt-5-5")
        self.assertEqual(detail["upstream_model_requested"], "gpt-5-5")

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

    def test_success_log_contains_step_timeline_with_durations(self):
        """时间线要能回答：每一步干嘛了、什么时候、花了多久、为什么。"""

        def stream_fn(backend, request, index=1, total=1):
            # 模拟 backend 阶段打点：先 bootstrap 40ms，再发主链路 10ms。
            now = time.monotonic()
            backend.image_phases = {
                "bootstrapping": now - 0.05,
                "starting_generation": now - 0.01,
            }
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a"]),
            config=SimpleNamespace(image_generation_retry_count=0),
            stream_image_outputs=stream_fn,
        )
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))
        groups = detail["image_timeline"]
        self.assertEqual(len(groups), 1)
        steps = groups[0]["steps"]
        steps_by_name = {step["step"]: step for step in steps}

        self.assertIn("request_start", steps_by_name)
        self.assertEqual(steps_by_name["select_account"]["account_email"], "token-a@example.test")
        self.assertEqual(steps_by_name["conduit_window"]["outcome"], "acquired")
        # 阶段耗时来自 backend 的打点，按真实时间排在时间线里。
        self.assertGreaterEqual(steps_by_name["phase_bootstrapping"]["dur_ms"], 30)
        self.assertIn("at_ms", steps_by_name["phase_bootstrapping"])
        # 每一步都有中文名，日志详情不用再看原始 key。
        self.assertTrue(all(step.get("label") for step in steps))
        # 时间线按发生时间排序。
        self.assertEqual([step["at_ms"] for step in steps], sorted(step["at_ms"] for step in steps))
        self.assertEqual(steps_by_name["attempt_end"]["outcome"], "success")

    def test_failure_log_contains_step_timeline(self):
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

        detail = self._log_call("调用失败", status="failed", error=str(failure), exc=failure)
        steps = detail["image_timeline"][0]["steps"]
        step_names = [step["step"] for step in steps]

        self.assertIn("request_start", step_names)
        self.assertIn("select_account", step_names)
        self.assertIn("retry", step_names)
        self.assertIn("attempt_end", step_names)
        self.assertNotIn("_image_timeline", detail)

    def test_conduit_busy_switch_is_visible_in_timeline(self):
        def stream_fn(backend, request, index=1, total=1):
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a", "token-b"], conduit_busy={"token-a"}),
            config=SimpleNamespace(image_generation_retry_count=0),
            stream_image_outputs=stream_fn,
        )
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))
        steps = detail["image_timeline"][0]["steps"]
        window_steps = [step for step in steps if step["step"] == "conduit_window"]
        select_steps = [step for step in steps if step["step"] == "select_account"]

        self.assertEqual([step["outcome"] for step in window_steps], ["busy_switch", "acquired"])
        self.assertEqual(window_steps[0]["wait_ms"], 0)
        self.assertEqual(window_steps[0]["account_email"], "token-a@example.test")
        self.assertIn("直接换号", window_steps[0]["detail"])
        self.assertEqual([step["account_email"] for step in select_steps],
                         ["token-a@example.test", "token-b@example.test"])

    def test_parallel_partial_failure_keeps_failed_image_timeline(self):
        """多图并行时某张失败，失败那张也要在日志时间线里有自己的一条。"""

        def stream_fn(backend, request, index=1, total=1):
            if index == 2:
                raise ImageGenerationError(CONTENT_POLICY_MESSAGE)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a", "token-b"]),
            config=SimpleNamespace(image_generation_retry_count=0, image_parallel_generation=True),
            stream_image_outputs=stream_fn,
        )
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True, n=2,
            )))

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))
        groups = {group["index"]: group for group in detail["image_timeline"]}

        self.assertEqual(sorted(groups), [1, 2])
        self.assertEqual(groups[1]["steps"][-1]["outcome"], "success")
        self.assertEqual(groups[2]["steps"][-1]["step"], "attempt_end")
        self.assertEqual(groups[2]["steps"][-1]["outcome"], "generation_error")

    def test_success_log_keeps_full_text_reply_for_expandable_trail(self):
        long_text = "已生成圣诞主题电商实景图，" + "保留红色木屋风格圣诞镂空桌布、" * 8

        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                yield ImageOutput(
                    kind="message", model=request.model, index=index, total=total,
                    text=long_text, text_reply=True,
                )
                return
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a", "token-b"]),
            config=SimpleNamespace(image_generation_retry_count=0),
            stream_image_outputs=stream_fn,
        )
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))
        attempts = detail["attempts"]

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["outcome"], "text_reply")
        # 摘要仍是一行短的（给列表/导出看），完整原文单独进结构化 attempts。
        self.assertIn("上游只回了文本", attempts[0]["error"])
        self.assertEqual(attempts[0]["upstream_text"], long_text)
        self.assertEqual(attempts[1]["outcome"], "success")

    def test_message_retry_logs_raw_upstream_error(self):
        raw_error = (
            '{"error": "upstream rejected this image", "padding": "'
            + "x" * 300
            + '"}'
        )

        def stream_fn(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                yield ImageOutput(
                    kind="message", model=request.model, index=index, total=total,
                    text=raw_error,
                )
                return
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        patcher = mock.patch.multiple(
            conversation,
            account_service=FakeAccountService(["token-a", "token-b"]),
            config=SimpleNamespace(image_generation_retry_count=1),
            stream_image_outputs=stream_fn,
        )
        with patcher:
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2", prompt="draw a cat", message_as_error=True,
            )))

        detail = self._log_call("调用完成", conversation.collect_image_outputs(outputs))
        attempt = detail["attempts"][0]

        self.assertEqual(attempt["outcome"], "message_retry")
        # 日志里不能再是本地兜底文案；error 至少保留原生报错前 200 字，完整原文在 upstream_text。
        self.assertIn("upstream rejected this image", attempt["error"])
        self.assertNotIn("本次生图失败，请重试", attempt["error"])
        self.assertEqual(attempt["upstream_text"], raw_error)
        self.assertIn("upstream rejected this image", detail["attempts_summary"])

    def test_stream_chunks_yield_one_record_and_hide_internal_keys(self):
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/generations", "gpt-image-2", "文生图",
        )
        chunks = [
            {"object": "image.generation.result", "data": [{"url": "ok"}],
             "_account_trail": ["token-a@example.test", "token-b@example.test"],
             "_image_timeline": [{"index": 1, "steps": [{"at_ms": 0, "step": "request_start"}]}],
             "_attempts": [{"n": 1, "account_email": "token-a@example.test", "outcome": "text_reply"}],
             "_attempts_summary": "#1 token-a@example.test poll_timeout → #2 token-b@example.test success"},
        ] * 2

        yielded = list(call.stream(iter(chunks)))

        self.assertTrue(all("_account_trail" not in item for item in yielded))
        self.assertTrue(all("_image_timeline" not in item for item in yielded))
        self.assertTrue(all("_attempts" not in item for item in yielded))
        items = self.service.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"]["account_trail"],
                         "token-a@example.test → token-b@example.test")
        self.assertEqual(
            items[0]["detail"]["image_timeline"][0]["steps"][0]["step"], "request_start",
        )
        # 重复 chunk 里的同一份 attempts 只落一份。
        self.assertEqual(len(items[0]["detail"]["attempts"]), 1)
        self.assertEqual(items[0]["detail"]["attempts"][0]["outcome"], "text_reply")

    def test_repeated_chunk_trails_collapse_to_one_round(self):
        """每个流式 chunk 都会带一份同样的轨迹，不能拼成 A → B → A → B。"""
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/edits", "gpt-image-2", "图生图",
        )
        chunks = [
            {"object": "image.generation.result", "data": [{"url": "ok"}],
             "_account_trail": ["token-a@example.test", "token-b@example.test"]},
        ] * 20

        list(call.stream(iter(chunks)))

        self.assertEqual(
            self.service.list()[0]["detail"]["account_trail"],
            "token-a@example.test → token-b@example.test",
        )

    def test_rotated_chunk_trails_do_not_ping_pong(self):
        """多图并行时副本顺序可能相反（B → A）；和 A → B 拼在一起就是一圈圈重复。"""
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/edits", "gpt-image-2", "图生图",
        )
        chunks = []
        for _ in range(10):
            chunks.append({"_account_trail": ["token-a@example.test", "token-b@example.test"],
                           "data": [{"url": "a"}]})
            chunks.append({"_account_trail": ["token-b@example.test", "token-a@example.test"],
                           "data": [{"url": "b"}]})

        list(call.stream(iter(chunks)))

        self.assertEqual(
            self.service.list()[0]["detail"]["account_trail"],
            "token-a@example.test → token-b@example.test",
        )

    def test_single_cyclic_trail_collapses_to_first_round(self):
        """旧版本会把 chunk 轨迹拼成 A→B→A→B… 一整条；单份轨迹也要能收敛。"""
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/edits", "gpt-image-2", "图生图",
        )
        chunks = [{
            "_account_trail": ["token-a@example.test", "token-b@example.test"] * 13
            + ["token-c@example.test", "token-d@example.test"] * 12,
            "data": [{"url": "a"}],
        }]

        list(call.stream(iter(chunks)))

        self.assertEqual(
            self.service.list()[0]["detail"]["account_trail"],
            "token-a@example.test → token-b@example.test → token-c@example.test → token-d@example.test",
        )

    def test_trails_with_new_accounts_are_still_merged(self):
        """只丢弃「没有新账号」的副本：别的图片确实换了新号时要保留。"""
        call = LoggedCall(
            {"id": "k1", "name": "管理员", "role": "admin"},
            "/v1/images/edits", "gpt-image-2", "图生图",
        )
        chunks = [
            {"_account_trail": ["token-a@example.test", "token-b@example.test"], "data": [{"url": "a"}]},
            {"_account_trail": ["token-c@example.test", "token-d@example.test"], "data": [{"url": "b"}]},
        ]

        list(call.stream(iter(chunks)))

        self.assertEqual(
            self.service.list()[0]["detail"]["account_trail"],
            "token-a@example.test → token-b@example.test → token-c@example.test → token-d@example.test",
        )

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
