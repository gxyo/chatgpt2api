import json
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
