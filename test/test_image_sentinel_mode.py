"""新版生图握手（sentinel 求解服务 + FNV-1a PoW + 404 账号拒绝）的回归测试。

拆分点：

- ``utils.pow``：FNV-1a + avalanche 的 hash、在新字段表上解 PoW；
- ``services.sentinel_solver_client``：求解服务的调用与退路；
- ``services.openai_backend_api``：finalize 字段名、TLS 指纹、404 账号拒绝的识别；
- ``services.protocol.conversation``：把 404 账号拒绝变成换号信号；
- ``services.account_service``：被拒账号的本地冷却。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

import pybase64

from services import openai_backend_api as backend_module
from services import sentinel_solver_client as client_module
from services.account_service import ACCOUNT_REJECT_COOLDOWN_SECS, AccountService
from services.config import DEFAULT_TIMEZONE, DEFAULT_TIMEZONE_OFFSET_MIN, config
from services.openai_backend_api import (
    DEFAULT_IMPERSONATE,
    LEGACY_IMPERSONATE,
    ChatRequirements,
    ImageAccountRejectedError,
    OpenAIBackendAPI,
    is_account_rejected_error,
)
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageOutput
from services.storage.json_storage import JSONStorageBackend
from test.test_image_mainline_recovery import ImageHandshakeLockTests
from test.test_upstream_retry import FakeAccountService, FakeResponse, FakeSession
from utils.helper import UpstreamHTTPError
from utils.pow import (
    _utf16_units,
    build_proof_token_from_p,
    build_requirements_token,
    decode_requirements_p,
    sentinel_hash,
)


def _code_point_hash(text: str) -> str:
    """按 Python 的 code point（等价于 ``ord(ch)``）迭代的同一套 hash。

    只用来对照：上游的 JS 用的是 ``charCodeAt``，非 BMP 字符上两者结果不同。
    """
    h = 2166136261
    for ch in text:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return format(h & 0xFFFFFFFF, "08x")


class SentinelHashTests(unittest.TestCase):
    def test_matches_the_reference_implementation_for_bmp_text(self):
        # 取自参考实现（同样的常量与迭代顺序）。ASCII 上两者必须逐位一致，
        # 否则 PoW 解出来的 nonce 上游根本不认。
        for text, expected in (
            ("hello", "888d766e"),
            ("", "ab3e7c0b"),
            ("en-US", "9f79f257"),
            ("Mozilla/5.0 (X11; Linux x86_64)", "742176bb"),
        ):
            with self.subTest(text=text):
                self.assertEqual(sentinel_hash(text), expected)

    def test_iterates_utf16_code_units_like_char_code_at(self):
        # JS 的 charCodeAt 看到的 code unit：emoji 在 JS 里是两个 unit，在 Python
        # 里是一个 code point，所以对非 BMP 字符必须按 UTF-16 拆开再 hash。
        self.assertEqual(_utf16_units("😀"), [55357, 56832])
        self.assertEqual(_utf16_units("a"), [97])
        # BMP 字符两种迭代方式结果一样，非 BMP 不一样——这就是要按 code unit
        # 迭代的原因（上游那串 seed+payload 里若混进非 BMP 字符，哈希就对不上）。
        self.assertEqual(sentinel_hash("é"), _code_point_hash("é"))
        self.assertNotEqual(sentinel_hash("😀"), _code_point_hash("😀"))


class ProofTokenV2Tests(unittest.TestCase):
    def _requirements_p(self) -> str:
        return build_requirements_token("Mozilla/5.0 Chrome/146.0.0.0")

    def test_proof_token_is_solved_on_the_submitted_p(self):
        p_token = self._requirements_p()
        fields = decode_requirements_p(p_token)
        self.assertEqual(len(fields), 25)

        token = build_proof_token_from_p(p_token, "seed-value", "fff")

        self.assertTrue(token.startswith("gAAAAAB"))
        self.assertTrue(token.endswith("~S"))
        payload = json.loads(pybase64.b64decode(token[7:-2]))
        # 只有 nonce(3) 和已耗时毫秒(9) 会被改写，其余字段必须和 prepare 用的那份一致。
        self.assertEqual(payload[:3], fields[:3])
        self.assertEqual(payload[4:9], fields[4:9])
        self.assertEqual(payload[10:], fields[10:])
        # 调用方的 p 不能被就地改掉：同一个 backend 还要拿它做别的判断。
        self.assertEqual(decode_requirements_p(p_token), fields)

    def test_solved_proof_token_actually_passes_the_difficulty_check(self):
        p_token = self._requirements_p()
        difficulty = "0fffff"

        token = build_proof_token_from_p(p_token, "seed-value", difficulty)

        payload_b64 = token[7:-2]
        digest = sentinel_hash("seed-value" + payload_b64)
        self.assertLessEqual(digest[: len(difficulty)], difficulty)

    def test_difficulty_is_required(self):
        with self.assertRaises(ValueError):
            build_proof_token_from_p(self._requirements_p(), "seed", "")

    def test_decode_accepts_both_prefixes_and_rejects_garbage(self):
        fields = decode_requirements_p(self._requirements_p())
        for prefix in ("gAAAAAC", "gAAAAAB"):
            payload = pybase64.b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode()
            with self.subTest(prefix=prefix):
                self.assertEqual(decode_requirements_p(prefix + payload), fields)
        for bad in ("", "not-base64!", pybase64.b64encode(b'{"a":1}').decode()):
            with self.subTest(token=bad):
                with self.assertRaises(ValueError):
                    decode_requirements_p(bad)


class AccountRejectedDetectionTests(unittest.TestCase):
    """伪装成 404 的账号拒绝必须精确识别，别的 404 一律不套用。"""

    def test_exact_404_model_not_found_on_auto_is_detected(self):
        self.assertTrue(is_account_rejected_error(
            UpstreamHTTPError("/backend-api/f/conversation", 404, {"detail": "Model not found"}),
            "auto",
        ))

    def test_json_encoded_body_is_detected(self):
        self.assertTrue(is_account_rejected_error(
            UpstreamHTTPError("/backend-api/f/conversation", 404, '{"detail": "Model not found"}'),
            "auto",
        ))

    def test_other_404_shapes_are_not_account_rejections(self):
        cases = [
            # 模型真的改名了 / 路径写错
            UpstreamHTTPError("/backend-api/f/conversation", 404, {"detail": "Not Found"}),
            # 只包含不算：上游文案里多一个字都不该被当成账号问题
            UpstreamHTTPError("/backend-api/f/conversation", 404, {"detail": "Model not found."}),
            # detail 是对象（另一种 404 格式）
            UpstreamHTTPError("/backend-api/f/conversation", 404, {"detail": {"error": "Model not found"}}),
            # 状态码不是 404
            UpstreamHTTPError("/backend-api/f/conversation", 400, {"detail": "Model not found"}),
            # 不是结构化异常
            RuntimeError("Model not found"),
            # 空体
            UpstreamHTTPError("/backend-api/f/conversation", 404, ""),
        ]
        for exc in cases:
            with self.subTest(exc=str(exc)):
                self.assertFalse(is_account_rejected_error(exc, "auto"))

    def test_non_auto_model_off_the_image_path_is_not_an_account_rejection(self):
        """图片链路之外的同款 404 不套用这条规则：只有生图那条路会这么伪装。"""
        exc = UpstreamHTTPError("/backend-api/conversation", 404, {"detail": "Model not found"})
        for model in ("", "gpt-5-5", "auto-thinking"):
            with self.subTest(model=model):
                self.assertFalse(is_account_rejected_error(exc, model))

    def test_the_image_path_alone_is_enough(self):
        """路径比模型名更硬：本地把图片模型配成别的东西也不能漏判。"""
        exc = UpstreamHTTPError("/backend-api/f/conversation", 404, {"detail": "Model not found"})
        for model in ("", "gpt-5-5", "auto-thinking", "auto"):
            with self.subTest(model=model):
                self.assertTrue(is_account_rejected_error(exc, model))

    def test_the_prepare_path_is_not_the_mainline_path(self):
        """prepare 也在这条前缀下，但没有它自己的伪装 404 语义——只有主对话路径算。"""
        exc = UpstreamHTTPError("/backend-api/f/conversation/prepare", 404, {"detail": "Model not found"})
        self.assertFalse(is_account_rejected_error(exc, "gpt-5-5"))


class SentinelModeSwitchTests(unittest.TestCase):
    """设置里的开关同时切换指纹、finalize 字段名和 PoW 实现。"""

    def test_default_impersonation_depends_on_mode(self):
        with mock.patch.dict(config.data, {}, clear=True):
            self.assertEqual(OpenAIBackendAPI._default_impersonate(), DEFAULT_IMPERSONATE)
        with mock.patch.dict(config.data, {"image_sentinel_mode": "legacy"}, clear=True):
            self.assertEqual(OpenAIBackendAPI._default_impersonate(), LEGACY_IMPERSONATE)
            self.assertFalse(OpenAIBackendAPI._use_new_sentinel())
        with mock.patch.dict(config.data, {"image_sentinel_mode": "new"}, clear=True):
            self.assertEqual(OpenAIBackendAPI._default_impersonate(), DEFAULT_IMPERSONATE)
            self.assertTrue(OpenAIBackendAPI._use_new_sentinel())

    def test_unknown_mode_falls_back_to_new(self):
        with mock.patch.dict(config.data, {"image_sentinel_mode": "whatever"}, clear=True):
            self.assertEqual(config.image_sentinel_mode, "new")

    def test_config_round_trips_the_mode_and_hides_the_solver_key(self):
        original = dict(config.data)
        self.addCleanup(lambda: config.data.update(original))
        # 只验证规范化与出参，不落盘：这里跑的是仓库根目录那份 config.json。
        with mock.patch.object(type(config), "_save", lambda _self: None):
            public = config.update({
                "image_sentinel_mode": "legacy",
                "sentinel_solver_key": "solver-secret",
                "sentinel_solver_url": "http://solver:8080",
            })
            self.assertEqual(public["image_sentinel_mode"], "legacy")
            self.assertEqual(public["sentinel_solver_url"], "http://solver:8080")
            # 求解服务密钥是凭据，绝不跟着设置接口回给前端
            self.assertNotIn("sentinel_solver_key", public)
            # 非法值一律落回 new，别让一个手改的字符串把链路切到旧版
            self.assertEqual(config.update({"image_sentinel_mode": "LEGACY "})["image_sentinel_mode"], "legacy")
            self.assertEqual(config.update({"image_sentinel_mode": "old"})["image_sentinel_mode"], "new")

    def test_stored_legacy_impersonation_is_upgraded_to_default(self):
        with mock.patch.object(backend_module.account_service, "get_account", return_value={"impersonate": "chrome110"}):
            self.assertEqual(OpenAIBackendAPI(access_token="token").fp["impersonate"], DEFAULT_IMPERSONATE)
        # 别的指纹（非本项目的旧默认值）不能被动
        with mock.patch.object(backend_module.account_service, "get_account", return_value={"impersonate": "safari17_0"}):
            self.assertEqual(OpenAIBackendAPI(access_token="token").fp["impersonate"], "safari17_0")
        with mock.patch.object(backend_module.account_service, "get_account", return_value={}):
            self.assertEqual(OpenAIBackendAPI(access_token="token").fp["impersonate"], DEFAULT_IMPERSONATE)

    def test_finalize_payload_field_names_follow_the_mode(self):
        prepare = {"prepare_token": "prepare-token"}
        with mock.patch.dict(config.data, {"image_sentinel_mode": "new"}, clear=True):
            self.assertEqual(
                OpenAIBackendAPI._finalize_payload(prepare, "proof", "turnstile"),
                {"prepare_token": "prepare-token", "proofofwork": "proof", "turnstile": "turnstile"},
            )
        with mock.patch.dict(config.data, {"image_sentinel_mode": "legacy"}, clear=True):
            self.assertEqual(
                OpenAIBackendAPI._finalize_payload(prepare, "proof", "turnstile"),
                {"prepare_token": "prepare-token", "proof_token": "proof", "turnstile_token": "turnstile"},
            )

    def test_turnstile_solver_choice_follows_the_mode(self):
        api = OpenAIBackendAPI(access_token="token")
        prepare = {"prepare_token": "prepare-token", "turnstile": {"dx": "dx"}}
        with mock.patch.dict(config.data, {"image_sentinel_mode": "new"}, clear=True), \
             mock.patch.object(backend_module, "solve_turnstile", return_value="from-solver") as solver, \
             mock.patch.object(backend_module, "solve_turnstile_token", return_value="from-vm") as local:
            # 新版把整份 prepare 响应一起交给求解服务：它优先调页面自带的 turnstile 模块。
            self.assertEqual(api._solve_turnstile("dx", "p", prepare), "from-solver")
        solver.assert_called_once_with("dx", "p", prepare)
        local.assert_not_called()

        with mock.patch.dict(config.data, {"image_sentinel_mode": "legacy"}, clear=True), \
             mock.patch.object(backend_module, "solve_turnstile", return_value="from-solver") as solver, \
             mock.patch.object(backend_module, "solve_turnstile_token", return_value="from-vm") as local:
            # 旧版刻意不碰求解服务，只用本地 VM。
            self.assertEqual(api._solve_turnstile("dx", "p", prepare), "from-vm")
        solver.assert_not_called()
        local.assert_called_once_with("dx", "p")

    def test_sentinel_user_agent_prefers_the_solver_page(self):
        api = OpenAIBackendAPI(access_token="token")
        with mock.patch.object(backend_module, "solver_user_agent", return_value="SolverUA/1.0"):
            self.assertEqual(api._sentinel_user_agent(), "SolverUA/1.0")
        with mock.patch.object(backend_module, "solver_user_agent", return_value=""):
            self.assertEqual(api._sentinel_user_agent(), api.user_agent)


class SentinelSolverClientTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, status_code: int = 200, payload: object = None) -> None:
            self.status_code = status_code
            self._payload = payload if payload is not None else {}
            self.text = str(self._payload)

        def json(self) -> object:
            return self._payload

    def setUp(self) -> None:
        client_module._profile_cache.update(url="", profile={}, expires_at=0.0)

    def _solver_config(self, url: str = "http://solver:8080", key: str = "solver-key"):
        return mock.patch.multiple(
            type(config),
            sentinel_solver_url=mock.PropertyMock(return_value=url),
            sentinel_solver_key=mock.PropertyMock(return_value=key),
        )

    def test_solve_turnstile_calls_the_solver_with_the_key(self):
        with self._solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=self.FakeResponse(200, {"token": "solved"})
        ) as post:
            self.assertEqual(client_module.solve_turnstile("dx-value", "p-value"), "solved")

        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://solver:8080/v1/turnstile")
        self.assertEqual(kwargs["json"], {"dx": "dx-value", "p": "p-value"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer solver-key")

    def test_the_prepare_response_is_forwarded_to_the_solver(self):
        """有 prepare 响应就一起转过去，求解服务据此调页面自带的 turnstile 模块。"""
        prepare = {"prepare_token": "prepare-token", "turnstile": {"dx": "dx-value"}}
        with self._solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=self.FakeResponse(200, {"token": "solved"})
        ) as post:
            self.assertEqual(client_module.solve_turnstile("dx-value", "p-value", prepare), "solved")
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"dx": "dx-value", "p": "p-value", "requirements": prepare},
        )

    def test_a_solve_timeout_gets_a_longer_budget_than_the_health_check(self):
        """页面自带模块第一次用要现取现解析，用探活那 20 秒会白白超时。"""
        with self._solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=self.FakeResponse(200, {"token": "solved"})
        ) as post:
            client_module.solve_turnstile("dx", "p")
        self.assertEqual(post.call_args.kwargs["timeout"], client_module.SOLVER_SOLVE_TIMEOUT_SECONDS)
        self.assertGreater(
            client_module.SOLVER_SOLVE_TIMEOUT_SECONDS,
            client_module.SOLVER_TIMEOUT_SECONDS,
        )

    def test_the_client_has_no_local_vm_to_fall_back_to(self):
        """退路必须是明确的报错，不能是本地 VM：本地算的 token 会被上游拒。"""
        self.assertFalse(hasattr(client_module, "solve_turnstile_token"))

    def test_solver_failure_raises_instead_of_falling_back(self):
        """故意失败即报错：本地 VM 算出来的 token 会被上游拒，代价是白冷却一个账号。"""
        with self._solver_config(), mock.patch.object(
            client_module.requests, "post", side_effect=RuntimeError("connection refused")
        ):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")

    def test_a_non_200_solver_response_raises(self):
        with self._solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=self.FakeResponse(503, {"detail": "solve failed"})
        ):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")

    def test_empty_solver_token_raises_instead_of_falling_back(self):
        with self._solver_config(), mock.patch.object(
            client_module.requests, "post", return_value=self.FakeResponse(200, {"token": ""})
        ):
            with self.assertRaises(client_module.SentinelSolverError):
                client_module.solve_turnstile("dx", "p")

    def test_unconfigured_solver_raises_without_calling_anything(self):
        with self._solver_config(url=""), mock.patch.object(client_module.requests, "post") as post:
            with self.assertRaises(client_module.SentinelSolverError) as ctx:
                client_module.solve_turnstile("dx", "p")
        # 报错信息要把「怎么退回旧版」写清楚，否则线上只会看到一句没头没尾的失败。
        self.assertIn("旧版", str(ctx.exception))
        post.assert_not_called()

    def test_solver_user_agent_is_cached(self):
        with self._solver_config(), mock.patch.object(
            client_module.requests, "get", return_value=self.FakeResponse(200, {"user_agent": "SolverUA/1.0"})
        ) as get:
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")
        get.assert_called_once()

    def test_solver_user_agent_is_empty_without_a_solver(self):
        with self._solver_config(url=""), mock.patch.object(client_module.requests, "get") as get:
            self.assertEqual(client_module.solver_user_agent(), "")
        get.assert_not_called()

    def test_profile_carries_user_agent_and_timezone_from_one_call(self):
        payload = {"user_agent": "SolverUA/1.0", "timezone": "America/Los_Angeles", "timezone_offset_min": 420}
        with self._solver_config(), mock.patch.object(
            client_module.requests, "get", return_value=self.FakeResponse(200, payload)
        ) as get:
            self.assertEqual(client_module.solver_timezone_pair(), ("America/Los_Angeles", 420))
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")
        # UA、时区、偏移来自同一次 /health，不会各取一次而互相错位
        get.assert_called_once()

    def test_timezone_pair_is_none_without_a_solver(self):
        with self._solver_config(url=""), mock.patch.object(client_module.requests, "get") as get:
            self.assertIsNone(client_module.solver_timezone_pair())
        get.assert_not_called()

    def test_a_half_profile_is_ignored_as_a_whole(self):
        """页面只报了一半时宁可整对作废，也不让调用方拼出跨来源的组合。"""
        for payload in (
            {"user_agent": "UA", "timezone": "America/Los_Angeles"},              # 只有名字
            {"user_agent": "UA", "timezone_offset_min": 420},                     # 只有偏移
            {"user_agent": "UA", "timezone": "", "timezone_offset_min": 420},     # 名字是空的
            {"user_agent": "UA", "timezone": "America/Chicago", "timezone_offset_min": "420"},
            {"user_agent": "UA", "timezone": "America/Chicago", "timezone_offset_min": True},
        ):
            with self.subTest(payload=payload), self._solver_config(), mock.patch.object(
                client_module.requests, "get", return_value=self.FakeResponse(200, payload)
            ):
                self.assertIsNone(client_module.solver_timezone_pair())

    def test_degraded_health_gets_only_the_short_cache(self):
        """降级中的求解服务 /health 会少字段，只短缓存，别把空档案钉死十分钟。"""
        with self._solver_config(), mock.patch.object(
            client_module.requests, "get",
            return_value=self.FakeResponse(200, {"ready": False, "user_agent": "", "last_error": "no chromium"}),
        ) as get:
            self.assertIsNone(client_module.solver_timezone_pair())
            self.assertEqual(client_module.solver_profile(), {})
        self.assertEqual(get.call_count, 1)
        self.assertLessEqual(float(client_module._profile_cache["expires_at"]) - time.monotonic(), 30.0)

    def test_a_real_profile_gets_the_long_cache(self):
        payload = {"user_agent": "SolverUA/1.0", "timezone": "America/Los_Angeles", "timezone_offset_min": 420}
        with self._solver_config(), mock.patch.object(
            client_module.requests, "get", return_value=self.FakeResponse(200, payload)
        ) as get:
            client_module.solver_profile()
        self.assertEqual(get.call_count, 1)
        self.assertGreater(float(client_module._profile_cache["expires_at"]) - time.monotonic(), 30.0)
        # 返回的是副本，调用方改不动缓存
        with mock.patch.object(client_module.requests, "get", return_value=self.FakeResponse(200, {})):
            client_module.solver_profile()["user_agent"] = "tampered"
            self.assertEqual(client_module.solver_user_agent(), "SolverUA/1.0")


class AccountRejectCooldownTests(unittest.TestCase):
    def _service(self, tokens: list[str]) -> AccountService:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        service = AccountService(JSONStorageBackend(Path(tmp_dir.name) / "accounts.json"))
        service.add_accounts(tokens)
        for token in tokens:
            service.update_account(token, {"status": "正常", "quota": 5})
        return service

    def test_rejected_account_is_deprioritized_but_not_removed(self):
        service = self._service(["token-a", "token-b"])

        service.mark_image_account_rejected("token-a")

        self.assertEqual(service.get_available_access_token(), "token-b")
        # 只是排序偏好：不动状态、不动额度，账号仍然可被选中
        account = service.get_account("token-a")
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["quota"], 5)
        # 池子里只剩被拒账号时依然能选出来（否则整池被拒就等于生图全挂）
        service.mark_image_account_rejected("token-b")
        self.assertEqual(service.get_available_access_token(), "token-a")
        service.release_image_slot("token-a")
        service.release_image_slot("token-b")

    def test_cooldown_expires_after_the_window(self):
        service = self._service(["token-a", "token-b"])
        service.mark_image_account_rejected("token-a")
        # 冷却期内排最后
        self.assertEqual(service.get_available_access_token(), "token-b")
        service.release_image_slot("token-b")

        with service._image_slot_condition:
            service._image_account_rejects["token-a"] = time.monotonic() - ACCOUNT_REJECT_COOLDOWN_SECS - 1
            service._prune_mainline_rejects_locked(time.monotonic())

            self.assertNotIn("token-a", service._image_account_rejects)
            # 冷却结束后不再被降级：和从没被拒过的账号同档（上游的判定会自己恢复）
            self.assertEqual(
                service._image_candidate_sort_key_locked("token-a", time.monotonic()),
                service._image_candidate_sort_key_locked("token-b", time.monotonic()),
            )

    def test_cooldown_is_longer_than_the_mainline_one(self):
        from services.account_service import MAINLINE_REJECT_COOLDOWN_SECS
        self.assertGreater(ACCOUNT_REJECT_COOLDOWN_SECS, MAINLINE_REJECT_COOLDOWN_SECS)


class PictureStreamAccountRejectionTests(unittest.TestCase):
    """404 Model not found 要在握手阶段就换成 ImageAccountRejectedError 抛给上层。"""

    def _api(self):
        api = OpenAIBackendAPI("token-a")
        return api

    def test_404_model_not_found_is_converted(self):
        api = self._api()
        fake_service = ImageHandshakeLockTests.RecordingAccountService([])
        with mock.patch("services.openai_backend_api.account_service", fake_service), \
             mock.patch.dict(config.data, {"default_upstream_model_name": "auto"}, clear=True), \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", return_value=ChatRequirements("one")), \
             mock.patch.object(api, "_prepare_image_conversation", return_value="one"), \
             mock.patch.object(
                 api, "_start_image_generation",
                 side_effect=UpstreamHTTPError("conversation", 404, {"detail": "Model not found"}),
             ), \
             mock.patch.object(api, "_sleep_with_deadline"):
            with self.assertRaises(ImageAccountRejectedError):
                list(api._stream_picture_conversation("draw", "gpt-image-2", []))

    def test_other_404_still_raises_the_original_error(self):
        api = self._api()
        fake_service = ImageHandshakeLockTests.RecordingAccountService([])
        with mock.patch("services.openai_backend_api.account_service", fake_service), \
             mock.patch.dict(config.data, {"default_upstream_model_name": "auto"}, clear=True), \
             mock.patch.object(api, "_bootstrap"), \
             mock.patch.object(api, "_get_chat_requirements", return_value=ChatRequirements("one")), \
             mock.patch.object(api, "_prepare_image_conversation", return_value="one"), \
             mock.patch.object(
                 api, "_start_image_generation",
                 side_effect=UpstreamHTTPError("conversation", 404, {"detail": "Not Found"}),
             ), \
             mock.patch.object(api, "_sleep_with_deadline"):
            with self.assertRaises(UpstreamHTTPError):
                list(api._stream_picture_conversation("draw", "gpt-image-2", []))


class ImagePoolAccountRejectionSwitchTests(unittest.TestCase):
    def test_pool_switches_account_on_account_rejection(self):
        fake_accounts = FakeAccountService(["token-a", "token-b"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            if backend.access_token == "token-a":
                raise ImageAccountRejectedError("official image account rejected: Model not found")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs):
            outputs = list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="test",
            )))

        self.assertEqual(outputs[0].kind, "result")
        # 换号，但绝不封号：只有额度/失效的路径才动 remove_invalid_token。
        self.assertEqual(fake_accounts.account_rejected_tokens, ["token-a"])
        self.assertEqual(fake_accounts.removed_invalid_tokens, [])
        self.assertEqual(fake_accounts.image_results, [("token-a", False), ("token-b", True)])

    def test_every_account_rejected_ends_as_a_channel_busy_error(self):
        fake_accounts = FakeAccountService(["token-a"])

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            raise ImageAccountRejectedError("official image account rejected: Model not found")
            yield  # pragma: no cover - 生成器签名

        # 抢救窗口压到 0：号池空了以后立刻收尾，不用真等满 20 秒。
        with mock.patch.object(conversation, "account_service", fake_accounts), \
             mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs), \
             mock.patch.object(conversation, "TRANSIENT_IMAGE_RESCUE_WINDOW_SECS", 0.0):
            with self.assertRaises(conversation.ImageGenerationError):
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(
                    model="gpt-image-2",
                    prompt="test",
                )))

        self.assertEqual(fake_accounts.account_rejected_tokens, ["token-a"])
        # 号池被拒也不封号：仍然只走本地冷却
        self.assertEqual(fake_accounts.removed_invalid_tokens, [])


class TimezoneConfigTests(unittest.TestCase):
    """上报给上游的时区名/偏移要跟着部署地走，不能再写死 Asia/Shanghai + -480。

    不挂代理时上游只看「IP 出口 + 浏览器时区」是否自洽，名字写错很难被发现：
    请求照样能发出去，只是被算成低可信。所以这里把取值顺序钉住。
    """

    def setUp(self):
        self._env = {name: os.environ.get(name) for name in ("CHATGPT2API_TIMEZONE", "TZ")}
        for name in self._env:
            os.environ.pop(name, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_precedence_env_beats_config_beats_tz(self):
        # 什么都没配 → 历史默认值，升级前后行为一致
        with mock.patch.dict(config.data, {}, clear=True):
            self.assertEqual(config.timezone, DEFAULT_TIMEZONE)
        # 容器的 TZ 是兜底：compose 里只改这一处就能改时区
        os.environ["TZ"] = "America/Chicago"
        with mock.patch.dict(config.data, {}, clear=True):
            self.assertEqual(config.timezone, "America/Chicago")
        # config.json 优先于 TZ
        with mock.patch.dict(config.data, {"timezone": "America/New_York"}, clear=True):
            self.assertEqual(config.timezone, "America/New_York")
        # 环境变量优先级最高，方便不动容器时区单独覆盖
        os.environ["CHATGPT2API_TIMEZONE"] = "America/Los_Angeles"
        with mock.patch.dict(config.data, {"timezone": "America/New_York"}, clear=True):
            self.assertEqual(config.timezone, "America/Los_Angeles")

    def test_blank_values_fall_through_to_the_default(self):
        os.environ["CHATGPT2API_TIMEZONE"] = "   "
        os.environ["TZ"] = ""
        with mock.patch.dict(config.data, {"timezone": "  "}, clear=True):
            self.assertEqual(config.timezone, DEFAULT_TIMEZONE)

    def test_offset_is_js_get_timezone_offset(self):
        """符号要跟 JS 的 getTimezoneOffset() 一致：UTC 减本地（东八区是 -480）。"""
        offset = config.timezone_offset_min
        self.assertIsInstance(offset, int)
        local = datetime.now().astimezone()
        delta = local.utcoffset()
        if delta is None:  # pragma: no cover - 系统时区数据缺失时的兜底
            self.assertEqual(offset, DEFAULT_TIMEZONE_OFFSET_MIN)
            return
        self.assertEqual(offset, -int(delta.total_seconds() // 60))
        # 把本地墙上时间按上报的偏移搬回 UTC，应该就是此刻的 UTC 时间
        as_utc = local.replace(tzinfo=None) + timedelta(minutes=offset)
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(abs((as_utc - utc_now).total_seconds()), 60)

    def test_image_prepare_reports_the_configured_timezone(self):
        api = OpenAIBackendAPI(access_token="token-a")
        captured: dict[str, object] = {}

        class FakeResponse:
            def json(self):
                return {"conduit_token": "conduit"}

        def fake_request(method, path, *args, **kwargs):
            captured.update(kwargs.get("json") or {})
            return FakeResponse()

        with mock.patch.dict(config.data, {
            "default_upstream_model_name": "auto",
            "timezone": "America/Los_Angeles",
        }, clear=True), \
             mock.patch.object(api, "_fast_request", side_effect=fake_request):
            api._prepare_image_conversation("draw", ChatRequirements("one"), "gpt-image-2")

        self.assertEqual(captured["timezone"], "America/Los_Angeles")
        self.assertEqual(captured["timezone_offset_min"], config.timezone_offset_min)

    def test_solver_page_timezone_wins_over_local_config(self):
        """两边都配时，以求解页面为准：它和算 Turnstile 的是同一个浏览器。"""
        api = OpenAIBackendAPI(access_token="token-a")
        with mock.patch.dict(config.data, {"timezone": "Asia/Shanghai"}, clear=True), \
             mock.patch.object(backend_module, "solver_timezone_pair", return_value=("America/Los_Angeles", 420)):
            self.assertEqual(api._sentinel_timezone_pair(), ("America/Los_Angeles", 420))
        # 页面没答全就整对退回本机配置（求解服务没配、降级）
        with mock.patch.dict(config.data, {"timezone": "Asia/Shanghai"}, clear=True), \
             mock.patch.object(backend_module, "solver_timezone_pair", return_value=None):
            self.assertEqual(api._sentinel_timezone_pair(), ("Asia/Shanghai", config.timezone_offset_min))

    def test_legacy_mode_never_touches_the_solver(self):
        """切回旧版要等于改动前那条路：不查求解服务，时区用本机配置。"""
        api = OpenAIBackendAPI(access_token="token-a")
        with mock.patch.dict(config.data, {"image_sentinel_mode": "legacy", "timezone": "Asia/Shanghai"}, clear=True), \
             mock.patch.object(backend_module, "solver_timezone_pair", side_effect=AssertionError("must not call")), \
             mock.patch.object(backend_module, "solver_user_agent", side_effect=AssertionError("must not call")):
            self.assertEqual(api._sentinel_timezone_pair(), ("Asia/Shanghai", config.timezone_offset_min))
            self.assertEqual(api._sentinel_user_agent(), api.user_agent)

    def test_image_prepare_prefers_the_solver_page_timezone(self):
        api = OpenAIBackendAPI(access_token="token-a")
        captured: dict[str, object] = {}

        class FakeResponse:
            def json(self):
                return {"conduit_token": "conduit"}

        def fake_request(method, path, *args, **kwargs):
            captured.update(kwargs.get("json") or {})
            return FakeResponse()

        # 故意不改 default_upstream_model_name：它就是线上的默认文本模型（gpt-5-5），
        # 图片链路必须无视它。之前这里被改成 auto，正好把这个 bug 盖住了。
        with mock.patch.dict(config.data, {"timezone": "Asia/Shanghai"}, clear=True), \
             mock.patch.object(backend_module, "solver_timezone_pair", return_value=("America/Chicago", 360)), \
             mock.patch.object(api, "_fast_request", side_effect=fake_request):
            api._prepare_image_conversation("draw", ChatRequirements("one"), "gpt-image-2")

        self.assertEqual(captured["timezone"], "America/Chicago")
        self.assertEqual(captured["timezone_offset_min"], 360)
        self.assertEqual(captured["model"], "auto")

    def test_settings_save_cannot_pin_the_timezone(self):
        """设置页整份回传时会带上 timezone，update() 必须丢掉它。

        照收的话一次保存就把时区写进 config.json，之后改 TZ 再也不会生效 —— 而偏移
        仍从进程时区算，名字和数字就开始各说各话。
        """
        original = dict(config.data)
        self.addCleanup(lambda: setattr(config, "data", original))
        with mock.patch.object(type(config), "_save", lambda _self: None):
            os.environ["TZ"] = "America/Chicago"
            config.update({"timezone": "Asia/Shanghai", "timezone_offset_min": -480})
            self.assertNotIn("timezone", config.data)
            self.assertNotIn("timezone_offset_min", config.data)
            self.assertEqual(config.timezone, "America/Chicago")
            self.assertEqual(config.update({})["timezone"], "America/Chicago")

    def test_chat_target_timezone_follows_config(self):
        with mock.patch.dict(config.data, {"timezone": "America/Chicago"}, clear=True):
            self.assertEqual(OpenAIBackendAPI(access_token="token-a")._chat_target()[1], "America/Chicago")
        # 匿名会话直接打匿名端点，时区是上游那边的固定值，不跟配置走
        self.assertEqual(OpenAIBackendAPI()._chat_target()[1], "America/Los_Angeles")


class ImageSentinelHeaderTests(unittest.TestCase):
    """图片链路的请求头：每个 sentinel token 都要跟着请求发出去，trace id 要一致。"""

    def test_image_headers_carry_every_sentinel_token(self):
        api = OpenAIBackendAPI(access_token="token-a")
        requirements = ChatRequirements(
            token="requirements-token",
            proof_token="proof-token",
            turnstile_token="turnstile-token",
            so_token="so-token",
        )
        headers = api._image_headers(
            "/backend-api/f/conversation", requirements, "conduit-token", "text/event-stream"
        )
        self.assertEqual(headers["OpenAI-Sentinel-Chat-Requirements-Token"], "requirements-token")
        self.assertEqual(headers["OpenAI-Sentinel-Proof-Token"], "proof-token")
        # 求解服务算 Turnstile token 就是为了这条链路，漏发等于白算。
        self.assertEqual(headers["OpenAI-Sentinel-Turnstile-Token"], "turnstile-token")
        self.assertEqual(headers["OpenAI-Sentinel-SO-Token"], "so-token")
        self.assertEqual(headers["X-Conduit-Token"], "conduit-token")
        self.assertTrue(headers["X-Oai-Turn-Trace-Id"])

    def test_missing_tokens_are_left_out_entirely(self):
        """没有的 token 就不发这个头：空字符串同样是一种「发过了」的痕迹。"""
        api = OpenAIBackendAPI(access_token="token-a")
        headers = api._image_headers(
            "/backend-api/f/conversation/prepare", ChatRequirements(token="requirements-token")
        )
        for name in (
            "OpenAI-Sentinel-Proof-Token",
            "OpenAI-Sentinel-Turnstile-Token",
            "OpenAI-Sentinel-SO-Token",
            "X-Conduit-Token",
        ):
            with self.subTest(header=name):
                self.assertNotIn(name, headers)

    def test_prepare_issues_the_turn_trace_id_that_the_mainline_reuses(self):
        """prepare 和主对话请求共用同一个 turn trace id，网页就是这么发的。"""
        api = OpenAIBackendAPI(access_token="token-a")
        api.session = FakeSession([FakeResponse(200, {"conduit_token": "conduit-token"})])

        api._prepare_image_conversation("draw a cat", ChatRequirements(token="requirements-token"), "gpt-image-2")

        trace_id = api._image_turn_trace_id
        self.assertTrue(trace_id)
        self.assertEqual(api.session.calls[0][2]["headers"]["X-Oai-Turn-Trace-Id"], trace_id)

    def test_each_attempt_gets_its_own_trace_id(self):
        api = OpenAIBackendAPI(access_token="token-a")
        api.session = FakeSession([
            FakeResponse(200, {"conduit_token": "conduit-one"}),
            FakeResponse(200, {"conduit_token": "conduit-two"}),
        ])

        api._prepare_image_conversation("draw a cat", ChatRequirements(token="one"), "gpt-image-2")
        first = api._image_turn_trace_id
        api._prepare_image_conversation("draw a cat", ChatRequirements(token="two"), "gpt-image-2")

        self.assertNotEqual(first, api._image_turn_trace_id)


class RestrictedCookieTests(unittest.TestCase):
    """finalize 种下的 oai-sc 是「这轮握手不干净」的标记，必须清掉。"""

    def test_finalize_drops_the_restricted_cookie_and_nothing_else(self):
        api = OpenAIBackendAPI(access_token="token-a")
        api.session.cookies.set("oai-sc", "restricted", domain="chatgpt.com", path="/")
        api.session.cookies.set("__Secure-next-auth.session-token", "keep-me", domain="chatgpt.com", path="/")

        api._drop_restricted_cookies()

        names = {cookie.name for cookie in api.session.cookies.jar}
        self.assertNotIn("oai-sc", names)
        self.assertIn("__Secure-next-auth.session-token", names)

    def test_dropping_is_a_noop_without_the_cookie(self):
        api = OpenAIBackendAPI(access_token="token-a")
        api.session.cookies.set("other", "value", domain="chatgpt.com", path="/")

        api._drop_restricted_cookies()

        self.assertEqual({cookie.name for cookie in api.session.cookies.jar}, {"other"})


if __name__ == "__main__":
    unittest.main()
