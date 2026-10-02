import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from services.register import task_process


def _stub(source: str) -> list[str]:
    return [sys.executable, "-u", "-c", source]


def _emit(message: dict) -> str:
    return f"print(json.dumps({message!r}), flush=True)"


def _pid_gone(pid: int, timeout: float = 3.0) -> bool:
    """等进程真正结束（僵尸不算活着）。

    被 SIGKILL 的进程可能短暂处于僵尸态，取决于谁负责收尸：生产容器有
    `init: true`，tini 会立刻收走；这里的裸容器没有 init，孤儿会一直挂着。
    僵尸不再运行、不占 CPU 和内存，只占一个 PID 位，算已回收。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()[0]
            if state == b"Z":
                return True
        except FileNotFoundError:
            return True
        except (OSError, IndexError):
            pass
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


class RunRegistrationTaskTests(unittest.TestCase):
    def test_result_and_logs_are_forwarded(self) -> None:
        logs: list[tuple[str, str]] = []
        source = (
            "import json\n"
            + _emit({"type": "log", "text": "子进程日志", "color": "yellow"})
            + "\nprint('裸输出', flush=True)\n"
            + _emit({"type": "result", "ok": True, "result": {"email": "a@b.c"}})
        )

        outcome = task_process.run_registration_task(
            1,
            {},
            command=_stub(source),
            on_log=lambda text, color="": logs.append((text, color)),
        )

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"], {"email": "a@b.c"})
        self.assertIn(("子进程日志", "yellow"), logs)
        # 非协议输出（第三方库或 Chromium 直接写 fd 1）不能丢。
        self.assertTrue(any("裸输出" in text for text, _color in logs))

    def test_config_is_passed_through_stdin(self) -> None:
        source = (
            "import json, sys\n"
            "payload = json.loads(sys.stdin.read())\n"
            "print(json.dumps({'type': 'result', 'ok': True, 'result': payload['config']}), flush=True)\n"
        )

        outcome = task_process.run_registration_task(
            7,
            {"proxy": "socks5://user:pass@host:1080", "engine": "playwright"},
            command=_stub(source),
        )

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["proxy"], "socks5://user:pass@host:1080")

    def test_child_failure_result_carries_error_and_fatal(self) -> None:
        source = "import json\n" + _emit({"type": "result", "ok": False, "error": "boom", "fatal": True})

        outcome = task_process.run_registration_task(2, {}, command=_stub(source))

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "boom")
        self.assertTrue(outcome["fatal"])

    def test_child_exiting_without_a_result_is_reported(self) -> None:
        outcome = task_process.run_registration_task(3, {}, command=_stub("pass"))

        self.assertFalse(outcome["ok"])
        self.assertIn("未返回结果", outcome["error"])
        self.assertFalse(outcome["fatal"])

    def test_timeout_kills_the_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            marker = Path(tmp_dir) / "finished.txt"
            source = (
                "import time, pathlib\n"
                "time.sleep(2)\n"
                f"pathlib.Path({str(marker)!r}).write_text('done')\n"
            )

            started = time.monotonic()
            outcome = task_process.run_registration_task(
                5, {}, timeout=0.5, command=_stub(source)
            )
            elapsed = time.monotonic() - started

            self.assertFalse(outcome["ok"])
            self.assertIn("未完成", outcome["error"])
            self.assertLess(elapsed, 2.0, "超时必须立刻返回，不能等子进程自己结束")
            # 子进程真被杀了：睡满 2 秒后也不会留下标记文件。
            time.sleep(2.5)
            self.assertFalse(marker.exists(), "子进程没有被杀掉")

    @unittest.skipUnless(os.name == "posix", "进程组回收只在 POSIX 上有保证")
    def test_timeout_kills_the_whole_process_group(self) -> None:
        """超时后连子进程的子孙（Node 驱动 / Chromium）一起回收——这是治本的关键。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "grandchild.pid"
            source = (
                "import subprocess, sys, time, pathlib\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
                "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )

            outcome = task_process.run_registration_task(
                6, {}, timeout=2.0, command=_stub(source)
            )

            self.assertFalse(outcome["ok"])
            self.assertTrue(pid_file.exists(), "子进程没来得及记录孙进程 pid")
            grandchild_pid = int(pid_file.read_text())
            self.assertTrue(
                _pid_gone(grandchild_pid),
                f"孙进程 {grandchild_pid} 还活着，说明只杀了子进程而没有杀整个进程组",
            )


class TaskOutputTests(unittest.TestCase):
    def test_non_protocol_lines_become_plain_logs(self) -> None:
        logs: list[tuple[str, str]] = []
        output = task_process._TaskOutput(lambda text, color="": logs.append((text, color)))

        output.feed("DevTools listening on ws://127.0.0.1:9222\n")
        output.feed("[1,2,3]\n")
        output.feed("   \n")

        self.assertEqual(logs, [("DevTools listening on ws://127.0.0.1:9222", ""), ("[1,2,3]", "")])
        self.assertIsNone(output.result)

    def test_mailbox_result_messages_are_dispatched(self) -> None:
        events: list[dict] = []
        output = task_process._TaskOutput(on_mailbox_result=events.append)

        output.feed(json.dumps({
            "type": "mailbox_result",
            "mailbox": {"provider": "cloudflare_temp_email", "address": "a@b.c"},
            "success": True,
        }) + "\n")

        self.assertEqual(
            events,
            [{
                "type": "mailbox_result",
                "mailbox": {"provider": "cloudflare_temp_email", "address": "a@b.c"},
                "success": True,
            }],
        )

    def test_broken_sink_does_not_break_dispatch(self) -> None:
        def broken(text: str, color: str = "") -> None:
            raise OSError("sink down")

        output = task_process._TaskOutput(broken)
        output.feed('{"type": "log", "text": "hi"}\n')
        output.feed('{"type": "result", "ok": true, "result": {"email": "x@y.z"}}\n')

        self.assertEqual(output.result["result"], {"email": "x@y.z"})

    def test_stdout_is_owned_by_the_protocol(self) -> None:
        """子进程里 fd 1 只走协议，人类可读输出改道 stderr。"""
        installed = {
            "log_sink": task_process.openai_register.register_log_sink,
            "mailbox_sink": task_process.mail_provider.mailbox_result_sink,
            "stdout": sys.stdout,
            "stderr": sys.stderr,
            "protocol": task_process._protocol_stream,
        }
        try:
            task_process._install_child_protocol()
            self.assertIs(sys.stdout, sys.stderr)
            self.assertIs(task_process._protocol_stream, installed["stdout"])
        finally:
            task_process.openai_register.register_log_sink = installed["log_sink"]
            task_process.mail_provider.mailbox_result_sink = installed["mailbox_sink"]
            sys.stdout = installed["stdout"]
            sys.stderr = installed["stderr"]
            task_process._protocol_stream = installed["protocol"]


class OutlookPoolLockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "跨进程文件锁只在 POSIX 上生效")
    def test_pool_state_write_is_serialised_across_processes(self) -> None:
        """子进程化后邮箱池的「读-改-写」必须跨进程串行，否则同一个邮箱会被重复发放。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "outlook_token_used.json"
            lock_file = Path(tmp_dir) / "outlook_token_used.lock"
            source = (
                "import json, sys\n"
                "from pathlib import Path\n"
                "from services.register import mail_provider\n"
                f"mail_provider.OUTLOOK_TOKEN_USED_FILE = Path({str(state_file)!r})\n"
                f"mail_provider.OUTLOOK_TOKEN_USED_LOCK_FILE = Path({str(lock_file)!r})\n"
                "address = sys.argv[1]\n"
                "mail_provider._set_outlook_token_state(address, 'in_use')\n"
                "print(json.dumps({'type': 'result', 'ok': True, 'result': {}}), flush=True)\n"
            )

            procs = [
                subprocess.Popen(
                    [sys.executable, "-u", "-c", source, f"person{index}@example.com"],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdout=subprocess.DEVNULL,
                )
                for index in range(8)
            ]
            for proc in procs:
                proc.wait(timeout=60)

            saved = json.loads(state_file.read_text(encoding="utf-8"))

        # 8 个进程各写一条互不相同的记录，一条都不能被别的进程的旧快照覆盖掉。
        self.assertEqual(len(saved), 8)
        self.assertTrue(all(str(value.get("state")) == "in_use" for value in saved.values()))


if __name__ == "__main__":
    unittest.main()
