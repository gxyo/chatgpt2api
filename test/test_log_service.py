from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.log_service import LogService
from utils.helper import CHANNEL_BUSY_MESSAGE, UpstreamHTTPError, describe_exception


def log_line(*, day: str, log_type: str = "call", status: str | None = "success", summary: str = "调用") -> str:
    detail: dict[str, object] = {}
    if status is not None:
        detail["status"] = status
    return json.dumps(
        {"id": f"{log_type}-{day}-{status}", "time": f"{day} 10:00:00", "type": log_type, "summary": summary, "detail": detail},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class LogListFilterTests(unittest.TestCase):
    """日志页的筛选：类型、成功/失败、日期范围，以及它们的组合。"""

    def make_service(self, tmp_dir: str, lines: list[str]) -> LogService:
        path = Path(tmp_dir) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return LogService(path)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = self.make_service(self._tmp.name, [
            log_line(day="2026-09-28", status="success"),
            log_line(day="2026-09-29", status="failed"),
            log_line(day="2026-09-29", status="success"),
            log_line(day="2026-09-30", log_type="account", status=None),
        ])

    @staticmethod
    def days(items: list[dict[str, object]]) -> list[str]:
        return [str(item["time"])[:10] for item in items]

    def test_latest_first_without_filters(self):
        self.assertEqual(self.days(self.service.list()), ["2026-09-30", "2026-09-29", "2026-09-29", "2026-09-28"])

    def test_status_filter_keeps_only_matching_records(self):
        failed = self.service.list(status="failed")
        success = self.service.list(status="success")

        self.assertEqual(self.days(failed), ["2026-09-29"])
        self.assertEqual(len(success), 2)
        self.assertTrue(all(item["detail"]["status"] == "success" for item in success))

    def test_status_filter_skips_records_without_a_status(self):
        """账号日志的 detail 里可能没有 status，不能被算进任何一侧。"""
        self.assertNotIn("2026-09-30", self.days(self.service.list(status="success")))
        self.assertNotIn("2026-09-30", self.days(self.service.list(status="failed")))

    def test_filters_combine(self):
        items = self.service.list(type="call", status="failed", start_date="2026-09-29", end_date="2026-09-29")

        self.assertEqual(self.days(items), ["2026-09-29"])

    def test_a_date_window_that_excludes_everything_returns_nothing(self):
        self.assertEqual(self.service.list(start_date="2026-10-01"), [])

    def test_missing_file_lists_nothing(self):
        missing = LogService(Path(self._tmp.name) / "missing.jsonl")

        self.assertEqual(missing.list(status="failed"), [])


class DescribeExceptionTests(unittest.TestCase):
    """失败日志要留下上游原始报错，而不是只有一句转译后的文案。"""

    def test_upstream_status_and_body_survive_wrapping(self):
        upstream = UpstreamHTTPError("POST /backend-api/conversation", 429, {"detail": "rate limited"})

        try:
            try:
                raise upstream
            except UpstreamHTTPError as exc:
                raise RuntimeError(CHANNEL_BUSY_MESSAGE) from exc
        except RuntimeError as exc:
            frames = describe_exception(exc)

        self.assertEqual(frames[0]["type"], "RuntimeError")
        self.assertEqual(frames[0]["message"], CHANNEL_BUSY_MESSAGE)
        self.assertEqual(frames[1]["type"], "UpstreamHTTPError")
        self.assertEqual(frames[1]["status_code"], 429)
        self.assertEqual(frames[1]["context"], "POST /backend-api/conversation")
        self.assertIn("rate limited", str(frames[1]["body"]))

    def test_a_plain_exception_yields_a_single_frame(self):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            frames = describe_exception(exc)

        self.assertEqual(frames, [{"type": "ValueError", "message": "boom"}])

    def test_long_messages_are_truncated(self):
        try:
            raise ValueError("x" * 5000)
        except ValueError as exc:
            frames = describe_exception(exc, limit=100)

        self.assertLess(len(frames[0]["message"]), 200)
        self.assertIn("[truncated]", frames[0]["message"])

    def test_a_cyclic_chain_terminates(self):
        try:
            raise ValueError("loop")
        except ValueError as exc:
            exc.__cause__ = exc
            frames = describe_exception(exc)

        self.assertEqual(len(frames), 1)


if __name__ == "__main__":
    unittest.main()
