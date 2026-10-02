import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.register import browser_reaper


def _write_proc(proc_root: Path, pid: int, argv: list[str] | None) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    if argv is None:
        (proc_dir / "cmdline").write_bytes(b"")
        return
    (proc_dir / "cmdline").write_bytes(b"\0".join(arg.encode("utf-8") for arg in argv) + b"\0")


def _fake_chromium(pid: int, *, kind: str = "renderer") -> tuple[int, list[str]]:
    return pid, ["/root/.cache/ms-playwright/chromium-1234/chrome-linux/chrome", f"--type={kind}"]


class ListBrowserProcessesTests(unittest.TestCase):
    def test_lists_chromium_tree_and_playwright_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir)
            for pid, argv in (
                _fake_chromium(100, kind="zygote"),
                _fake_chromium(101, kind="gpu-process"),
                _fake_chromium(102, kind="renderer"),
                (103, ["/root/.cache/ms-playwright/chromium-1234/chrome-linux/chrome_crashpad_handler", "--database=/tmp"]),
                (104, ["/root/.cache/ms-playwright/driver/node", "/root/.cache/ms-playwright/driver/cli.js", "run-driver"]),
            ):
                _write_proc(proc_root, pid, argv)
            # 不该被误杀：普通 node 应用、python 本体、shell
            _write_proc(proc_root, 200, ["/usr/bin/node", "server.js"])
            _write_proc(proc_root, 201, ["/usr/local/bin/python", "-m", "uvicorn", "main:app"])
            _write_proc(proc_root, 202, ["/bin/sh", "-c", "ls"])
            _write_proc(proc_root, 203, [])  # 僵尸：cmdline 为空，没有身份信息

            pids = browser_reaper.list_browser_processes(str(proc_root))

        self.assertEqual(pids, [100, 101, 102, 103, 104])

    def test_comm_style_truncated_names_are_not_needed(self) -> None:
        """crashpad 的可执行名超过 15 字符，comm 会被截断，匹配必须走 cmdline。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir)
            _write_proc(proc_root, 100, ["/opt/chrome/chrome_crashpad_handler"])
            (proc_root / "100" / "comm").write_text("chrome_crashpa", encoding="utf-8")

            pids = browser_reaper.list_browser_processes(str(proc_root))

        self.assertEqual(pids, [100])

    def test_missing_proc_root_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "proc"

            self.assertEqual(browser_reaper.list_browser_processes(str(missing)), [])
            self.assertEqual(browser_reaper.kill_browser_processes(str(missing)), 0)

    def test_ignores_own_pid_and_init(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir)
            _write_proc(proc_root, os.getpid(), ["/opt/chrome/chrome"])
            _write_proc(proc_root, 1, ["/opt/chrome/chrome"])
            _write_proc(proc_root, 300, ["/opt/chrome/chrome"])

            pids = browser_reaper.list_browser_processes(str(proc_root))

        self.assertEqual(pids, [300])


class KillBrowserProcessesTests(unittest.TestCase):
    def _run_kill(self, lister, send_signal=None):
        signals: list[tuple[int, int]] = []
        with patch.object(browser_reaper, "reap_zombie_children", return_value=0):
            killed = browser_reaper.kill_browser_processes(
                "unused",
                send_signal=send_signal or (lambda pid, sig: signals.append((pid, sig))),
                sleep=lambda _seconds: None,
                proc_lister=lister,
            )
        return killed, signals

    def test_terminates_first_and_escalates_to_sigkill(self) -> None:
        # 每次扫描都还能看到 4242，模拟信号送不动的进程：先 TERM，之后升级 KILL。
        killed, signals = self._run_kill(lambda: [4242])

        self.assertEqual(killed, 1)
        # 用模块常量而不是 signal.SIGKILL：Windows 上没有后者，测试要能跨平台跑。
        self.assertEqual(
            [sig for _pid, sig in signals],
            [signal.SIGTERM, browser_reaper.SIGKILL, browser_reaper.SIGKILL],
        )
        self.assertTrue(all(pid == 4242 for pid, _sig in signals))

    def test_stops_without_signalling_when_nothing_matches(self) -> None:
        killed, signals = self._run_kill(lambda: [])

        self.assertEqual(killed, 0)
        self.assertEqual(signals, [])

    def test_missing_process_does_not_abort_the_sweep(self) -> None:
        """进程在扫描和发信号之间消失：ProcessLookupError 不能中断整轮清理。"""
        def flaky(pid, sig):
            raise ProcessLookupError(pid)

        killed, signals = self._run_kill(lambda: [7], send_signal=flaky)

        self.assertEqual(killed, 1)
        self.assertEqual(signals, [])


class ReapZombieChildrenTests(unittest.TestCase):
    def setUp(self) -> None:
        # 这几条把 waitpid 整个注入掉，测的是回收算法本身，不碰任何真实系统调用，
        # 所以在非 POSIX 上也要照常跑；平台闸门由下面那条用例单独验证。
        patcher = patch.object(browser_reaper, "CAN_REAP", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_waitpid_is_never_called_off_posix(self) -> None:
        """回收僵尸只在 POSIX 上做。

        Windows 的 os.waitpid 一旦收到「不是自己子进程」的 PID，就会污染 CPython
        的子进程记账，之后连 subprocess.Popen 都以 0xC0000142 失败——不可恢复，
        所以必须靠平台判定挡在调用之前，不能指望异常处理。
        """
        calls: list[int] = []

        def waitpid(pid, options):
            calls.append(pid)
            raise ChildProcessError(pid)

        with patch.object(browser_reaper, "CAN_REAP", False):
            self.assertEqual(browser_reaper.reap_zombie_children(waitpid=waitpid, pids=[7]), 0)
            self.assertEqual(browser_reaper.reap_zombie_children(waitpid=waitpid), 0)

        self.assertEqual(calls, [])

    def test_scoped_waitpid_tolerates_processes_that_are_not_children(self) -> None:
        calls: list[int] = []

        def waitpid(pid, options):
            calls.append(pid)
            raise ChildProcessError(pid)

        reaped = browser_reaper.reap_zombie_children(waitpid=waitpid, pids=[11, 12])

        self.assertEqual(reaped, 0)
        self.assertEqual(calls, [11, 12])

    def test_scoped_waitpid_reaps_until_child_is_gone(self) -> None:
        results = iter([(9, 0), (0, 0)])

        def waitpid(pid, options):
            outcome = next(results)
            if outcome is None:
                raise ChildProcessError(pid)
            return outcome

        reaped = browser_reaper.reap_zombie_children(waitpid=waitpid, pids=[9])

        self.assertEqual(reaped, 1)

    def test_full_waitpid_reaps_until_no_children_left(self) -> None:
        results = iter([(5, 0), (6, 0)])

        def waitpid(pid, options):
            try:
                return next(results)
            except StopIteration:
                raise ChildProcessError(pid)

        reaped = browser_reaper.reap_zombie_children(waitpid=waitpid)

        self.assertEqual(reaped, 2)


if __name__ == "__main__":
    unittest.main()
