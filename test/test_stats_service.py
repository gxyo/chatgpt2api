from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services import stats_service
from services.stats_service import ImageStatsService, beijing_today


def log_line(time_text: str, endpoint: str, status: str = "success", duration_ms: int = 100,
             log_type: str = "call", summary: str = "调用") -> str:
    detail = {"endpoint": endpoint, "status": status, "duration_ms": duration_ms, "ended_at": time_text}
    return json.dumps(
        {"id": f"id-{time_text}-{endpoint}-{status}", "time": time_text, "type": log_type, "summary": summary, "detail": detail},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ImageStatsServiceTests(unittest.TestCase):
    def make_service(self, tmp_dir: str, lines: list[str]) -> ImageStatsService:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ImageStatsService(path)

    def test_single_day_returns_24_hourly_points(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 09:05:00", "/v1/images/generations"),
                log_line("2026-09-28 09:45:00", "/v1/images/generations", status="failed"),
                log_line("2026-09-28 21:00:00", "/v1/images/edits"),
            ])

            result = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["range"]["granularity"], "hour")
        self.assertEqual(len(result["series"]), 24)
        self.assertEqual(result["range"]["start_date"], "2026-09-28")
        self.assertEqual(result["totals"], {
            "requests": 3, "success": 2, "failed": 1,
            "success_rate": 2 / 3, "avg_duration_ms": 100,
        })
        by_label = {point["label"]: point for point in result["series"]}
        self.assertEqual(by_label["09:00"]["requests"], 2)
        self.assertEqual(by_label["09:00"]["success"], 1)
        self.assertEqual(by_label["09:00"]["failed"], 1)
        self.assertEqual(by_label["21:00"]["requests"], 1)
        self.assertEqual(by_label["00:00"]["requests"], 0)

    def test_date_range_is_aggregated_by_day(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-27 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 10:00:00", "/v1/images/generations", status="failed"),
                log_line("2026-09-28 23:00:00", "/v1/images/generations"),
            ])

            result = service.summary("2026-09-27", "2026-09-28")

        self.assertEqual(result["range"]["granularity"], "day")
        self.assertEqual(result["range"]["days"], 2)
        self.assertEqual([point["label"] for point in result["series"]], ["09-27", "09-28"])
        self.assertEqual([point["requests"] for point in result["series"]], [1, 2])
        self.assertEqual(result["totals"]["requests"], 3)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["peak"]["full_label"], "2026-09-28")
        self.assertEqual(result["peak"]["requests"], 2)

    def test_days_without_traffic_are_filled_with_zeroes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-27 10:00:00", "/v1/images/generations"),
            ])

            result = service.summary("2026-09-27", "2026-09-30")

        self.assertEqual([point["requests"] for point in result["series"]], [1, 0, 0, 0])
        self.assertEqual(result["peak"]["label"], "09-27")

    def test_task_endpoint_failures_count_once_and_task_successes_use_image_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                # 网页端任务成功：由 image_task_service 以 /v1/images/* 记一次 success
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                # 网页端任务被内容审核拦截：以 /api/image-tasks/* 记一次 failed
                log_line("2026-09-28 11:00:00", "/api/image-tasks/generations", status="failed"),
                # 同一 endpoint 的 success 不应被统计（避免与上面的 /v1 重复计数）
                log_line("2026-09-28 12:00:00", "/api/image-tasks/edits"),
            ])

            result = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["requests"], 2)
        self.assertEqual(result["totals"]["success"], 1)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["by_mode"], [
            {"mode": "generate", "label": "文生图", "requests": 2, "success": 1, "failed": 1},
            {"mode": "edit", "label": "图生图", "requests": 0, "success": 0, "failed": 0},
        ])

    def test_non_image_and_non_call_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 10:00:00", "/v1/messages"),
                log_line("2026-09-28 10:00:00", "/v1/images/generations", log_type="account"),
                "{ this is not json",
            ])

            result = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["requests"], 1)

    def test_empty_range_defaults_to_beijing_today(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [])

            result = service.summary()

        self.assertEqual(result["range"]["start_date"], beijing_today())
        self.assertEqual(result["range"]["end_date"], beijing_today())
        self.assertEqual(result["range"]["granularity"], "hour")
        self.assertEqual(result["totals"]["requests"], 0)
        self.assertEqual(result["totals"]["success_rate"], 0.0)
        self.assertIsNone(result["peak"])

    def test_single_sided_and_reversed_ranges_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [])

            start_only = service.summary("2026-09-28", "")
            reversed_range = service.summary("2026-09-30", "2026-09-28")

        self.assertEqual(start_only["range"]["end_date"], "2026-09-28")
        self.assertEqual(start_only["range"]["granularity"], "hour")
        self.assertEqual(reversed_range["range"]["start_date"], "2026-09-28")
        self.assertEqual(reversed_range["range"]["end_date"], "2026-09-30")

    def test_malformed_date_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [])

            with self.assertRaises(ValueError):
                service.summary("2026-9-28", "")

    def test_long_ranges_are_clamped_to_the_most_recent_window(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [])

            result = service.summary("2000-01-01", "2026-09-30")

        self.assertEqual(result["range"]["days"], stats_service.MAX_SERIES_POINTS)
        self.assertEqual(result["range"]["end_date"], "2026-09-30")
        self.assertEqual(len(result["series"]), stats_service.MAX_SERIES_POINTS)

    def test_aggregate_cache_refreshes_when_the_log_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(log_line("2026-09-28 10:00:00", "/v1/images/generations") + "\n", encoding="utf-8")
            service = ImageStatsService(path)

            first = service.summary("2026-09-28", "2026-09-28")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line("2026-09-28 10:05:00", "/v1/images/generations", status="failed") + "\n")
            second = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(first["totals"]["requests"], 1)
        self.assertEqual(second["totals"]["requests"], 2)
        self.assertEqual(second["totals"]["failed"], 1)


class IncrementalAggregationTests(unittest.TestCase):
    """增量聚合：只解析新追加的尾部，且文件被改写时不能留下陈旧计数。"""

    def make_service(self, tmp_dir: str, lines: list[str]) -> tuple[ImageStatsService, Path]:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ImageStatsService(path), path

    def test_repeated_queries_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, _ = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
            ])

            counts = [service.summary("2026-09-28", "2026-09-28")["totals"]["requests"] for _ in range(5)]

        self.assertEqual(counts, [1, 1, 1, 1, 1])

    def test_appends_are_merged_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
            ])
            service.summary("2026-09-28", "2026-09-28")

            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line("2026-09-28 11:00:00", "/v1/images/edits", status="failed") + "\n")
            after_one = service.summary("2026-09-28", "2026-09-28")

            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line("2026-09-28 12:00:00", "/v1/images/generations") + "\n")
            after_two = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(after_one["totals"]["requests"], 2)
        self.assertEqual(after_two["totals"]["requests"], 3)
        self.assertEqual(after_two["totals"]["failed"], 1)

    def test_trailing_partial_line_waits_until_it_is_completed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            # 模拟「日志正在写」：最后一行还没落换行符。
            path.write_text(log_line("2026-09-28 10:00:00", "/v1/images/generations"), encoding="utf-8")
            service = ImageStatsService(path)

            while_incomplete = service.summary("2026-09-28", "2026-09-28")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            after_complete = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(while_incomplete["totals"]["requests"], 0)
        self.assertEqual(after_complete["totals"]["requests"], 1)

    def test_truncated_file_is_rebuilt_from_scratch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 11:00:00", "/v1/images/generations"),
            ])
            before = service.summary("2026-09-28", "2026-09-28")

            path.write_text(log_line("2026-09-28 12:00:00", "/v1/images/generations") + "\n", encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(before["totals"]["requests"], 2)
        self.assertEqual(after["totals"]["requests"], 1)
        self.assertEqual(after["series"][10]["requests"], 0)
        self.assertEqual(after["series"][12]["requests"], 1)

    def test_in_place_rewrite_is_detected_even_when_the_file_grows(self):
        """删除部分日志会原地重写文件；若新内容比旧偏移更长，只靠大小判断会漏掉。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
            ])
            before = service.summary("2026-09-28", "2026-09-28")

            # 重写成前缀不同、但总体更长的内容，且不含任何生图调用。
            rewritten = "".join(
                log_line(f"2026-09-28 13:{minute:02d}:00", "/v1/messages", summary="无关调用")
                for minute in range(10)
            )
            path.write_text(rewritten, encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(before["totals"]["requests"], 1)
        self.assertEqual(after["totals"]["requests"], 0)

    def test_deleted_log_file_keeps_the_last_known_aggregate(self):
        """文件暂时读不到时保持现状，不把已有统计清零。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
            ])
            service.summary("2026-09-28", "2026-09-28")

            path.unlink()
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(after["totals"]["requests"], 1)


if __name__ == "__main__":
    unittest.main()
