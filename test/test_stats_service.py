from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import stats_service
from services.stats_service import ImageStatsService, beijing_today


def log_line(time_text: str, endpoint: str, status: str = "success", duration_ms: int = 100,
             log_type: str = "call", summary: str = "调用", model: str | None = None,
             urls: list[str] | None = None) -> str:
    detail = {"endpoint": endpoint, "status": status, "duration_ms": duration_ms, "ended_at": time_text}
    if model is not None:
        detail["model"] = model
    if urls is not None:
        detail["urls"] = urls
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
            "requests": 3, "success": 2, "failed": 1, "policy_violation": 0,
            "unsupported_app_response": 0,
            "success_rate": 2 / 3, "avg_duration_ms": 100, "images": 0, "revenue": 0,
            "legacy_revenue": 0, "legacy_success": 0,
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
                log_line("2026-09-27 10:00:00", "/v1/images/generations",
                         urls=["https://img/1"]),
                log_line("2026-09-28 10:00:00", "/v1/images/generations", status="failed"),
                log_line("2026-09-28 23:00:00", "/v1/images/generations",
                         urls=["https://img/2", "https://img/3"]),
            ])

            result = service.summary("2026-09-27", "2026-09-28")

        self.assertEqual(result["range"]["granularity"], "day")
        self.assertEqual(result["range"]["days"], 2)
        self.assertEqual([point["label"] for point in result["series"]], ["09-27", "09-28"])
        self.assertEqual([point["requests"] for point in result["series"]], [1, 2])
        self.assertEqual([point["images"] for point in result["series"]], [1, 2])
        self.assertEqual(result["totals"]["requests"], 3)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["peak"]["full_label"], "2026-09-28")
        self.assertEqual(result["peak"]["images"], 2)

    def test_days_without_traffic_are_filled_with_zeroes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-27 10:00:00", "/v1/images/generations",
                         urls=["https://img/1"]),
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
            {"mode": "generate", "label": "文生图", "requests": 2, "success": 1, "failed": 1, "policy_violation": 0, "unsupported_app_response": 0, "images": 0},
            {"mode": "edit", "label": "图生图", "requests": 0, "success": 0, "failed": 0, "policy_violation": 0, "unsupported_app_response": 0, "images": 0},
        ])

    def test_policy_violations_are_counted_separately_from_failures(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 10:30:00", "/v1/images/generations", status="failed"),
                log_line("2026-09-28 11:00:00", "/v1/images/generations", status="policy_violation"),
                # 网页端任务被本地审核拦截：也记在任务接口下，同样单独计数。
                log_line("2026-09-28 12:00:00", "/api/image-tasks/edits", status="policy_violation"),
            ])

            result = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["requests"], 4)
        self.assertEqual(result["totals"]["success"], 1)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["totals"]["policy_violation"], 2)
        # 成功率的分母只有成功 + 失败：1 成功、1 失败 → 50%，两条拦截不参与。
        self.assertEqual(result["totals"]["success_rate"], 0.5)
        by_label = {point["label"]: point for point in result["series"]}
        self.assertEqual(by_label["11:00"]["policy_violation"], 1)
        self.assertEqual(by_label["11:00"]["failed"], 0)

    def test_unsupported_app_responses_are_counted_separately_from_failures(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 10:30:00", "/v1/images/generations", status="failed"),
                log_line("2026-09-28 11:00:00", "/v1/images/generations", status="unsupported_app_response"),
                # 网页端任务的同类失败也必须计入，不能因为走任务接口就漏掉。
                log_line("2026-09-28 12:00:00", "/api/image-tasks/edits", status="unsupported_app_response"),
            ])

            result = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["requests"], 4)
        self.assertEqual(result["totals"]["success"], 1)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["totals"]["unsupported_app_response"], 2)
        # 成功率的分母只有成功 + 失败：应用响应和内容拦截都不参与。
        self.assertEqual(result["totals"]["success_rate"], 0.5)
        by_label = {point["label"]: point for point in result["series"]}
        self.assertEqual(by_label["11:00"]["unsupported_app_response"], 1)
        self.assertEqual(by_label["11:00"]["failed"], 0)

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

    def test_scope_all_spans_from_the_earliest_recorded_day(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 09:00:00", "/v1/images/generations"),
                log_line("2026-09-30 10:00:00", "/v1/images/edits"),
            ])

            result = service.summary(scope="all")

        self.assertEqual(result["range"]["scope"], "all")
        self.assertEqual(result["range"]["start_date"], "2026-09-28")
        self.assertEqual(result["range"]["end_date"], beijing_today())
        self.assertEqual(result["range"]["granularity"], "day")
        self.assertEqual(result["totals"]["requests"], 2)

    def test_scope_all_without_records_falls_back_to_today(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [])

            result = service.summary(scope="all")

        self.assertEqual(result["range"]["start_date"], beijing_today())
        self.assertEqual(result["range"]["end_date"], beijing_today())
        self.assertEqual(result["range"]["granularity"], "hour")
        self.assertEqual(len(result["series"]), 24)
        self.assertEqual(result["totals"]["requests"], 0)

    def test_scope_all_keeps_days_that_the_log_no_longer_has(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(
                log_line("2026-09-28 09:00:00", "/v1/images/generations") + "\n"
                + log_line("2026-09-30 09:00:00", "/v1/images/generations") + "\n",
                encoding="utf-8",
            )
            service = ImageStatsService(path)
            service.summary("2026-09-28", "2026-09-30")

            # 按保留期清理掉最老的一天：聚合只增不减，「全部」仍应把它算进来。
            path.write_text(log_line("2026-09-30 09:00:00", "/v1/images/generations") + "\n", encoding="utf-8")
            result = service.summary(scope="all")

        self.assertEqual(result["range"]["start_date"], "2026-09-28")
        self.assertEqual(result["totals"]["requests"], 2)

    def test_non_all_scope_reports_the_range_scope(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 09:00:00", "/v1/images/generations"),
            ])

            result = service.summary("2026-09-28", "2026-09-28", "  ")

        self.assertEqual(result["range"]["scope"], "range")

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

    def test_truncated_file_keeps_counted_requests_and_counts_the_new_line(self):
        """文件被截断后：已计入的请求不回退，替换进来的新行必须计入而不是被丢掉。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 11:00:00", "/v1/images/generations"),
            ])
            before = service.summary("2026-09-28", "2026-09-28")

            path.write_text(log_line("2026-09-28 12:00:00", "/v1/images/generations") + "\n", encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(before["totals"]["requests"], 2)
        # 2 条老请求（已计入、不回退）+ 截断后写进来的 1 条新请求。
        self.assertEqual(after["totals"]["requests"], 3)

    def test_rewritten_log_without_any_old_line_counts_the_new_file(self):
        """日志被按保留期整份清掉后重写：老统计保留，新文件里的请求一条都不能漏。

        旧逻辑在这里把游标直接退到文件末尾，结果「打开统计页之前」产生的请求全部
        不计入；线上表现为当天只统计到最近几十分钟。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-27 10:00:00", "/v1/images/generations"),
                log_line("2026-09-27 11:00:00", "/v1/images/generations"),
            ])
            before = service.summary("2026-09-27", "2026-09-27")

            fresh = [
                log_line(f"2026-09-28 12:{minute:02d}:00", "/v1/images/generations")
                for minute in range(30)
            ]
            path.write_text("\n".join(fresh) + "\n", encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(before["totals"]["requests"], 2)
        self.assertEqual(after["totals"]["requests"], 30)

    def test_deleting_the_newest_consumed_line_loses_and_double_counts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = log_line("2026-09-28 10:00:00", "/v1/images/generations")
            second = log_line("2026-09-28 11:00:00", "/v1/images/generations")
            third = log_line("2026-09-28 12:00:00", "/v1/images/generations")
            service, path = self.make_service(tmp_dir, [first, second, third])
            before = service.summary("2026-09-28", "2026-09-28")

            # 删掉最新一条（尾部字节锚点随之消失），再写入一条新的日志。
            fourth = log_line("2026-09-28 13:00:00", "/v1/images/generations")
            path.write_text("\n".join([first, second, fourth]) + "\n", encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(before["totals"]["requests"], 3)
        # 保留的两条不重复计数，被删那条已经计入的不回退，新写入的一条要计入。
        self.assertEqual(after["totals"]["requests"], 4)

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

        # 游标对齐到了新文件末尾，既没有重复计数，也没有把已计入的请求抹掉。
        self.assertEqual(before["totals"]["requests"], 1)
        self.assertEqual(after["totals"]["requests"], 1)

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


class LogCleanupCompatibilityTests(unittest.TestCase):
    """清理日志不能顺带清掉统计——这是清理功能成立的前提。"""

    def make_service(self, tmp_dir: str, lines: list[str]) -> tuple[ImageStatsService, Path]:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ImageStatsService(path), path

    def test_prefix_trim_keeps_earlier_statistics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-29 10:00:00", "/v1/images/generations"),
                log_line("2026-09-30 10:00:00", "/v1/images/generations"),
            ])
            before = service.summary("2026-09-28", "2026-09-28")

            # 按保留期清理：删掉最老的一天，其余按原样保留。
            path.write_text("\n".join([
                log_line("2026-09-29 10:00:00", "/v1/images/generations"),
                log_line("2026-09-30 10:00:00", "/v1/images/generations"),
            ]) + "\n", encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")
            untouched = service.summary("2026-09-29", "2026-09-30")

        self.assertEqual(before["totals"]["requests"], 1)
        self.assertEqual(after["totals"]["requests"], 1)
        self.assertEqual(untouched["totals"]["requests"], 2)

    def test_removing_a_middle_line_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 11:00:00", "/v1/images/generations"),
                log_line("2026-09-28 12:00:00", "/v1/images/generations"),
            ])
            before = service.summary("2026-09-28", "2026-09-28")

            # 逐条删除中间那一条：后面的字节整体前移，游标必须跟着对齐。
            path.write_text("\n".join([
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-28 12:00:00", "/v1/images/generations"),
            ]) + "\n", encoding="utf-8")
            after = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(before["totals"]["requests"], 3)
        self.assertEqual(after["totals"]["requests"], 3)

    def test_appends_after_a_trim_are_still_counted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-30 10:00:00", "/v1/images/generations"),
            ])
            service.summary("2026-09-30", "2026-09-30")

            path.write_text(log_line("2026-09-30 10:00:00", "/v1/images/generations") + "\n", encoding="utf-8")
            service.summary("2026-09-30", "2026-09-30")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line("2026-09-30 14:00:00", "/v1/images/generations") + "\n")
            after = service.summary("2026-09-30", "2026-09-30")

        self.assertEqual(after["totals"]["requests"], 2)
        self.assertEqual(after["series"][14]["requests"], 1)


class SnapshotTodayRepairTests(unittest.TestCase):
    """v5 及更早的快照：游标可能因日志改写跳到末尾，升级时按现存日志重算今天。"""

    def test_v5_snapshot_recovers_today_requests_lost_by_cursor_jump(self):
        day = "2026-09-28"
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            lines = [
                log_line(f"{day} {hour:02d}:30:00", "/v1/images/generations",
                         model="gpt-image-2", urls=[f"https://img/{hour}"])
                for hour in (9, 10, 11, 12, 13)
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            ImageStatsService(path).summary(day, day)

            # 模拟线上坏掉的 v5 快照：游标停在文件末尾，聚合里只剩最后一小时。
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["version"] = 5
            snapshot["hours"] = {
                key: value for key, value in snapshot["hours"].items() if key == f"{day}T13"
            }
            snapshot.pop("recent_ids", None)
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            with mock.patch.object(stats_service, "beijing_today", return_value=day):
                restarted = ImageStatsService(path)
                repaired = restarted.summary(day, day)
                again = restarted.summary(day, day)
            snapshot_version = json.loads(
                snapshot_path.read_text(encoding="utf-8")
            )["version"]

        # 白天早些时候被跳过的请求要按现存日志补回来。
        self.assertEqual(repaired["totals"]["requests"], 5)
        self.assertEqual(repaired["totals"]["images"], 5)
        self.assertEqual(repaired["series"][9]["requests"], 1)
        self.assertEqual(again["totals"]["requests"], 5)
        self.assertEqual(snapshot_version, stats_service.SNAPSHOT_VERSION)

    def test_repair_keeps_history_for_days_outside_the_log(self):
        """重算只替换今天的桶：更早的历史仍然来自快照，不会被重建抹掉。"""
        day = "2026-09-28"
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(
                log_line(f"{day} 09:30:00", "/v1/images/generations") + "\n", encoding="utf-8"
            )
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            ImageStatsService(path).summary(day, day)

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["version"] = 5
            snapshot["hours"]["2026-09-01T08"] = {
                "requests": 7, "success": 7, "failed": 0, "policy_violation": 0,
                "unsupported_app_response": 0, "duration_ms": 700, "images": 7,
                "models": {"gpt-image-2": 7}, "revenue": 0.07,
                "models_revenue": {"gpt-image-2": 0.07}, "legacy_success": 0,
                "modes": {},
            }
            snapshot.pop("recent_ids", None)
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            with mock.patch.object(stats_service, "beijing_today", return_value=day):
                result = ImageStatsService(path).summary("2026-09-01", day)

        self.assertEqual(result["totals"]["requests"], 8)
        by_day = {point["key"]: point for point in result["series"]}
        self.assertEqual(by_day["2026-09-01"]["requests"], 7)
        self.assertEqual(by_day[day]["requests"], 1)


class SnapshotPersistenceTests(unittest.TestCase):
    """聚合快照：重启后统计不能丢，否则日志一清理历史就没了。"""

    def make_service(self, tmp_dir: str, lines: list[str]) -> tuple[ImageStatsService, Path]:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ImageStatsService(path), path

    def test_statistics_survive_log_cleanup_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-30 10:00:00", "/v1/images/generations", status="failed"),
            ])
            service.summary("2026-09-28", "2026-09-28")

            path.write_text(log_line("2026-09-30 10:00:00", "/v1/images/generations", status="failed") + "\n", encoding="utf-8")
            after_cleanup = service.summary("2026-09-28", "2026-09-30")

            restarted = ImageStatsService(path)
            after_restart = restarted.summary("2026-09-28", "2026-09-30")

        self.assertEqual(after_cleanup["totals"], {
            "requests": 2, "success": 1, "failed": 1, "policy_violation": 0,
            "unsupported_app_response": 0,
            "success_rate": 0.5, "avg_duration_ms": 100, "images": 0, "revenue": 0,
            "legacy_revenue": 0, "legacy_success": 0,
        })
        self.assertEqual(after_restart["series"][0]["requests"], 1)
        self.assertEqual(after_restart["totals"]["requests"], 2)

    def test_restart_after_cleanup_resumes_from_the_snapshot_cursor(self):
        """重启后游标从快照接着走：清理掉的历史留着，新增的日志也只数一遍。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
                log_line("2026-09-30 10:00:00", "/v1/images/generations"),
            ])
            service.summary("2026-09-30", "2026-09-30")

            path.write_text(log_line("2026-09-30 10:00:00", "/v1/images/generations") + "\n", encoding="utf-8")
            restarted = ImageStatsService(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line("2026-09-30 14:00:00", "/v1/images/generations") + "\n")
            after = restarted.summary("2026-09-28", "2026-09-30")

        self.assertEqual(after["series"][0]["requests"], 1)
        self.assertEqual(after["series"][2]["requests"], 2)
        self.assertEqual(after["totals"]["requests"], 3)

    def test_corrupt_snapshot_is_ignored_and_the_log_is_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations"),
            ])
            (Path(tmp_dir) / "image_stats.json").write_text("{ not json", encoding="utf-8")

            result = ImageStatsService(path).summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["requests"], 1)


class ModelRevenueTests(unittest.TestCase):
    """收入口径：只有成功请求返回的图片才计费，按 detail.urls 的数量算。"""

    def make_service(self, tmp_dir: str, lines: list[str]) -> ImageStatsService:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ImageStatsService(path)

    def test_successful_images_are_counted_by_urls_and_priced(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1", "https://img/2"]),
                log_line("2026-09-28 10:05:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/3"]),
                log_line("2026-09-28 10:10:00", "/v1/images/generations",
                         model="gpt-image-2.5", urls=["https://img/4"]),
                # 失败请求即使带了 urls 也不能计收入。
                log_line("2026-09-28 10:15:00", "/v1/images/generations", status="failed",
                         model="gpt-image-2", urls=["https://img/5"]),
                # 成功但没有返回图片（理论上不该发生）：不计数。
                log_line("2026-09-28 10:20:00", "/v1/images/generations", model="gpt-image-2"),
            ])

            result = service.summary(
                "2026-09-28", "2026-09-28",
                pricing={"gpt-image-2": 0.5, "gpt-image-2.5": 1.2},
            )

        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["totals"]["images"], 4)
        self.assertEqual(result["totals"]["revenue"], 2.7)
        self.assertEqual([row["model"] for row in result["by_model"]], ["gpt-image-2", "gpt-image-2.5"])
        self.assertEqual(result["by_model"][0], {
            "model": "gpt-image-2", "images": 3, "price": 0.5, "revenue": 1.5,
        })
        self.assertEqual(result["by_model"][1], {
            "model": "gpt-image-2.5", "images": 1, "price": 1.2, "revenue": 1.2,
        })

    def test_unpriced_models_report_null_price_and_zero_revenue(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1", "https://img/2"]),
            ])

            result = service.summary("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["images"], 2)
        self.assertEqual(result["totals"]["revenue"], 0)
        self.assertEqual(result["by_model"], [
            {"model": "gpt-image-2", "images": 2, "price": None, "revenue": 0.0},
        ])

    def test_zero_price_means_free_not_unpriced(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="auto", urls=["https://img/1"]),
            ])

            result = service.summary("2026-09-28", "2026-09-28", pricing={"auto": 0})

        self.assertEqual(result["totals"]["revenue"], 0)
        self.assertEqual(result["by_model"][0]["price"], 0)
        self.assertEqual(result["by_model"][0]["revenue"], 0.0)

    def test_lifetime_totals_ignore_the_selected_range(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1", "https://img/2"]),
                log_line("2026-09-29 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/3"]),
            ])

            today_only = service.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 1})
            everything = service.summary(scope="all", pricing={"gpt-image-2": 1})

        # 区间内只算当天的 2 张，累计口径始终是全部 3 张。
        self.assertEqual(today_only["totals"]["images"], 2)
        self.assertEqual(today_only["totals"]["revenue"], 2)
        self.assertEqual(today_only["lifetime"], {"images": 3, "revenue": 3})
        self.assertEqual(everything["totals"]["revenue"], everything["lifetime"]["revenue"])
        self.assertEqual(everything["lifetime"], {"images": 3, "revenue": 3})

    def test_price_changes_only_affect_future_requests(self):
        """收入在消费日志时计入：调价只影响之后新增的请求，旧收入保持冻结。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(log_line(
                "2026-09-28 10:00:00", "/v1/images/generations",
                model="gpt-image-2", urls=["https://img/1", "https://img/2"],
            ) + "\n", encoding="utf-8")
            service = ImageStatsService(path)

            before = service.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 1})
            # 调价：已计入的 2 张不能按新价格重算。
            repriced = service.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 2})
            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line(
                    "2026-09-28 12:00:00", "/v1/images/generations",
                    model="gpt-image-2", urls=["https://img/3"],
                ) + "\n")
            # 新增的 1 张按新价格计费。
            after = service.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 2})

        self.assertEqual(before["totals"]["revenue"], 2)
        self.assertEqual(repriced["totals"]["revenue"], 2)
        self.assertEqual(repriced["lifetime"], {"images": 2, "revenue": 2})
        self.assertEqual(after["totals"]["revenue"], 4)
        self.assertEqual(after["totals"]["images"], 3)
        self.assertEqual(after["lifetime"], {"images": 3, "revenue": 4})
        # 明细里的单价是当前配置，收入是当时计入的金额，两者可以不一致。
        self.assertEqual(after["by_model"], [
            {"model": "gpt-image-2", "images": 3, "price": 2, "revenue": 4.0},
        ])

    def test_restart_keeps_frozen_revenue(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1"]),
            ])
            service.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 1})

            restarted = ImageStatsService(Path(tmp_dir) / "logs.jsonl")
            after = restarted.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 9})

        self.assertEqual(after["totals"]["revenue"], 1)
        self.assertEqual(after["lifetime"], {"images": 1, "revenue": 1})

    def test_invalid_prices_are_treated_as_unpriced(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1"]),
            ])

            result = service.summary(
                "2026-09-28", "2026-09-28",
                pricing={"gpt-image-2": -1},  # type: ignore[dict-item]
            )

        self.assertIsNone(result["by_model"][0]["price"])

    def test_image_urls_are_counted_by_length(self):
        """多图请求按返回的 urls 数量计费；日志侧已经去过重，这里只信任长度。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1", "https://img/1"]),
            ])

            result = service.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 1})

        self.assertEqual(result["totals"]["images"], 2)
        self.assertEqual(result["totals"]["revenue"], 2.0)


def strip_mode_images(snapshot: dict) -> None:
    for bucket in snapshot["hours"].values():
        for mode_bucket in (bucket.get("modes") or {}).values():
            mode_bucket.pop("images", None)


def downgrade_snapshot_to_v1(snapshot_path: Path) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["version"] = 1
    for bucket in snapshot["hours"].values():
        bucket.pop("images", None)
        bucket.pop("models", None)
        bucket.pop("revenue", None)
        bucket.pop("models_revenue", None)
        bucket.pop("legacy_success", None)
    strip_mode_images(snapshot)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def downgrade_snapshot_to_v2(snapshot_path: Path) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["version"] = 2
    for bucket in snapshot["hours"].values():
        bucket.pop("revenue", None)
        bucket.pop("models_revenue", None)
        bucket.pop("legacy_success", None)
    strip_mode_images(snapshot)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def downgrade_snapshot_to_v3(snapshot_path: Path) -> None:
    """模拟旧版快照：调用方式桶还没有 images，收入已经是按图片计的冻结值。"""
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["version"] = 3
    strip_mode_images(snapshot)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


class SnapshotModelMigrationTests(unittest.TestCase):
    """v1 快照直升 v2：请求历史保留，模型/图片数据从现存日志回填且只回填一次。"""

    def make_service(self, tmp_dir: str, lines: list[str]) -> tuple[ImageStatsService, Path, Path]:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ImageStatsService(path), path, Path(tmp_dir) / "image_stats.json"

    def test_v1_snapshot_backfills_images_without_recounting_requests(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path, snapshot_path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1", "https://img/2"]),
                log_line("2026-09-28 11:00:00", "/v1/images/generations", status="failed",
                         model="gpt-image-2"),
                log_line("2026-09-29 10:00:00", "/v1/images/edits",
                         model="gpt-image-2.5", urls=["https://img/3"]),
            ])
            service.summary("2026-09-28", "2026-09-29")
            self.assertEqual(
                json.loads(snapshot_path.read_text(encoding="utf-8"))["version"],
                stats_service.SNAPSHOT_VERSION,
            )

            downgrade_snapshot_to_v1(snapshot_path)
            restarted = ImageStatsService(path)
            first = restarted.summary("2026-09-28", "2026-09-29")

            self.assertEqual(first["totals"]["requests"], 3)
            self.assertEqual(first["totals"]["success"], 2)
            self.assertEqual(first["totals"]["failed"], 1)
            self.assertEqual(first["totals"]["images"], 3)
            self.assertEqual(first["totals"]["revenue"], 0)
            self.assertEqual(
                [(row["model"], row["images"]) for row in first["by_model"]],
                [("gpt-image-2", 2), ("gpt-image-2.5", 1)],
            )

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["version"], stats_service.SNAPSHOT_VERSION)
            self.assertEqual(sum(bucket["images"] for bucket in snapshot["hours"].values()), 3)

            # 迁移只做一次：再查一遍不能把图片数翻倍。
            second = restarted.summary("2026-09-28", "2026-09-29")
            self.assertEqual(second["totals"]["images"], 3)
            self.assertEqual(second["totals"]["requests"], 3)

    def test_v1_snapshot_keeps_appending_new_images_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path, snapshot_path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1"]),
            ])
            service.summary("2026-09-28", "2026-09-28")
            downgrade_snapshot_to_v1(snapshot_path)

            restarted = ImageStatsService(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_line("2026-09-28 12:00:00", "/v1/images/generations",
                                      model="gpt-image-2", urls=["https://img/2"]) + "\n")
            result = restarted.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 1})

        self.assertEqual(result["totals"]["images"], 2)
        self.assertEqual(result["totals"]["revenue"], 2.0)

    def test_v1_snapshot_with_lost_anchor_estimates_images_from_successes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path, snapshot_path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1"]),
            ])
            service.summary("2026-09-28", "2026-09-28")
            downgrade_snapshot_to_v1(snapshot_path)

            # 日志被整份改写：锚点找不到，不能确认旧聚合对应哪些行，只能放弃回填。
            path.write_text("".join(
                log_line(f"2026-09-28 13:{minute:02d}:00", "/v1/messages")
                for minute in range(5)
            ), encoding="utf-8")
            restarted = ImageStatsService(path)
            result = restarted.summary("2026-09-28", "2026-09-28")

            self.assertEqual(result["totals"]["requests"], 1)
            # 日志回填失败，但成功计数还在：按 1 次成功 = 1 张图估算，不能显示成 0。
            self.assertEqual(result["totals"]["images"], 1)
            self.assertEqual(result["by_model"], [])
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["version"], stats_service.SNAPSHOT_VERSION)

    def test_v1_snapshot_without_log_file_is_marked_migrated(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, path, snapshot_path = self.make_service(tmp_dir, [
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1"]),
            ])
            service.summary("2026-09-28", "2026-09-28")
            downgrade_snapshot_to_v1(snapshot_path)

            path.unlink()
            restarted = ImageStatsService(path)
            result = restarted.summary("2026-09-28", "2026-09-28")

            self.assertEqual(result["totals"]["requests"], 1)
            self.assertEqual(result["totals"]["images"], 1)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["version"], stats_service.SNAPSHOT_VERSION)


class SnapshotRevenueBootstrapTests(unittest.TestCase):
    """v2 快照没有收入：升级时按迁移这一刻的定价补一次，之后冻结。"""

    def test_v2_snapshot_bills_existing_successes_at_migration_prices(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(log_line(
                "2026-09-28 10:00:00", "/v1/images/generations",
                model="gpt-image-2", urls=["https://img/1", "https://img/2"],
            ) + "\n", encoding="utf-8")
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            ImageStatsService(path).summary("2026-09-28", "2026-09-28")
            downgrade_snapshot_to_v2(snapshot_path)

            restarted = ImageStatsService(path)
            first = restarted.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 0.75})
            # 迁移后调价：已经补上的收入不能跟着变。
            second = restarted.summary("2026-09-28", "2026-09-28", pricing={"gpt-image-2": 5})
            snapshot_version = json.loads(snapshot_path.read_text(encoding="utf-8"))["version"]

        self.assertEqual(first["totals"]["images"], 2)
        self.assertEqual(first["totals"]["success"], 1)
        self.assertEqual(first["totals"]["revenue"], 1.5)
        self.assertEqual(first["totals"]["legacy_revenue"], 0)
        self.assertEqual(first["by_model"], [
            {"model": "gpt-image-2", "images": 2, "price": 0.75, "revenue": 1.5},
        ])
        self.assertEqual(second["totals"]["revenue"], 1.5)
        self.assertEqual(second["lifetime"], {"images": 2, "revenue": 1.5})
        self.assertEqual(snapshot_version, stats_service.SNAPSHOT_VERSION)

    def test_v2_snapshot_estimates_legacy_success_with_fallback_price(self):
        """连 images 都没有的旧记录：按成功请求数 × 回退单价估算。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text("", encoding="utf-8")
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            snapshot_path.write_text(json.dumps({
                "version": 2,
                "hours": {
                    "2026-09-28T10": {
                        "requests": 3, "success": 3, "failed": 0, "policy_violation": 0,
                        "duration_ms": 300, "images": 0, "models": {},
                        "modes": {"generate": {
                            "requests": 3, "success": 3, "failed": 0, "policy_violation": 0,
                        }},
                    },
                },
                "offset": 0,
                "anchor": "",
            }), encoding="utf-8")

            service = ImageStatsService(path)
            result = service.summary("2026-09-28", "2026-09-28", pricing={"auto": 0.5})
            restarted = ImageStatsService(path)
            after_restart = restarted.summary(
                "2026-09-28", "2026-09-28", pricing={"auto": 9, "gpt-image-2": 9},
            )

        self.assertEqual(result["totals"]["revenue"], 1.5)
        self.assertEqual(result["totals"]["legacy_revenue"], 1.5)
        self.assertEqual(result["totals"]["legacy_success"], 3)
        self.assertEqual(result["by_model"], [])
        # 没有图片明细的旧记录用成功次数估算图片数，展示不再全是 0。
        self.assertEqual(result["totals"]["images"], 3)
        self.assertEqual(result["lifetime"], {"images": 3, "revenue": 1.5})
        self.assertEqual(after_restart["totals"]["revenue"], 1.5)

    def test_v1_snapshot_bills_backfilled_images_at_migration_prices(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(log_line(
                "2026-09-28 10:00:00", "/v1/images/generations",
                model="gpt-image-2", urls=["https://img/1"],
            ) + "\n", encoding="utf-8")
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            ImageStatsService(path).summary("2026-09-28", "2026-09-28")
            downgrade_snapshot_to_v1(snapshot_path)

            result = ImageStatsService(path).summary(
                "2026-09-28", "2026-09-28", pricing={"gpt-image-2": 2},
            )

        self.assertEqual(result["totals"]["images"], 1)
        self.assertEqual(result["totals"]["revenue"], 2)
        self.assertEqual(result["by_model"][0]["revenue"], 2)

    def test_v4_snapshot_estimates_missing_images_without_touching_revenue(self):
        """线上旧数据可能只有成功计数、没有图片数：升级后成功历史要能显示出来。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text("", encoding="utf-8")
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            snapshot_path.write_text(json.dumps({
                "version": 4,
                "hours": {
                    "2026-09-20T10": {
                        "requests": 5, "success": 3, "failed": 2, "policy_violation": 0,
                        "duration_ms": 500, "images": 0, "models": {},
                        "modes": {
                            "generate": {
                                "requests": 4, "success": 2, "failed": 2,
                                "policy_violation": 0, "images": 0,
                            },
                            "edit": {
                                "requests": 1, "success": 1, "failed": 0,
                                "policy_violation": 0, "images": 0,
                            },
                        },
                        "revenue": 0.6, "models_revenue": {}, "legacy_success": 3,
                    },
                },
                "offset": 0,
                "anchor": "",
            }), encoding="utf-8")

            # 调价也不该动已冻结的收入，只补图片数。
            result = ImageStatsService(path).summary(
                "2026-09-20", "2026-09-20", pricing={"auto": 9},
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            modes = {row["mode"]: row for row in result["by_mode"]}

        self.assertEqual(result["totals"]["images"], 3)
        self.assertEqual(result["totals"]["revenue"], 0.6)
        self.assertEqual(result["lifetime"], {"images": 3, "revenue": 0.6})
        self.assertEqual(modes["generate"]["images"], 2)
        self.assertEqual(modes["edit"]["images"], 1)
        self.assertEqual(sum(row["images"] for row in modes.values()), 3)
        self.assertEqual(result["by_model"], [])
        self.assertEqual(snapshot["version"], stats_service.SNAPSHOT_VERSION)

    def test_v3_snapshot_backfills_mode_images_without_rebilling(self):
        """v3 快照只有分桶图片数：升级时按成功请求占比分摊到各调用方式，收入保持冻结。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text("\n".join([
                log_line("2026-09-28 10:00:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/1", "https://img/2"]),
                log_line("2026-09-28 10:05:00", "/v1/images/generations",
                         model="gpt-image-2", urls=["https://img/3"]),
                log_line("2026-09-28 10:10:00", "/v1/images/edits",
                         model="gpt-image-2", urls=["https://img/4"]),
            ]) + "\n", encoding="utf-8")
            snapshot_path = Path(tmp_dir) / "image_stats.json"
            ImageStatsService(path).summary(
                "2026-09-28", "2026-09-28", pricing={"gpt-image-2": 1},
            )
            downgrade_snapshot_to_v3(snapshot_path)

            # 调价后重启：收入是冻结的，不跟着重算；只补调用方式的图片数。
            result = ImageStatsService(path).summary(
                "2026-09-28", "2026-09-28", pricing={"gpt-image-2": 5},
            )
            snapshot_version = json.loads(snapshot_path.read_text(encoding="utf-8"))["version"]
            modes = {
                row["mode"]: row for row in result["by_mode"]
            }

        self.assertEqual(result["totals"]["images"], 4)
        self.assertEqual(result["totals"]["revenue"], 4.0)
        self.assertEqual(modes["generate"]["images"], 3)
        self.assertEqual(modes["edit"]["images"], 1)
        self.assertEqual(sum(row["images"] for row in modes.values()), 4)
        self.assertEqual(snapshot_version, stats_service.SNAPSHOT_VERSION)


class ImageLogCostTests(unittest.TestCase):
    def _item(self, endpoint: str, status: str = "success", model: str = "gpt-image-2",
              urls: list[str] | None = None, log_type: str = "call") -> dict:
        return json.loads(log_line(
            "2026-09-28 10:00:00", endpoint, status=status, log_type=log_type,
            model=model, urls=["https://img/1", "https://img/2"] if urls is None else urls,
        ))

    def test_successful_generation_charges_images_times_price(self):
        item = self._item("/v1/images/generations")
        self.assertEqual(stats_service.image_log_cost(item, {"gpt-image-2": 1.5}), 3.0)

    def test_failed_and_policy_violation_are_free(self):
        pricing = {"gpt-image-2": 1.5}
        for status in ("failed", "policy_violation", "unsupported_app_response"):
            self.assertEqual(stats_service.image_log_cost(self._item("/v1/images/edits", status=status), pricing), 0.0)

    def test_non_image_call_and_account_log_are_none(self):
        self.assertIsNone(stats_service.image_log_cost(self._item("/v1/chat/completions"), {"auto": 1}))
        self.assertIsNone(stats_service.image_log_cost(self._item("/v1/images/generations", log_type="account"), {"auto": 1}))

    def test_successful_task_endpoint_is_none_but_failure_is_free(self):
        # 任务接口成功由 /v1/images/* 另行记费，这里不能再算一次。
        self.assertIsNone(stats_service.image_log_cost(self._item("/api/image-tasks/generations"), {"auto": 1}))
        self.assertEqual(
            stats_service.image_log_cost(self._item("/api/image-tasks/generations", status="failed"), {"auto": 1}),
            0.0,
        )

    def test_unpriced_model_returns_none(self):
        self.assertIsNone(stats_service.image_log_cost(self._item("/v1/images/generations"), {}))
        self.assertIsNone(stats_service.image_log_cost(self._item("/v1/images/generations", model=""), {}))

    def test_no_urls_costs_zero(self):
        item = self._item("/v1/images/generations", urls=[])
        self.assertEqual(stats_service.image_log_cost(item, {"gpt-image-2": 1.5}), 0.0)


class BuildImageStatsTests(unittest.TestCase):
    def test_build_image_stats_reads_pricing_from_config(self):
        import types
        from unittest import mock

        import services.config as config_module

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(log_line(
                "2026-09-28 10:00:00", "/v1/images/generations",
                model="gpt-image-2", urls=["https://img/1", "https://img/2"],
            ) + "\n", encoding="utf-8")
            service = ImageStatsService(path)
            with mock.patch.object(stats_service, "image_stats_service", service), \
                    mock.patch.object(config_module, "config", types.SimpleNamespace(model_pricing={"gpt-image-2": 1.5})):
                result = stats_service.build_image_stats("2026-09-28", "2026-09-28")

        self.assertEqual(result["totals"]["images"], 2)
        self.assertEqual(result["totals"]["success"], 1)
        self.assertEqual(result["totals"]["revenue"], 3.0)
        self.assertEqual(result["by_model"][0]["price"], 1.5)


if __name__ == "__main__":
    unittest.main()
