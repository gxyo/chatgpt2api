"""注册机失败日志：只记失败，但要记全现场。

写进 logs.jsonl 的 type=register 记录就是「注册日志」页面和一键导出的全部数据源，
所以这里断言的是记录内容本身（邮箱域名、停在哪一步、堆栈、过程记录都在）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import services.log_service as log_service_module
from services.log_service import LOG_TYPE_REGISTER, LogService
from services.register import openai_register, task_process


class TaskContextTestCase(unittest.TestCase):
    """过程记录是模块级全局：测试之间必须清干净，顺便把 step() 的控制台输出静音。

    先清再测（而不是只清在收尾）：别的测试模块也可能留下同一个任务号的过程记录，
    不清就会串进这里的断言。
    """

    def setUp(self) -> None:
        self._sink = openai_register.register_log_sink
        openai_register.register_log_sink = None
        self._clear_task_context()
        patcher = mock.patch.object(openai_register, "log", lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._restore_task_context)

    @staticmethod
    def _clear_task_context() -> None:
        with openai_register.print_lock:
            openai_register._task_context.clear()

    def _restore_task_context(self) -> None:
        openai_register.register_log_sink = self._sink
        self._clear_task_context()


class StepTraceTests(TaskContextTestCase):
    """step() 顺手记下的过程记录，是失败日志里最有用的一段。"""

    def test_steps_are_recorded_with_a_timestamp(self) -> None:
        openai_register.step(1, "开始创建邮箱")
        openai_register.step(1, "邮箱创建完成[cf]: a@b.c")

        steps = openai_register.task_trace(1)

        self.assertEqual(len(steps), 2)
        self.assertRegex(steps[0], r"^\d{2}:\d{2}:\d{2} 开始创建邮箱$")
        self.assertIn("a@b.c", steps[1])

    def test_the_trace_is_bounded(self) -> None:
        for i in range(openai_register.MAX_TASK_TRACE_LINES + 20):
            openai_register.step(1, f"第 {i} 步")

        steps = openai_register.task_trace(1)

        self.assertEqual(len(steps), openai_register.MAX_TASK_TRACE_LINES)
        self.assertIn("第 20 步", steps[0], "超过上限时丢的是最老的几步")
        self.assertIn(f"第 {openai_register.MAX_TASK_TRACE_LINES + 19} 步", steps[-1])

    def test_drop_clears_the_slot(self) -> None:
        openai_register.step(1, "一步")
        openai_register.note_task_facts(1, email="a@b.c")

        openai_register.drop_task_context(1)

        self.assertEqual(openai_register.task_trace(1), [])
        self.assertEqual(openai_register.task_facts(1), {})


class FailureDetailTests(TaskContextTestCase):
    def test_detail_carries_facts_stage_chain_and_traceback(self) -> None:
        openai_register.note_task_facts(
            3, email="someone@mail.example.com", mail_domain="mail.example.com",
            mail_provider="cloudflare_temp_email", mail_label="cf-1",
        )
        openai_register.step(3, "任务启动 (引擎: playwright)")
        openai_register.step(3, "提交密码后页面未继续, url=https://auth.openai.com/create-account/password")

        error = RuntimeError("提交密码后页面未继续")
        error.__cause__ = ValueError("上游拒绝")
        detail = openai_register.failure_detail(3, error)

        self.assertEqual(detail["email"], "someone@mail.example.com")
        self.assertEqual(detail["mail_domain"], "mail.example.com")
        self.assertEqual(detail["mail_label"], "cf-1")
        self.assertEqual(detail["stage"], "提交密码后页面未继续, url=https://auth.openai.com/create-account/password")
        self.assertEqual(detail["step_count"], 2)
        self.assertEqual(len(detail["steps"]), 2)
        self.assertEqual(detail["error_type"], "RuntimeError")
        self.assertTrue(detail["traceback"])
        # 异常链摊平：外层文案和上游原始报错都要在（字段名与调用日志一致，日志页直接渲染）。
        self.assertEqual([frame["type"] for frame in detail["upstream_error"]], ["RuntimeError", "ValueError"])

    def test_proxy_credentials_never_reach_the_log(self) -> None:
        self.assertEqual(
            openai_register._mask_proxy("socks5://user:secret@1.2.3.4:1080"),
            "socks5://1.2.3.4:1080",
        )
        self.assertEqual(openai_register._mask_proxy("http://1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(openai_register._mask_proxy(""), "")
        self.assertEqual(openai_register._mask_proxy("not a url"), "(无法解析)")
        self.assertNotIn("secret", openai_register._mask_proxy("socks5://user:secret@1.2.3.4:1080"))

    def test_detail_survives_a_task_that_never_created_a_mailbox(self) -> None:
        openai_register.step(3, "启动浏览器")

        detail = openai_register.failure_detail(3, RuntimeError("浏览器启动失败"))

        self.assertNotIn("email", detail)
        self.assertEqual(detail["stage"], "启动浏览器")


class RecordRegisterFailureTests(TaskContextTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = LogService(Path(self._tmp.name) / "logs.jsonl")
        patcher = mock.patch.object(log_service_module, "log_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def records(self) -> list[dict]:
        return self.service.list(type=LOG_TYPE_REGISTER)

    def test_failure_record_keeps_the_whole_scene(self) -> None:
        detail = {
            "email": "someone@mail.example.com",
            "mail_domain": "mail.example.com",
            "mail_provider": "cloudflare_temp_email",
            "stage": "提交密码后页面未继续",
            "steps": ["12:00:01 任务启动", "12:00:02 提交密码后页面未继续"],
            "traceback": ["Traceback (most recent call last):", "RuntimeError: boom"],
            "upstream_error": [{"type": "RuntimeError", "message": "boom"}],
            "engine": "playwright",
            "threads": 3,
            "proxy": "socks5://1.2.3.4:1080",
        }

        openai_register._record_register_failure(
            4, {"error": "提交密码后页面未继续", "fatal": False}, detail, 41.5,
        )

        items = self.records()
        self.assertEqual(len(items), 1)
        record = items[0]["detail"]
        self.assertEqual(items[0]["type"], LOG_TYPE_REGISTER)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["task_index"], 4)
        self.assertEqual(record["email"], "someone@mail.example.com")
        self.assertEqual(record["stage"], "提交密码后页面未继续")
        self.assertEqual(record["duration_ms"], 41500)
        self.assertFalse(record["fatal"])
        self.assertEqual(record["steps"], detail["steps"])
        self.assertEqual(record["traceback"], detail["traceback"])
        self.assertEqual(record["upstream_error"], [{"type": "RuntimeError", "message": "boom"}])
        self.assertEqual(record["error"], "提交密码后页面未继续")
        self.assertIn("someone@mail.example.com", items[0]["summary"])
        self.assertIn("任务4", items[0]["summary"])

    def test_empty_detail_fields_are_dropped(self) -> None:
        openai_register._record_register_failure(
            4, {"error": "boom", "fatal": True}, {"email": "", "steps": [], "stage": None}, 1.0,
        )

        record = self.records()[0]["detail"]
        self.assertNotIn("email", record)
        self.assertNotIn("steps", record)
        self.assertNotIn("stage", record)
        self.assertTrue(record["fatal"])

    def test_email_is_recovered_from_the_steps_when_the_child_could_not_report_it(self) -> None:
        """超时被强杀时只剩父进程攒的输出尾部：邮箱要从过程记录里回捞。"""
        openai_register._record_register_failure(
            4, {"error": "注册超过 600s 未完成", "fatal": False},
            {
                "stage": "任务超过 600s 未完成，已被强制终止",
                "steps": ["12:00:01 [任务4] 创建邮箱", "12:00:03 [任务4] 邮箱创建完成[cf-1]: ab@mail.example.com"],
            },
            600.0,
        )

        record = self.records()[0]["detail"]
        self.assertEqual(record["email"], "ab@mail.example.com")
        self.assertEqual(record["mail_domain"], "mail.example.com")

    def test_a_broken_log_backend_does_not_break_the_registration_task(self) -> None:
        broken = mock.Mock()
        broken.add.side_effect = OSError("disk full")
        with mock.patch.object(log_service_module, "log_service", broken):
            openai_register._record_register_failure(4, {"error": "boom"}, {}, 1.0)

        broken.add.assert_called_once()


class WorkerRegisterLogTests(TaskContextTestCase):
    """worker() 是主进程里唯一给失败收尾的地方，「只记失败」在这里把关。"""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = LogService(Path(self._tmp.name) / "logs.jsonl")
        patcher = mock.patch.object(log_service_module, "log_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._stats = dict(openai_register.stats)
        self.addCleanup(lambda: openai_register.stats.update(self._stats))

    def test_a_failed_child_writes_exactly_one_register_record(self) -> None:
        outcome = {
            "ok": False,
            "fatal": False,
            "error": "提交密码后页面未继续",
            "detail": {
                "email": "someone@mail.example.com",
                "mail_domain": "mail.example.com",
                "stage": "提交密码后页面未继续",
                "steps": ["12:00:01 任务启动"],
            },
        }
        with mock.patch.object(task_process, "run_registration_task", lambda *args, **kwargs: outcome):
            result = openai_register.worker(1)

        self.assertFalse(result["ok"])
        items = self.service.list(type=LOG_TYPE_REGISTER)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"]["email"], "someone@mail.example.com")
        self.assertEqual(items[0]["detail"]["task_index"], 1)
        # 任务收尾后过程记录要丢干净，否则补号模式跑久了就是内存泄漏。
        self.assertEqual(openai_register.task_trace(1), [])

    def test_a_successful_registration_writes_nothing(self) -> None:
        outcome = {
            "ok": True,
            "result": {"email": "ok@mail.example.com", "access_token": "token"},
        }
        accounts = mock.Mock()
        accounts.refresh_accounts.return_value = {}
        with mock.patch.object(task_process, "run_registration_task", lambda *args, **kwargs: outcome), \
                mock.patch.object(openai_register, "account_service", accounts):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(self.service.list(), [], "成功的注册不进失败日志")
        accounts.add_account_items.assert_called_once()

    def test_a_parent_side_crash_still_leaves_a_record(self) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("主进程收尾炸了")

        with mock.patch.object(task_process, "run_registration_task", boom):
            result = openai_register.worker(1)

        self.assertFalse(result["ok"])
        items = self.service.list(type=LOG_TYPE_REGISTER)
        self.assertEqual(len(items), 1)
        self.assertIn("主进程收尾炸了", items[0]["detail"]["error"])
        self.assertTrue(items[0]["detail"]["traceback"])


class TaskProcessDetailTests(unittest.TestCase):
    """子进程的现场要原样过管道回到主进程；超时那一路则由父进程自己补。"""

    @staticmethod
    def stub(source: str) -> list[str]:
        return [sys.executable, "-u", "-c", source]

    def test_child_detail_is_passed_through(self) -> None:
        detail = {"email": "a@b.c", "stage": "停在密码页", "steps": ["一步"]}
        source = (
            "import json\n"
            "print(json.dumps({'type': 'result', 'ok': False, 'error': 'boom', 'fatal': False, "
            f"'detail': {detail!r}}}), flush=True)\n"
        )

        outcome = task_process.run_registration_task(1, {}, command=self.stub(source))

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["detail"], detail)

    def test_a_child_that_returns_nothing_falls_back_to_the_output_tail(self) -> None:
        source = (
            "import json\n"
            "print(json.dumps({'type': 'log', 'text': '[任务2] 邮箱创建完成[cf]: x@y.z'}), flush=True)\n"
            "print('raw chromium noise', flush=True)\n"
        )

        outcome = task_process.run_registration_task(2, {}, command=self.stub(source))

        self.assertFalse(outcome["ok"])
        steps = outcome["detail"]["steps"]
        self.assertTrue(any("邮箱创建完成" in step for step in steps))
        self.assertTrue(any("raw chromium noise" in step for step in steps))

    def test_timeout_keeps_the_tail_and_the_timeout_length(self) -> None:
        source = (
            "import json, time\n"
            "print(json.dumps({'type': 'log', 'text': '[任务3] 卡在验证码页'}), flush=True)\n"
            "time.sleep(30)\n"
        )

        outcome = task_process.run_registration_task(
            3, {}, timeout=1.0, command=self.stub(source)
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["detail"]["timeout_seconds"], 1)
        self.assertTrue(any("卡在验证码页" in step for step in outcome["detail"]["steps"]))


class RegisterLogExportTests(unittest.TestCase):
    """注册日志走的是同一套导出：type=register 一筛就是失败记录，报文完整。"""

    @staticmethod
    def register_line(day: str, index: int) -> str:
        return json.dumps({
            "id": f"r{index}",
            "time": f"{day} 10:00:00",
            "type": LOG_TYPE_REGISTER,
            "summary": f"任务{index} 注册失败：a@mail.example.com · 停在密码页",
            "detail": {
                "status": "failed",
                "task_index": index,
                "email": "a@mail.example.com",
                "mail_domain": "mail.example.com",
                "stage": "停在密码页",
                "steps": [f"12:00:0{index} 邮箱创建完成[cf]: a@mail.example.com"],
            },
        }, ensure_ascii=False, separators=(",", ":"))

    def test_export_keeps_the_register_fields_and_orders_by_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(
                "\n".join(self.register_line(f"2026-10-0{i}", i) for i in (1, 2, 3)) + "\n",
                encoding="utf-8",
            )

            text, count = LogService(path).build_export(type=LOG_TYPE_REGISTER, status="failed")

        self.assertEqual(count, 3)
        self.assertIn("mail.example.com", text)
        self.assertIn("停在密码页", text)
        self.assertIn("邮箱创建完成", text)
        # 由旧到新：先 10-01，最后 10-03。
        self.assertLess(text.index("任务1 注册失败"), text.index("任务3 注册失败"))

    def test_export_isolates_register_logs_from_call_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"id": "c1", "time": "2026-10-01 10:00:00", "type": "call", "summary": "调用失败",
                                "detail": {"status": "failed"}}, ensure_ascii=False),
                    self.register_line("2026-10-01", 1),
                ]) + "\n",
                encoding="utf-8",
            )

            text, count = LogService(path).build_export(type=LOG_TYPE_REGISTER)

        self.assertEqual(count, 1)
        self.assertIn("任务1 注册失败", text)
        self.assertNotIn("调用失败", text)


if __name__ == "__main__":
    unittest.main()
