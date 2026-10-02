"""域名 / 邮箱提供商的轮询游标必须跨进程活着。

注册任务改成独立子进程之后，进程内的计数器每个任务都从 0 重新开始，轮询退化成
"永远用第一个"——所有号都压在同一个域名上，正好撞上游的按域名限流。这里用真实的
子进程来钉住这个行为：进程内怎么测都测不出来，必须换个进程再取一次。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.register import mail_provider

ROOT_DIR = Path(__file__).resolve().parents[1]
DOMAINS = ["a.example", "b.example", "c.example"]

# 子进程：把状态文件指向临时目录，取一次域名并打印。
_CHILD_SOURCE = "\n".join(
    [
        "from pathlib import Path",
        "import sys",
        "from services.register import mail_provider",
        "mail_provider.ROTATION_STATE_FILE = Path(sys.argv[1])",
        "mail_provider.ROTATION_STATE_LOCK_FILE = Path(sys.argv[2])",
        f"print(mail_provider._next_domain({DOMAINS!r}))",
    ]
)


def _state_paths(tmp_dir: str) -> tuple[Path, Path]:
    root = Path(tmp_dir)
    return root / "rotation_state.json", root / "rotation_state.lock"


def _spawn(tmp_dir: str) -> subprocess.Popen:
    state_file, lock_file = _state_paths(tmp_dir)
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD_SOURCE, str(state_file), str(lock_file)],
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )


class CrossProcessRotationTests(unittest.TestCase):
    def test_domain_rotation_survives_a_fresh_process_per_task(self) -> None:
        """每个注册任务都是新进程，游标不能跟着进程一起归零。

        旧实现把游标放在模块全局里：这里是 6 个独立进程，每个都从 0 开始，
        会得到 6 个 a.example——线上表现就是所有号都用同一个域名。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            chosen = []
            for _ in range(6):
                proc = _spawn(tmp_dir)
                stdout, _stderr = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0)
                chosen.append(stdout.strip())

        self.assertEqual(chosen, ["a.example", "b.example", "c.example"] * 2)

    @unittest.skipUnless(os.name == "posix", "跨进程文件锁只在 POSIX 上生效")
    def test_concurrent_tasks_take_distinct_slots(self) -> None:
        """并发注册时游标的读-改-写要跨进程串行，不能两个任务拿到同一个域名。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            procs = [_spawn(tmp_dir) for _ in range(9)]
            chosen = []
            for proc in procs:
                stdout, _stderr = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0)
                chosen.append(stdout.strip())

        # 9 次取号落在 9 个连续游标上，三个域名各占 3 次。
        self.assertEqual(sorted(chosen), sorted(DOMAINS * 3))


class RotationCursorTests(unittest.TestCase):
    def test_single_domain_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, lock_file = _state_paths(tmp_dir)
            with patch.object(mail_provider, "ROTATION_STATE_FILE", state_file), patch.object(
                mail_provider, "ROTATION_STATE_LOCK_FILE", lock_file
            ):
                self.assertEqual(mail_provider._next_domain(["only.example"]), "only.example")
            # 只有一个域名时不该产生状态文件（省掉无谓的跨进程加锁）。
            self.assertFalse(state_file.exists())

    def test_empty_domain_list_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            mail_provider._next_domain([])

    def test_cursors_are_independent_per_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, lock_file = _state_paths(tmp_dir)
            with patch.object(mail_provider, "ROTATION_STATE_FILE", state_file), patch.object(
                mail_provider, "ROTATION_STATE_LOCK_FILE", lock_file
            ):
                self.assertEqual(mail_provider._next_rotation_index("domain"), 0)
                self.assertEqual(mail_provider._next_rotation_index("domain"), 1)
                # provider 走自己的游标，不受 domain 影响。
                self.assertEqual(mail_provider._next_rotation_index("provider"), 0)

    def test_unwritable_state_file_does_not_break_registration(self) -> None:
        """状态文件写不进去时退化成"仍用第一个"，但不能让注册整个失败。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, lock_file = _state_paths(tmp_dir)
            with patch.object(mail_provider, "ROTATION_STATE_FILE", state_file), patch.object(
                mail_provider, "ROTATION_STATE_LOCK_FILE", lock_file
            ), patch.object(mail_provider, "_atomic_write_text", side_effect=OSError("read-only")):
                self.assertEqual(mail_provider._next_domain(DOMAINS), "a.example")


if __name__ == "__main__":
    unittest.main()
