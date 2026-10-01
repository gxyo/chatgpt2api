import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from services import register_service as register_module
from services import register_stats_service
from services.register import mail_provider


class RegisterStatsStoreTests(unittest.TestCase):
    def _store(self, path: Path, when: datetime) -> register_stats_service.RegisterStatsStore:
        # 注入固定时钟，而不是打补丁改 utils.beijing_time，测试才是确定性的。
        return register_stats_service.RegisterStatsStore(path, now=lambda: when)

    def test_single_day_series_is_hourly_and_totals_add_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = self._store(Path(tmp_dir) / "register_stats.json", datetime(2026, 9, 28, 9, 5))
            for _ in range(3):
                store.record(success=True)
            store.record(success=False)

            result = store.history("2026-09-28", "2026-09-28")

            self.assertEqual(result["range"]["granularity"], "hour")
            self.assertEqual(len(result["series"]), 24)
            self.assertEqual(result["totals"]["success"], 3)
            self.assertEqual(result["totals"]["failed"], 1)
            self.assertEqual(result["totals"]["requests"], 4)

            # 计数必须落在 09:00 这个桶上，其余小时为零。
            by_label = {point["label"]: point for point in result["series"]}
            self.assertEqual(by_label["09:00"]["success"], 3)
            self.assertEqual(by_label["09:00"]["failed"], 1)
            self.assertEqual(by_label["08:00"]["requests"], 0)
            for point in result["series"]:
                self.assertEqual(point["requests"], point["success"] + point["failed"])

            # 主线是成功数，峰值就该是成功数最大的那一个小时。
            self.assertEqual(result["peak"]["label"], "09:00")
            self.assertEqual(result["peak"]["success"], 3)

    def test_multi_day_series_is_daily_and_zero_fills_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "register_stats.json"
            store = self._store(path, datetime(2026, 9, 28, 9, 5))
            store.record(success=True)
            store.record(success=False)

            later = self._store(path, datetime(2026, 9, 30, 23, 30))
            later.record(success=True)

            result = later.history("2026-09-28", "2026-09-30")

            self.assertEqual(result["range"]["granularity"], "day")
            self.assertEqual([point["key"] for point in result["series"]], ["2026-09-28", "2026-09-29", "2026-09-30"])
            self.assertEqual(result["series"][1]["requests"], 0)  # 没有记录的那天补 0，不是缺口
            self.assertEqual(result["totals"]["success"], 2)
            self.assertEqual(result["totals"]["failed"], 1)

    def test_scope_all_starts_at_earliest_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "register_stats.json"
            store = self._store(path, datetime(2026, 9, 28, 9, 5))
            store.record(success=True)
            later = self._store(path, datetime(2026, 9, 30, 9, 5))
            later.record(success=True)

            result = later.history(scope="all")

            self.assertEqual(result["range"]["scope"], "all")
            self.assertEqual(result["range"]["start_date"], "2026-09-28")
            self.assertGreaterEqual(result["range"]["end_date"], "2026-09-30")

    def test_invalid_date_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = self._store(Path(tmp_dir) / "register_stats.json", datetime(2026, 9, 28, 9, 5))
            # 未补零的写法 strptime 能接受，但匹配不上账本里的小时键，必须显式挡掉。
            with self.assertRaises(ValueError):
                store.history("2026-9-28", "2026-09-30")

    def test_corrupt_sidecar_is_ignored_and_rebuilt_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "register_stats.json"
            path.write_text("{ not json", encoding="utf-8")

            store = self._store(path, datetime(2026, 9, 28, 9, 5))
            result = store.history("2026-09-28", "2026-09-28")

            self.assertEqual(result["totals"]["requests"], 0)
            self.assertIsNone(result["peak"])

    def test_counts_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "register_stats.json"
            store = self._store(path, datetime(2026, 9, 28, 9, 5))
            store.record(success=True)
            # 小时翻转时强制落盘，不依赖节流窗口。
            reborn = self._store(path, datetime(2026, 9, 28, 10, 5))
            reborn.record(success=False)

            reloaded = self._store(path, datetime(2026, 9, 28, 11, 0)).history("2026-09-28", "2026-09-28")
            self.assertEqual(reloaded["totals"]["success"], 1)
            self.assertEqual(reloaded["totals"]["failed"], 1)

    def test_old_buckets_are_pruned_past_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "register_stats.json"
            stale = self._store(path, datetime(2024, 1, 1, 9, 0))
            stale.record(success=True)

            # 保留期是 400 天，两年后的这一小时会把 2024-01-01 的桶裁掉。
            fresh = self._store(path, datetime(2026, 9, 28, 9, 0))
            fresh.record(success=True)

            result = fresh.history(scope="all")
            self.assertEqual(result["range"]["start_date"], "2026-09-28")
            self.assertEqual(result["totals"]["requests"], 1)


class RegisterStatsIntegrationTests(unittest.TestCase):
    def _service(self, tmp_dir: str) -> register_module.RegisterService:
        return register_module.RegisterService(Path(tmp_dir) / "register.json")

    def _run_events(self, events: list[bool], address: str = "person@Example.COM.") -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(
                lambda succeeded: mail_provider.mark_mailbox_result(
                    {"provider": "cloudflare_temp_email", "address": address},
                    success=succeeded,
                    error=None if succeeded else "registration failed",
                ),
                events,
            ))

    def test_results_are_counted_into_hourly_history(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        original_log_sink = register_module.openai_register.register_log_sink
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = self._service(tmp_dir)
                self._run_events([True] * 20 + [False] * 7)

                history = service.history(scope="all")
                self.assertEqual(history["totals"]["requests"], 27)
                self.assertEqual(history["totals"]["success"], 20)
                self.assertEqual(history["totals"]["failed"], 7)

                # 账本是旁路文件，不能挤进 register.json —— 那份快照每 0.5s 会被 SSE 全量广播。
                saved = json.loads((Path(tmp_dir) / "register.json").read_text(encoding="utf-8"))
                self.assertNotIn("register_stats", saved)
                self.assertTrue((Path(tmp_dir) / "register_stats.json").exists())

                mail_provider.mark_mailbox_result(
                    {"provider": "tempmail_lol", "address": "person@ignored.example"},
                    success=True,
                )
                self.assertEqual(service.history(scope="all")["totals"]["requests"], 27)
        finally:
            mail_provider.mailbox_result_sink = original_result_sink
            register_module.openai_register.register_log_sink = original_log_sink

    def test_reset_does_not_clear_history(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        original_log_sink = register_module.openai_register.register_log_sink
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = self._service(tmp_dir)
                self._run_events([True, False])
                service.reset()

                # 注册统计页承诺「「重置」不会清空这里的数据」，和 cloudflare_domain_stats 同口径。
                self.assertEqual(service.history(scope="all")["totals"]["requests"], 2)
                self.assertEqual(service.get()["stats"]["success"], 0)
        finally:
            mail_provider.mailbox_result_sink = original_result_sink
            register_module.openai_register.register_log_sink = original_log_sink

    def test_snapshot_stays_byte_stable_for_sse(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        original_log_sink = register_module.openai_register.register_log_sink
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = self._service(tmp_dir)
                self._run_events([True, False])

                # api/register.py 的 SSE 靠逐字节比对 json.dumps(get()) 决定要不要推送；
                # 两次序列化不一致就等于每 0.5 秒给所有客户端全量重推一遍。
                first = json.dumps(service.get(), ensure_ascii=False)
                second = json.dumps(service.get(), ensure_ascii=False)
                self.assertEqual(first, second)
                self.assertNotIn("register_stats", first)
        finally:
            mail_provider.mailbox_result_sink = original_result_sink
            register_module.openai_register.register_log_sink = original_log_sink

    def test_broken_sidecar_does_not_break_registration(self) -> None:
        original_result_sink = mail_provider.mailbox_result_sink
        original_log_sink = register_module.openai_register.register_log_sink
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                (Path(tmp_dir) / "register_stats.json").write_text("[]", encoding="utf-8")
                service = self._service(tmp_dir)
                self._run_events([True])

                # 账本格式不对应当被当成空账本重建，绝不能把注册结果连带拖垮。
                self.assertEqual(service.history(scope="all")["totals"]["success"], 1)
                # 按域名取而不是按下标：视图里还会出现「已配置但尚无结果」的零计数占位行，
                # 其顺序取决于 openai_register.config 这个全局配置，测试之间会互相影响。
                rows = {item["domain"]: item for item in service.get()["cloudflare_domain_stats"]}
                self.assertEqual(rows["example.com"]["success"], 1)
        finally:
            mail_provider.mailbox_result_sink = original_result_sink
            register_module.openai_register.register_log_sink = original_log_sink


if __name__ == "__main__":
    unittest.main()
