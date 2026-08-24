import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


class FakeAccountService:
    def __init__(self) -> None:
        self.refresh_calls: list[dict] = []
        self.items = [{"access_token": "token-a", "status": "限流", "quota": 0}]

    def list_tokens(self) -> list[str]:
        return ["token-a"]

    def refresh_accounts(self, tokens: list[str], defer_invalid_removal: bool = True) -> dict:
        self.refresh_calls.append({"tokens": list(tokens), "defer_invalid_removal": defer_invalid_removal})
        self.items = [{"access_token": "token-a", "status": "正常", "quota": 10}]
        return {"refreshed": 1, "errors": [], "items": self.items}

    def list_accounts(self) -> list[dict]:
        return [dict(item) for item in self.items]


class RegisterServiceTests(unittest.TestCase):
    def test_browser_resource_exhaustion_error_is_fatal_and_concise(self) -> None:
        from services.register import openai_register

        message, fatal = openai_register._classify_worker_error(
            RuntimeError(
                "BrowserType.launch: Target page, context or browser has been closed "
                "chrome_crashpad_handler: Resource temporarily unavailable (11)"
            )
        )

        self.assertTrue(fatal)
        self.assertEqual(
            message,
            "浏览器启动失败：系统无法创建 Chromium 子进程，PID/线程或内存资源已耗尽",
        )

    def test_fatal_worker_error_stops_registration_instead_of_retrying(self) -> None:
        from services import register_service as register_module

        calls = []

        def fatal_worker(task_number: int) -> dict:
            calls.append(task_number)
            return {
                "ok": False,
                "index": task_number,
                "error": "浏览器进程资源已耗尽",
                "fatal": True,
            }

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = register_module.RegisterService(Path(tmp_dir) / "register.json")
            service.update({"mode": "total", "total": 100, "threads": 1})
            with patch.object(register_module.openai_register, "worker", side_effect=fatal_worker):
                service.start()
                self.assertIsNotNone(service._runner)
                service._runner.join(timeout=5)

            snapshot = service.get()

        self.assertFalse(service._runner.is_alive())
        self.assertFalse(snapshot["enabled"])
        self.assertEqual(calls, [1])
        self.assertEqual(snapshot["stats"]["done"], 1)
        self.assertTrue(
            any("已自动停止注册任务" in item["text"] for item in snapshot["logs"])
        )

    def test_running_counts_only_workers_actively_executing(self) -> None:
        from services import register_service as register_module

        workers_started = threading.Event()
        release_workers = threading.Event()
        worker_lock = threading.Lock()
        started_count = 0

        def blocked_worker(task_number: int) -> dict:
            nonlocal started_count
            with worker_lock:
                started_count += 1
                should_block = started_count <= 2
                if started_count == 2:
                    workers_started.set()
            if should_block:
                release_workers.wait(timeout=5)
            return {"ok": True, "task_number": task_number}

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = register_module.RegisterService(Path(tmp_dir) / "register.json")
            with patch.object(register_module.openai_register, "worker", side_effect=blocked_worker):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(service._run_worker, index) for index in range(5)]
                    self.assertTrue(workers_started.wait(timeout=5))

                    self.assertEqual(service.get()["stats"]["running"], 2)

                    release_workers.set()
                    results = [future.result(timeout=5) for future in futures]

            self.assertEqual(len(results), 5)
            self.assertEqual(service.get()["stats"]["running"], 0)

    def test_available_mode_refreshes_accounts_before_target_check(self) -> None:
        from services import register_service as register_module

        fake_account_service = FakeAccountService()
        original_account_service = register_module.account_service
        try:
            register_module.account_service = fake_account_service
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = register_module.RegisterService(Path(tmp_dir) / "register.json")

                reached = service._target_reached(
                    {"mode": "available", "target_available": 1, "target_quota": 1},
                    submitted=0,
                )
                saved = json.loads((Path(tmp_dir) / "register.json").read_text(encoding="utf-8"))

            self.assertTrue(reached)
            self.assertEqual(
                fake_account_service.refresh_calls,
                [{"tokens": ["token-a"], "defer_invalid_removal": False}],
            )
            self.assertEqual(saved["stats"]["current_available"], 1)
            self.assertEqual(saved["stats"]["current_quota"], 10)
        finally:
            register_module.account_service = original_account_service

    def test_available_mode_limits_each_batch_before_refreshing_again(self) -> None:
        from services import register_service as register_module

        fake_account_service = FakeAccountService()
        fake_account_service.items = [
            {"access_token": f"token-{index}", "status": "正常", "quota": 5}
            for index in range(34)
        ]

        def refresh_accounts(tokens: list[str], defer_invalid_removal: bool = True) -> dict:
            fake_account_service.refresh_calls.append({
                "tokens": list(tokens),
                "defer_invalid_removal": defer_invalid_removal,
            })
            return {"refreshed": len(tokens), "errors": [], "items": fake_account_service.items}

        fake_account_service.list_tokens = lambda: [item["access_token"] for item in fake_account_service.items]
        fake_account_service.refresh_accounts = refresh_accounts
        original_account_service = register_module.account_service
        try:
            register_module.account_service = fake_account_service
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = register_module.RegisterService(Path(tmp_dir) / "register.json")

                plan = service._target_plan(
                    {"mode": "available", "target_available": 40, "target_quota": 1, "refresh_batch_size": 3},
                    submitted=0,
                )

            self.assertFalse(plan["reached"])
            self.assertEqual(plan["batch_size"], 3)
            self.assertEqual(len(fake_account_service.refresh_calls), 1)
        finally:
            register_module.account_service = original_account_service

    def test_available_mode_refreshes_after_each_configured_registration_batch(self) -> None:
        from services import register_service as register_module

        class GrowingAccountService:
            def __init__(self) -> None:
                self.items = [{"access_token": "token-0", "status": "正常", "quota": 5}]
                self.refresh_calls: list[list[str]] = []
                self.lock = threading.Lock()
                self.stop_callback = lambda: None

            def list_tokens(self) -> list[str]:
                with self.lock:
                    return [str(item["access_token"]) for item in self.items]

            def refresh_accounts(self, tokens: list[str], defer_invalid_removal: bool = True) -> dict:
                self.refresh_calls.append(list(tokens))
                if len(tokens) >= 5:
                    self.stop_callback()
                return {"refreshed": len(tokens), "errors": [], "items": self.items}

            def list_accounts(self) -> list[dict]:
                with self.lock:
                    return [dict(item) for item in self.items]

            def add_account(self, task_number: int) -> None:
                with self.lock:
                    self.items.append({"access_token": f"token-{task_number}", "status": "正常", "quota": 5})

        fake_account_service = GrowingAccountService()
        original_account_service = register_module.account_service
        try:
            register_module.account_service = fake_account_service
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = register_module.RegisterService(Path(tmp_dir) / "register.json")
                service.update({
                    "mode": "available",
                    "target_available": 5,
                    "refresh_batch_size": 2,
                    "check_interval": 1,
                    "threads": 1,
                })
                fake_account_service.stop_callback = service.stop

                def worker_result(task_number: int) -> dict:
                    succeeded = task_number != 1
                    if succeeded:
                        fake_account_service.add_account(task_number)
                    return {"ok": succeeded}

                with patch.object(register_module.openai_register, "worker", side_effect=worker_result):
                    service.start()
                    self.assertIsNotNone(service._runner)
                    service._runner.join(timeout=5)

                self.assertFalse(service._runner.is_alive())
                self.assertEqual(len(fake_account_service.items), 5)
                self.assertEqual([len(tokens) for tokens in fake_account_service.refresh_calls], [1, 2, 4, 5])
                self.assertEqual(service.get()["stats"]["done"], 5)
                self.assertIn("本轮计划补号 2 个，完成后刷新号池并重新计算缺口", [
                    item["text"] for item in service.get()["logs"]
                ])
        finally:
            register_module.account_service = original_account_service


if __name__ == "__main__":
    unittest.main()
