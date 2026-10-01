from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from services import log_cleanup_service
from services.config import MAX_LOG_RETENTION_DAYS, ConfigStore
from services.log_cleanup_service import (
    cleanup_logs,
    cutoff_day_for,
    log_storage_info,
    next_cleanup_at,
    run_scheduled_log_cleanup_if_due,
)
from services.log_service import LogService
from utils.beijing_time import beijing_now


def log_line(time_text: str, log_type: str = "call") -> str:
    return json.dumps(
        {"id": f"id-{time_text}", "time": time_text, "type": log_type, "summary": "调用", "detail": {}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def beijing_today() -> str:
    return beijing_now().strftime("%Y-%m-%d")


class StubSettings:
    """替代 ConfigStore，只暴露清理逻辑真正读的那几个开关。"""

    def __init__(self, *, days: int = 2, auto: bool = True, cleanup_time: str = "03:00"):
        self.log_retention_days = days
        self.log_auto_cleanup = auto
        self.log_cleanup_time = cleanup_time


class LogServiceCleanupTests(unittest.TestCase):
    def make_service(self, tmp_dir: str, lines: list[str]) -> LogService:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return LogService(path)

    @staticmethod
    def remaining_days(service: LogService) -> list[str]:
        raw_lines = service.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(raw)["time"][:10] for raw in raw_lines]

    def test_cleanup_removes_only_lines_before_the_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-27 10:00:00"),
                log_line("2026-09-28 10:00:00"),
                log_line("2026-09-29 10:00:00"),
            ])

            result = service.cleanup_before("2026-09-29")

            self.assertEqual(result, {"removed": 2, "kept": 1})
            self.assertEqual(self.remaining_days(service), ["2026-09-29"])

    def test_cleanup_keeps_unparseable_lines(self):
        """读不懂的行一律留着，宁可少删也不丢内容。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-27 10:00:00"),
                "{ not json",
                log_line("2026-09-29 10:00:00"),
            ])

            result = service.cleanup_before("2026-09-29")

            self.assertEqual(result["removed"], 1)
            self.assertIn("{ not json", service.path.read_text(encoding="utf-8"))

    def test_cleanup_preserves_the_original_bytes_of_kept_lines(self):
        """保留的行必须原样写回：统计服务靠比对字节来推进增量游标。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 多出来的空格和键顺序都会被 json.dumps 重新序列化时改掉。
            handcrafted = '{"type": "call", "time": "2026-09-29 10:00:00",   "id": "abc", "detail": {}, "summary": "x"}'
            service = self.make_service(tmp_dir, [log_line("2026-09-27 10:00:00"), handcrafted])

            service.cleanup_before("2026-09-29")

            self.assertEqual(service.path.read_text(encoding="utf-8"), f"{handcrafted}\n")

    def test_cleanup_on_a_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "missing.jsonl")

            self.assertEqual(service.cleanup_before("2026-09-29"), {"removed": 0, "kept": 0})
            self.assertFalse(service.path.exists())

    def test_everything_old_leaves_an_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [log_line("2026-09-27 10:00:00")])

            service.cleanup_before("2026-09-29")

            self.assertEqual(service.path.read_text(encoding="utf-8"), "")


class RetentionWindowTests(unittest.TestCase):
    def test_keeping_one_day_means_keeping_only_today(self):
        self.assertEqual(cutoff_day_for(1), beijing_today())

    def test_keeping_seven_days_cuts_at_today_minus_six(self):
        expected = (beijing_now() - timedelta(days=6)).strftime("%Y-%m-%d")
        self.assertEqual(cutoff_day_for(7), expected)

    def test_non_positive_retention_is_clamped_to_one_day(self):
        self.assertEqual(cutoff_day_for(0), cutoff_day_for(1))
        self.assertEqual(cutoff_day_for(-5), cutoff_day_for(1))


class LogRetentionConfigTests(unittest.TestCase):
    def make_store(self, tmp_dir: str, data: dict[str, object] | None = None) -> ConfigStore:
        payload = {"auth-key": "test-auth", **(data or {})}
        path = Path(tmp_dir) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return ConfigStore(path)

    def test_defaults_are_thirty_days_and_auto_cleanup_off(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = self.make_store(tmp_dir)

            self.assertEqual(store.log_retention_days, 30)
            self.assertFalse(store.log_auto_cleanup)

    def test_retention_days_are_clamped_and_published(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            for raw, expected in ((0, 1), (-3, 1), (99999, MAX_LOG_RETENTION_DAYS), ("abc", 30)):
                store = self.make_store(tmp_dir, {"log_retention_days": raw})
                self.assertEqual(store.log_retention_days, expected, raw)

            self.assertEqual(self.make_store(tmp_dir).get()["log_retention_days"], 30)

    def test_auto_cleanup_only_accepts_an_explicit_true(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            for raw in ("true", 1, "yes", None):
                store = self.make_store(tmp_dir, {"log_auto_cleanup": raw})
                self.assertFalse(store.log_auto_cleanup, raw)

            store = self.make_store(tmp_dir, {"log_auto_cleanup": True})
            self.assertTrue(store.log_auto_cleanup)

    def test_update_normalizes_and_persists_the_retention_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = self.make_store(tmp_dir)

            store.update({"log_retention_days": 0, "log_auto_cleanup": True})

            self.assertEqual(store.log_retention_days, 1)
            self.assertTrue(store.log_auto_cleanup)
            self.assertEqual(json.loads((Path(tmp_dir) / "config.json").read_text(encoding="utf-8"))["log_retention_days"], 1)

    def test_cleanup_time_defaults_to_three_and_is_published(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = self.make_store(tmp_dir)

            self.assertEqual(store.log_cleanup_time, "03:00")
            self.assertEqual(store.get()["log_cleanup_time"], "03:00")

    def test_cleanup_time_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            for raw, expected in (("7:05", "07:05"), ("23:59", "23:59"), ("", "03:00"), ("25:99", "03:00"), ("abc", "03:00")):
                store = self.make_store(tmp_dir, {"log_cleanup_time": raw})
                self.assertEqual(store.log_cleanup_time, expected, raw)

            store = self.make_store(tmp_dir)
            store.update({"log_cleanup_time": "6:30"})
            self.assertEqual(store.log_cleanup_time, "06:30")
            self.assertEqual(json.loads((Path(tmp_dir) / "config.json").read_text(encoding="utf-8"))["log_cleanup_time"], "06:30")


class CleanupReportTests(unittest.TestCase):
    """手动清理的返回值：页面要拿它提示删了多少、还剩多大。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "logs.jsonl"
        self.log_path.write_text(log_line("2020-01-01 10:00:00") + "\n" + log_line("2020-01-02 10:00:00") + "\n", encoding="utf-8")

        for target, value in (
            ("config", StubSettings(days=7, auto=True)),
            ("log_service", LogService(self.log_path)),
        ):
            patcher = mock.patch.object(log_cleanup_service, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cleanup_reports_removed_kept_and_the_new_size(self):
        result = cleanup_logs()

        self.assertEqual(result["days"], 7)
        self.assertEqual(result["cutoff_day"], cutoff_day_for(7))
        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["kept"], 0)
        self.assertEqual(result["size_bytes"], 0)

    def test_explicit_days_override_the_configured_retention(self):
        result = cleanup_logs(1)

        self.assertEqual(result["days"], 1)
        self.assertEqual(result["cutoff_day"], cutoff_day_for(1))

    def test_storage_info_reports_size_and_settings(self):
        info = log_storage_info()

        self.assertEqual(info["size_bytes"], self.log_path.stat().st_size)
        self.assertEqual(info["days"], 7)
        self.assertIs(info["auto_cleanup"], True)
        self.assertEqual(info["cleanup_time"], "03:00")

    def test_storage_info_tolerates_a_missing_file(self):
        self.log_path.unlink()

        self.assertEqual(log_storage_info()["size_bytes"], 0)


class ScheduledLogCleanupTests(unittest.TestCase):
    """每日自动清理：默认关闭，开启后也要跨天才动手。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.log_path = root / "logs.jsonl"
        self.state_path = root / "log_cleanup_state.json"
        self.settings = StubSettings(days=2, auto=True)

        for target, value in (
            ("config", self.settings),
            ("log_service", LogService(self.log_path)),
            ("STATE_FILE", self.state_path),
        ):
            patcher = mock.patch.object(log_cleanup_service, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_logs(self, days: list[str]) -> None:
        self.log_path.write_text("\n".join(log_line(f"{day} 10:00:00") for day in days) + "\n", encoding="utf-8")

    def test_disabled_auto_cleanup_does_nothing(self):
        self.settings.log_auto_cleanup = False
        self.write_logs(["2020-01-01"])

        self.assertIsNone(run_scheduled_log_cleanup_if_due())
        self.assertIn("2020-01-01", self.log_path.read_text(encoding="utf-8"))
        self.assertFalse(self.state_path.exists())

    def at(self, text: str) -> datetime:
        return datetime.fromisoformat(text)

    def arm(self, *, schedule: str = "03:00", next_run_at: str = "2026-10-02T03:00:00") -> None:
        self.state_path.write_text(
            json.dumps({"schedule": f"{schedule}@+08:00", "next_run_at": next_run_at}),
            encoding="utf-8",
        )

    def test_first_run_only_records_the_next_run_without_deleting(self):
        """刚打开开关时先不动数据，给用户一个反悔的机会。"""
        self.write_logs(["2020-01-01", f"{beijing_today()} 10:00:00"])

        self.assertIsNone(run_scheduled_log_cleanup_if_due(self.at("2026-10-01T02:00:00")))

        self.assertIn("2020-01-01", self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8")),
            {"schedule": "03:00@+08:00", "next_run_at": "2026-10-01T03:00:00"},
        )

    def test_before_the_scheduled_time_nothing_happens(self):
        self.arm()
        self.write_logs(["2020-01-01"])

        self.assertIsNone(run_scheduled_log_cleanup_if_due(self.at("2026-10-02T02:59:00")))

        self.assertIn("2020-01-01", self.log_path.read_text(encoding="utf-8"))

    def test_at_the_scheduled_time_the_cleanup_runs_once(self):
        self.arm()
        self.write_logs(["2020-01-01", f"{beijing_today()} 10:00:00"])

        result = run_scheduled_log_cleanup_if_due(self.at("2026-10-02T03:00:00"))

        self.assertEqual(result["removed"], 1)
        self.assertNotIn("2020-01-01", self.log_path.read_text(encoding="utf-8"))
        # 同一天再跑一次应当是空转，下一次要等到明天。
        self.assertIsNone(run_scheduled_log_cleanup_if_due(self.at("2026-10-02T03:01:00")))
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))["next_run_at"], "2026-10-03T03:00:00"
        )

    def test_a_missed_slot_is_not_replayed(self):
        """关机错过一次就跳过，不补跑：只留下一次的时刻。"""
        self.arm(next_run_at="2026-10-01T03:00:00")
        self.write_logs(["2020-01-01", f"{beijing_today()} 10:00:00"])

        run_scheduled_log_cleanup_if_due(self.at("2026-10-05T09:00:00"))

        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))["next_run_at"], "2026-10-06T03:00:00"
        )

    def test_changing_the_time_re_arms_without_deleting(self):
        self.arm(schedule="03:00", next_run_at="2026-10-02T03:00:00")
        self.write_logs(["2020-01-01"])
        self.settings.log_cleanup_time = "06:30"

        self.assertIsNone(run_scheduled_log_cleanup_if_due(self.at("2026-10-02T03:00:00")))

        self.assertIn("2020-01-01", self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8")),
            {"schedule": "06:30@+08:00", "next_run_at": "2026-10-02T06:30:00"},
        )

    def test_next_cleanup_at_rolls_over_to_tomorrow(self):
        self.assertEqual(
            next_cleanup_at(datetime(2026, 10, 1, 1, 0, 0), "03:00").isoformat(),
            "2026-10-01T03:00:00",
        )
        self.assertEqual(
            next_cleanup_at(datetime(2026, 10, 1, 3, 0, 0), "03:00").isoformat(),
            "2026-10-02T03:00:00",
        )


if __name__ == "__main__":
    unittest.main()
