from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import IMAGE_HANDSHAKE_LEASE_SECS, AccountService
from services.auth_service import AuthService
from services.config import config
from services.openai_backend_api import InvalidAccessTokenError
from services.storage.json_storage import JSONStorageBackend
from utils.helper import UpstreamHTTPError, anonymize_token, split_image_model


class AccountCapabilityTests(unittest.TestCase):
    def test_unknown_quota_accounts_are_available_only_when_not_throttled(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "image_quota_unknown": True, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "image_quota_unknown": True, "quota": 0}
            )
        )

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_does_not_consume_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 0,
                    "image_quota_unknown": True,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "正常")
            self.assertTrue(updated["image_quota_unknown"])

    def test_list_accounts_reports_inflight_slot_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service._image_inflight["token-1"] = [1.0, 2.0]

            items = service.list_accounts()

            self.assertEqual(items[0]["image_inflight"], 2)

    def test_malformed_numeric_account_fields_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "token-1",
                        "status": "正常",
                        "quota": [],
                        "success": {},
                        "fail": (),
                        "invalid_count": [],
                    }
                ]
            )

            account = service.get_account("token-1")
            stats = service.get_stats()

            self.assertEqual(account["quota"], 0)
            self.assertEqual(account["success"], 0)
            self.assertEqual(account["fail"], 0)
            self.assertEqual(account["invalid_count"], 0)
            self.assertEqual(stats["total_quota"], 0)

    def test_stale_image_slot_does_not_block_account_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account("token-1", {"status": "正常", "quota": 1})

            self.assertEqual(service.get_available_access_token(), "token-1")
            service._image_inflight["token-1"] = [time.monotonic() - 10000]

            self.assertEqual(service.get_available_access_token(), "token-1")
            service.release_image_slot("token-1")
            self.assertEqual(len(service._image_inflight["token-1"]), 1)
            service.release_image_slot("token-1")
            self.assertNotIn("token-1", service._image_inflight)

    def test_available_token_prefers_the_idle_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-busy", "token-idle"])
            for token in ("token-busy", "token-idle"):
                service.update_account(token, {"status": "正常", "quota": 5})
            service._image_inflight["token-busy"] = [time.monotonic(), time.monotonic()]

            picked = service.get_available_access_token()

            self.assertEqual(picked, "token-idle")
            self.assertEqual(len(service._image_inflight["token-idle"]), 1)
            self.assertEqual(len(service._image_inflight["token-busy"]), 2)

    def test_single_account_still_serves_the_configured_concurrency(self) -> None:
        original_value = config.data.get("image_account_concurrency")
        config.data["image_account_concurrency"] = 3
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_accounts(["token-1"])
                service.update_account("token-1", {"status": "正常", "quota": 5})

                picked = [service.get_available_access_token() for _ in range(3)]

                self.assertEqual(picked, ["token-1"] * 3)
                self.assertEqual(len(service._image_inflight["token-1"]), 3)
        finally:
            if original_value is None:
                config.data.pop("image_account_concurrency", None)
            else:
                config.data["image_account_concurrency"] = original_value

    def test_image_handshake_lock_serializes_only_the_same_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1", "token-2"])

            self.assertTrue(service.acquire_image_handshake("token-1", timeout=1.0))
            try:
                # 同账号的第二个握手必须等，短超时下拿不到锁
                self.assertFalse(service.acquire_image_handshake("token-1", timeout=0.05))
                # 换一个账号不受影响
                self.assertTrue(service.acquire_image_handshake("token-2", timeout=0.05))
                service.release_image_handshake("token-2")
            finally:
                service.release_image_handshake("token-1")

            # 释放后立刻可以重新拿到
            self.assertTrue(service.acquire_image_handshake("token-1", timeout=0.05))
            service.release_image_handshake("token-1")

    def test_image_handshake_lock_survives_token_rotation(self) -> None:
        """access_token 轮换后，同一账号的握手仍然串行。

        之前锁按 token 字符串索引，轮换前拿到的锁和轮换后的请求用的锁是两个对象，
        同账号照样并发握手——这正是 skipped_mainline 的触发条件。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "token-old", "user_id": "user-1", "status": "正常", "quota": 5},
            ])

            self.assertTrue(service.acquire_image_handshake("token-old", timeout=1.0))
            try:
                rotated = service._apply_refreshed_tokens(
                    "token-old", {"access_token": "token-new", "refresh_token": "refresh-1"}, "test",
                )
                self.assertEqual(rotated, "token-new")
                # 轮换后的 token、以及轮换前的旧 token，都必须落到同一把锁上
                self.assertFalse(service.acquire_image_handshake("token-new", timeout=0.05))
                self.assertFalse(service.acquire_image_handshake("token-old", timeout=0.05))
            finally:
                service.release_image_handshake("token-old")

            self.assertTrue(service.acquire_image_handshake("token-new", timeout=0.05))
            service.release_image_handshake("token-new")

    def test_release_from_another_thread_does_not_free_someone_elses_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])

            self.assertTrue(service.acquire_image_handshake("token-1", timeout=1.0))
            try:
                worker = Thread(target=service.release_image_handshake, args=("token-1",))
                worker.start()
                worker.join()
                # 别的线程调 release 不能把本线程持有的锁放掉
                self.assertFalse(service.acquire_image_handshake("token-1", timeout=0.05))
            finally:
                service.release_image_handshake("token-1")

            self.assertTrue(service.acquire_image_handshake("token-1", timeout=0.05))
            service.release_image_handshake("token-1")

    def test_handshake_diagnostics_report_recent_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])

            self.assertTrue(service.acquire_image_handshake("token-1", timeout=1.0))
            diagnostics = service.image_handshake_diagnostics("token-1")
            service.release_image_handshake("token-1")

            self.assertEqual(diagnostics["handshake_key_kind"], "token")
            self.assertTrue(diagnostics["handshake_lock_held"])
            self.assertTrue(diagnostics["handshake_recent"][-1]["acquired"])
            self.assertIsInstance(diagnostics["handshake_recent"][-1]["wait_ms"], int)

    def test_stale_handshake_lock_is_broken_after_lease(self) -> None:
        """持有线程消失导致的泄漏锁会被租约强行拆掉。

        这类泄漏如果不拆，账号以后每条请求都要白等一次窗口超时。正常窗口只有 1-2 秒，
        所以超出租约（远大于 30s 的窗口超时）一定是泄漏。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])

            self.assertTrue(service.acquire_image_handshake("token-1", timeout=1.0))
            # 模拟持有线程消失：本线程的持有记录没了，锁还在
            service._image_handshake_held.locks = {}
            key, _ = service._image_handshake_key("token-1")
            service._image_handshake_holder[key] = time.monotonic() - (IMAGE_HANDSHAKE_LEASE_SECS + 60)

            def _try_acquire() -> None:
                acquired.append(service.acquire_image_handshake("token-1", timeout=0.5))
                service.release_image_handshake("token-1")

            acquired: list[bool] = []
            worker = Thread(target=_try_acquire)
            worker.start()
            worker.join()

            self.assertEqual(acquired, [True])
            # 拆锁后恢复正常：可以正常排队
            self.assertTrue(service.acquire_image_handshake("token-1", timeout=0.5))
            service.release_image_handshake("token-1")

    def test_recently_rejected_account_is_deprioritized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-a", "token-b"])
            for token in ("token-a", "token-b"):
                service.update_account(token, {"status": "正常", "quota": 5})

            service.mark_image_mainline_rejected("token-a")
            self.assertEqual(service.get_available_access_token(), "token-b")

            # 号池里全被拒过时依然能选出账号：这是排序偏好，不是排除
            service.mark_image_mainline_rejected("token-b")
            self.assertEqual(service.get_available_access_token(), "token-a")
            service.release_image_slot("token-a")
            service.release_image_slot("token-b")

    def test_excluded_token_follows_rotation(self) -> None:
        """排除项按账号算，不按 token 字符串：轮换后刚失败的账号不会再被选中。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "token-a", "user_id": "user-a", "status": "正常", "quota": 5},
            ])
            service._apply_refreshed_tokens("token-a", {"access_token": "token-a2"}, "test")

            # 池子里只有这一个账号：排除轮换前的旧 token 就等于排除这个账号
            self.assertFalse(service.has_available_image_account({"token-a"}))
            with self.assertRaises(RuntimeError):
                service.get_available_access_token({"token-a"})
            # 不排除时仍然可用（挂的是轮换后的新 token）
            self.assertEqual(service.get_available_access_token(), "token-a2")
            service.release_image_slot("token-a2")

    def test_has_available_image_account_excludes_without_taking_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1", "token-2"])
            for token in ("token-1", "token-2"):
                service.update_account(token, {"status": "正常", "quota": 5})

            self.assertTrue(service.has_available_image_account({"token-1"}))
            self.assertFalse(service.has_available_image_account({"token-1", "token-2"}))
            # 只是查询，不能占掉槽位
            self.assertEqual(service._image_inflight, {})

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = False
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_force_refresh_removes_limited_and_zero_quota_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "limited-token", "status": "正常", "quota": 5},
                {"access_token": "zero-token", "status": "正常", "quota": 5},
                {"access_token": "healthy-token", "status": "正常", "quota": 5},
            ])

            def fake_user_info(access_token: str) -> dict:
                if access_token == "limited-token":
                    return {"status": "限流", "quota": 0}
                if access_token == "zero-token":
                    return {"status": "正常", "quota": 0}
                return {"status": "正常", "quota": 3}

            with patch(
                "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                lambda api: fake_user_info(api.access_token),
            ):
                result = service.refresh_accounts(
                    ["limited-token", "zero-token", "healthy-token"],
                    defer_invalid_removal=False,
                )

            self.assertEqual(result["refreshed"], 3)
            self.assertIsNone(service.get_account("limited-token"))
            self.assertIsNone(service.get_account("zero-token"))
            self.assertIsNotNone(service.get_account("healthy-token"))
            self.assertEqual([item["access_token"] for item in result["items"]], ["healthy-token"])

    def test_force_refresh_removes_configured_http_error_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "delete-token", "status": "正常", "quota": 5},
                {"access_token": "keep-token", "status": "正常", "quota": 5},
                {"access_token": "stale-limited-token", "status": "限流", "quota": 0},
            ])

            def fake_user_info(access_token: str) -> dict:
                if access_token == "delete-token":
                    raise UpstreamHTTPError("/backend-api/me", 403, "forbidden")
                raise UpstreamHTTPError("/backend-api/me", 429, "rate limited")

            with patch(
                "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                lambda api: fake_user_info(api.access_token),
            ):
                result = service.refresh_accounts(
                    ["delete-token", "keep-token", "stale-limited-token"],
                    defer_invalid_removal=False,
                )

            self.assertEqual(result["refreshed"], 0)
            self.assertEqual(len(result["errors"]), 3)
            self.assertIsNone(service.get_account("delete-token"))
            self.assertIsNone(service.get_account("stale-limited-token"))
            self.assertIsNotNone(service.get_account("keep-token"))

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["invalid_count"], 1)
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
