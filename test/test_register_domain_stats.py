import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.register import mail_provider
from services import register_service as register_module


class RegisterDomainStatsTests(unittest.TestCase):
    def test_cloudflare_domain_results_are_counted_and_persisted(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        original_log_sink = register_module.openai_register.register_log_sink
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                store_file = Path(tmp_dir) / "register.json"
                service = register_module.RegisterService(store_file)
                service.update({
                    "mail": {
                        "request_timeout": 30,
                        "wait_timeout": 30,
                        "wait_interval": 2,
                        "providers": [{
                            "enable": True,
                            "type": "cloudflare_temp_email",
                            "api_base": "https://mail.example.test",
                            "admin_password": "test-only",
                            "domain": ["Example.COM", "unused.example"],
                        }],
                    },
                })

                events = [True] * 20 + [False] * 7
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(
                        lambda succeeded: mail_provider.mark_mailbox_result(
                            {"provider": "cloudflare_temp_email", "address": "person@Example.COM."},
                            success=succeeded,
                            error=None if succeeded else "registration failed",
                        ),
                        events,
                    ))
                mail_provider.mark_mailbox_result(
                    {"provider": "tempmail_lol", "address": "person@ignored.example"},
                    success=False,
                    error="ignored provider",
                )

                snapshot = service.get()
                stats = {item["domain"]: item for item in snapshot["cloudflare_domain_stats"]}
                saved = json.loads(store_file.read_text(encoding="utf-8"))

                self.assertEqual(stats["example.com"]["success"], 20)
                self.assertEqual(stats["example.com"]["fail"], 7)
                self.assertEqual(stats["example.com"]["total"], 27)
                self.assertEqual(stats["example.com"]["success_rate"], 74.1)
                self.assertEqual(stats["unused.example"]["total"], 0)
                self.assertNotIn("ignored.example", stats)
                self.assertEqual(len(saved["cloudflare_domain_stats"]), 1)

                reloaded = register_module.RegisterService(store_file)
                reloaded_stats = {
                    item["domain"]: item
                    for item in reloaded.get()["cloudflare_domain_stats"]
                }
                self.assertEqual(reloaded_stats["example.com"]["success"], 20)
                self.assertEqual(reloaded_stats["example.com"]["fail"], 7)
        finally:
            mail_provider.mailbox_result_sink = original_result_sink
            register_module.openai_register.register_log_sink = original_log_sink

    def test_stats_sink_failure_does_not_change_registration_result(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        try:
            def broken_sink(*args, **kwargs) -> None:
                raise OSError("disk unavailable")

            mail_provider.mailbox_result_sink = broken_sink
            mail_provider.mark_mailbox_result(
                {"provider": "cloudflare_temp_email", "address": "person@example.com"},
                success=True,
            )
        finally:
            mail_provider.mailbox_result_sink = original_result_sink

    def test_saving_domains_keeps_deleted_domain_stats_permanently(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        original_log_sink = register_module.openai_register.register_log_sink
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                store_file = Path(tmp_dir) / "register.json"
                service = register_module.RegisterService(store_file)
                mail_config = {
                    "request_timeout": 30,
                    "wait_timeout": 30,
                    "wait_interval": 2,
                    "providers": [{
                        "enable": True,
                        "type": "cloudflare_temp_email",
                        "api_base": "https://mail.example.test",
                        "admin_password": "test-only",
                        "domain": ["deleted.example", "kept.example"],
                    }],
                }
                service.update({"mail": mail_config})
                mail_provider.mark_mailbox_result(
                    {"provider": "cloudflare_temp_email", "address": "person@deleted.example"},
                    success=False,
                )
                mail_provider.mark_mailbox_result(
                    {"provider": "cloudflare_temp_email", "address": "person@kept.example"},
                    success=True,
                )

                mail_config["providers"][0]["domain"] = ["kept.example", "new.example"]
                snapshot = service.update({"mail": mail_config})
                stats = {item["domain"]: item for item in snapshot["cloudflare_domain_stats"]}
                saved = json.loads(store_file.read_text(encoding="utf-8"))
                saved_stats = {item["domain"]: item for item in saved["cloudflare_domain_stats"]}

                # 视图同时包含：仍在配置里的域名（含尚无结果的 new.example）与已删除的 deleted.example
                self.assertEqual(set(stats), {"deleted.example", "kept.example", "new.example"})
                self.assertEqual(stats["kept.example"]["success"], 1)
                self.assertEqual(stats["new.example"]["total"], 0)
                self.assertTrue(stats["new.example"]["configured"])
                self.assertFalse(stats["deleted.example"]["configured"])
                self.assertEqual(stats["deleted.example"]["fail"], 1)

                # 删除域名不再清空累计值；只有产生过结果的域名才写入存储
                self.assertEqual(set(saved_stats), {"deleted.example", "kept.example"})
                self.assertEqual(saved_stats["deleted.example"]["fail"], 1)
                self.assertNotIn("new.example", saved_stats)

                # 删除域名后迟到的注册结果依旧计数，而不是被丢弃
                mail_provider.mark_mailbox_result(
                    {"provider": "cloudflare_temp_email", "address": "person@deleted.example"},
                    success=True,
                )
                saved_after = {
                    item["domain"]: item
                    for item in json.loads(
                        store_file.read_text(encoding="utf-8")
                    )["cloudflare_domain_stats"]
                }
                self.assertEqual(saved_after["deleted.example"]["success"], 1)
                self.assertEqual(saved_after["deleted.example"]["fail"], 1)

                # 重新加载后历史累计值不丢
                reloaded = register_module.RegisterService(store_file)
                reloaded_stats = {
                    item["domain"]: item
                    for item in reloaded.get()["cloudflare_domain_stats"]
                }
                self.assertEqual(reloaded_stats["deleted.example"]["total"], 2)
                self.assertFalse(reloaded_stats["deleted.example"]["configured"])
                self.assertEqual(reloaded_stats["kept.example"]["success"], 1)
        finally:
            mail_provider.mailbox_result_sink = original_result_sink
            register_module.openai_register.register_log_sink = original_log_sink


if __name__ == "__main__":
    unittest.main()
