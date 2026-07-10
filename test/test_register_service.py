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

    def test_available_mode_plans_missing_accounts_as_one_batch(self) -> None:
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
                    {"mode": "available", "target_available": 40, "target_quota": 1, "threads": 2},
                    submitted=0,
                    max_batch=2,
                )

            self.assertFalse(plan["reached"])
            self.assertEqual(plan["batch_size"], 6)
            self.assertEqual(len(fake_account_service.refresh_calls), 1)
        finally:
            register_module.account_service = original_account_service


if __name__ == "__main__":
    unittest.main()
