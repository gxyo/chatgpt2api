"""域名 / 邮箱提供商的轮询必须跨进程活着，并且接着上次用的那个往下走。

注册任务改成独立子进程之后，进程内的计数器每个任务都从 0 重新开始，轮询退化成
"永远用第一个"——所有号都压在同一个域名上，正好撞上游的按域名限流。这里用真实的
子进程来钉住这个行为：进程内怎么测都测不出来，必须换个进程再取一次。

轮询的锚点是「上次用过的那个」本身而不是计数器取模：域名列表会被增删调序，计数器
一取模就跳号，看起来就跟"每次开始都取第一个"一样。
"""
import json
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

# 子进程：把状态文件指向临时目录，按给定域名列表取一次，打印结果。
_CHILD_SOURCE = "\n".join(
    [
        "from pathlib import Path",
        "import sys",
        "from services.register import mail_provider",
        "mail_provider.ROTATION_STATE_FILE = Path(sys.argv[1])",
        "mail_provider.ROTATION_STATE_LOCK_FILE = Path(sys.argv[2])",
        "print(mail_provider._next_domain(sys.argv[3:]))",
    ]
)


def _state_paths(tmp_dir: str) -> tuple[Path, Path]:
    root = Path(tmp_dir)
    return root / "rotation_state.json", root / "rotation_state.lock"


def _spawn(tmp_dir: str, domains: list[str] | None = None) -> subprocess.Popen:
    state_file, lock_file = _state_paths(tmp_dir)
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD_SOURCE, str(state_file), str(lock_file), *(domains or DOMAINS)],
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )


class CrossProcessRotationTests(unittest.TestCase):
    def _run_in_fresh_process(self, tmp_dir: str, domains: list[str] | None = None) -> str:
        proc = _spawn(tmp_dir, domains)
        stdout, _stderr = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)
        return stdout.strip()

    def test_domain_rotation_survives_a_fresh_process_per_task(self) -> None:
        """每个注册任务都是新进程，游标不能跟着进程一起归零。

        旧实现把游标放在模块全局里：这里是 6 个独立进程，每个都从 0 开始，
        会得到 6 个 a.example——线上表现就是所有号都用同一个域名。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            chosen = [self._run_in_fresh_process(tmp_dir) for _ in range(6)]

        self.assertEqual(chosen, ["a.example", "b.example", "c.example"] * 2)

    def test_rotation_resumes_from_the_last_used_domain(self) -> None:
        """下次开始要接着上次停的那个往下走，而不是从头再来。

        线上的说法：这次停的时候用的是第二个域名，下次开始就该用第三个。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, _lock_file = _state_paths(tmp_dir)
            state_file.write_text(json.dumps({"domain_last": "b.example"}), encoding="utf-8")

            # 上一次停在第二个域名 b，下一次就该是第三个 c。
            self.assertEqual(self._run_in_fresh_process(tmp_dir), "c.example")

    def test_rotation_wraps_back_to_the_first_after_the_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, _lock_file = _state_paths(tmp_dir)
            state_file.write_text(json.dumps({"domain_last": "c.example"}), encoding="utf-8")

            self.assertEqual(self._run_in_fresh_process(tmp_dir), "a.example")

    def test_reordered_list_follows_the_new_order(self) -> None:
        """锚点是域名本身，列表调序后跟着新顺序走，不是按老位置取模。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, _lock_file = _state_paths(tmp_dir)
            state_file.write_text(json.dumps({"domain_last": "b.example"}), encoding="utf-8")

            reordered = ["c.example", "b.example", "a.example"]
            self.assertEqual(self._run_in_fresh_process(tmp_dir, reordered), "a.example")

    def test_removed_domain_restarts_from_the_head(self) -> None:
        """上次用的域名被删掉了就从头开始，不能让轮询卡住或报错。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, _lock_file = _state_paths(tmp_dir)
            state_file.write_text(json.dumps({"domain_last": "gone.example"}), encoding="utf-8")

            self.assertEqual(self._run_in_fresh_process(tmp_dir), "a.example")

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

        # 9 次取号落在 9 个连续位置，三个域名各占 3 次。
        self.assertEqual(sorted(chosen), sorted(DOMAINS * 3))

    def test_state_file_records_the_last_used_domain(self) -> None:
        """状态文件要人能看懂：运维 `cat` 一下就知道上次停在哪个域名。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._run_in_fresh_process(tmp_dir)
            state_file, _lock_file = _state_paths(tmp_dir)
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8")), {"domain_last": "a.example"})


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

    def test_domain_and_provider_cursors_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file, lock_file = _state_paths(tmp_dir)
            with patch.object(mail_provider, "ROTATION_STATE_FILE", state_file), patch.object(
                mail_provider, "ROTATION_STATE_LOCK_FILE", lock_file
            ):
                self.assertEqual(mail_provider._next_rotation_index("domain_last", ["a", "b"]), 0)
                self.assertEqual(mail_provider._next_rotation_index("domain_last", ["a", "b"]), 1)
                # provider 走自己的游标，不受 domain 影响。
                self.assertEqual(mail_provider._next_rotation_index("provider_last", ["x", "y"]), 0)

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
