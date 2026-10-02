from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.system as system_module
import services.log_service as log_service_module
from services.log_service import LoggedCall, LogService
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


class LogExportTests(unittest.TestCase):
    """一键导出：完整的报文、跟着筛选走、默认 10 条。"""

    def make_service(self, lines: list[str]) -> LogService:
        path = Path(self._tmp.name) / "logs.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return LogService(path)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_default_export_keeps_the_latest_ten_in_chronological_order(self):
        lines = [log_line(day=f"2026-09-{day:02d}") for day in range(1, 15)]
        service = self.make_service(lines)

        text, count = service.build_export()

        self.assertEqual(count, 10)
        # 由旧到新：14 条里最早的四天被切掉，剩下 05 → 14
        self.assertNotIn("2026-09-04", text)
        self.assertLess(text.index("2026-09-05"), text.index("2026-09-14"))

    def test_full_payload_survives_the_export(self):
        """上游原始响应体、请求摘要这些排查时真正要看的东西一个字都不能少。"""
        body = {"error": {"code": "skipped_mainline", "detail": "上游原文 " + "x" * 200}}
        line = json.dumps(
            {
                "id": "log-1",
                "time": "2026-09-29 10:00:00",
                "type": "call",
                "summary": "图生图调用失败",
                "detail": {
                    "status": "failed",
                    "duration_ms": 21440,
                    "endpoint": "/v1/images/edits",
                    "image_trace": "a1b2c3d4e5f6-0",
                    "account_email": "someone@example.com",
                    "request_text": "把这张图改成夜景",
                    "upstream_error": [{"type": "UpstreamHTTPError", "status_code": 400, "body": body}],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        service = self.make_service([line])

        text, count = service.build_export()

        self.assertEqual(count, 1)
        self.assertIn("skipped_mainline", text)
        self.assertIn("x" * 200, text)
        self.assertIn("把这张图改成夜景", text)
        self.assertIn("a1b2c3d4e5f6-0", text)
        self.assertIn("someone@example.com", text)
        self.assertIn("耗时=21440ms", text)

    def test_export_follows_the_filters(self):
        service = self.make_service([
            log_line(day="2026-09-28", status="success"),
            log_line(day="2026-09-29", status="failed"),
            log_line(day="2026-09-29", status="success"),
        ])

        text, count = service.build_export(status="failed")

        self.assertEqual(count, 1)
        self.assertIn("2026-09-29", text)

    def test_export_limit_is_clamped(self):
        service = self.make_service([log_line(day="2026-09-29") for _ in range(3)])

        self.assertEqual(service.build_export(limit=0)[1], 1)
        self.assertEqual(service.build_export(limit="nonsense")[1], 3)
        self.assertEqual(service.build_export(limit=10 ** 6)[1], 3)

    def test_empty_export_still_produces_a_readable_file(self):
        service = self.make_service([])

        text, count = service.build_export()

        self.assertEqual(count, 0)
        self.assertIn("没有符合条件的日志", text)

    def test_missing_file_exports_without_crashing(self):
        service = LogService(Path(self._tmp.name) / "missing.jsonl")

        text, count = service.build_export()

        self.assertEqual(count, 0)
        self.assertIn("ChatGPT2API 日志导出", text)


class LoggedCallDetailTests(unittest.TestCase):
    """写进日志详情的东西，就是导出 txt 里能看到的东西。"""

    def test_failure_detail_carries_trace_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "logs.jsonl")
            error = RuntimeError("boom")
            error.image_trace = "trace-1"
            error.image_diagnostics = {"image_peers": 2, "handshake_wait_ms": 15}
            call = LoggedCall({"id": "k1", "name": "管理员", "role": "admin"}, "/v1/images/edits", "gpt-image-2", "图生图")
            with mock.patch.object(log_service_module, "log_service", service):
                call.log("调用失败", status="failed", error="boom", exc=error)

            detail = service.list()[0]["detail"]
            self.assertEqual(detail["image_trace"], "trace-1")
            self.assertEqual(detail["image_diagnostics"], {"image_peers": 2, "handshake_wait_ms": 15})
            self.assertEqual(detail["status"], "failed")

            text, count = service.build_export()
            self.assertEqual(count, 1)
            self.assertIn("image_diagnostics", text)
            self.assertIn("handshake_wait_ms", text)

    def test_failure_without_diagnostics_keeps_the_old_shape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "logs.jsonl")
            call = LoggedCall({"id": "k1", "name": "管理员", "role": "admin"}, "/v1/chat/completions", "gpt-4", "对话")
            with mock.patch.object(log_service_module, "log_service", service):
                call.log("调用失败", status="failed", error="boom", exc=ValueError("boom"))

            detail = service.list()[0]["detail"]
            self.assertNotIn("image_diagnostics", detail)
            self.assertNotIn("image_trace", detail)


class LogExportApiTests(unittest.TestCase):
    """导出接口：带鉴权的 txt 下载，默认 10 条。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "logs.jsonl"
        path.write_text(
            "\n".join(log_line(day=f"2026-09-{day:02d}", summary="图生图调用失败") for day in range(1, 13)) + "\n",
            encoding="utf-8",
        )
        self.service = LogService(path)
        patcher = mock.patch.object(system_module, "log_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(system_module.create_router("2.0.1"))
        self.client = TestClient(app)

    def test_export_requires_an_admin_key(self):
        self.assertEqual(self.client.get("/api/logs/export").status_code, 401)

    def test_export_returns_a_txt_attachment_with_the_default_limit(self):
        with mock.patch.object(system_module, "require_admin", lambda authorization=None: {"role": "admin"}):
            response = self.client.get("/api/logs/export")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn(".txt", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-exported-count"], "10")
        self.assertIn("图生图调用失败", response.text)

    def test_export_honours_the_requested_limit(self):
        with mock.patch.object(system_module, "require_admin", lambda authorization=None: {"role": "admin"}):
            response = self.client.get("/api/logs/export", params={"limit": 3})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-exported-count"], "3")


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
