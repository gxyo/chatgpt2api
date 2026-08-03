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


if __name__ == "__main__":
    unittest.main()
