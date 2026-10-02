"""回收 Playwright 泄漏的 Chromium / Node 驱动进程。

每注册一个号都会新起一棵 Chromium 进程树和一个 Node 驱动进程，正常收尾靠
`browser.close()` + `async with async_playwright()` 退出。只要有一环没走到
（单步卡死、驱动被超时打断、Cloudflare 挑战循环），那棵树就变成孤儿挂在容器里，
越攒越多，最后 fork 时拿不到 PID（`Resource temporarily unavailable`）——
也就是"PID 用尽"。容器的 init 只负责收尸僵尸，不会杀掉还活着的孤儿。

这里只做两件事：认出这些进程，把它们连同子进程一起送走。

刻意不依赖 ps：容器镜像是 python:slim，没有 procps，直接读 /proc。
非 Linux 平台（开发机是 Windows）没有 /proc，所有函数退化成空操作。
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

PROC_ROOT = "/proc"

# 按 argv[0] 的 basename 匹配。用 cmdline 而不是 /proc/<pid>/comm，因为 comm 会被
# 截断到 15 字符（chrome_crashpad_handler 根本匹配不上），而且 cmdline 是普通文件，
# 测试里可以直接造假，不用创建符号链接。
BROWSER_EXE_NAMES = frozenset({
    "chrome",
    "chromium",
    "chromium-browser",
    "headless_shell",
    "chrome_crashpad_handler",
})
NODE_EXE_NAMES = frozenset({"node", "nodejs"})
# node 太宽泛，必须再命中 Playwright 驱动的特征才认。本项目没有别的 node 用法。
NODE_CMDLINE_MARKERS = ("playwright", "run-driver", "ms-playwright")

SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
# Windows 既没有 SIGKILL 也没有 WNOHANG；回收逻辑在那边整体是空操作，只是别在导入期炸掉。
WNOHANG = getattr(os, "WNOHANG", 1)
# 僵尸是 POSIX 概念，Windows 上根本没有可回收的僵尸；而且 Windows 的 os.waitpid
# 对「不是自己子进程的 PID」调用一次就会污染 CPython 的子进程记账，之后连
# subprocess.Popen 都会以 0xC0000142 失败。生产跑在 Linux 容器里，开发机是 Windows，
# 所以按平台整体关掉。显式判定而不是靠异常捕获：上面那个污染是不可恢复的。
CAN_REAP = os.name == "posix"

TERMINATE_GRACE_SECONDS = 1.0
KILL_GRACE_SECONDS = 0.2
MAX_ROUNDS = 3


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _cmdline_argv(proc_dir: Path) -> list[str]:
    raw = _read_bytes(proc_dir / "cmdline")
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _is_browser_process(proc_dir: Path) -> bool:
    argv = _cmdline_argv(proc_dir)
    if not argv:
        # 空 cmdline 是僵尸——没有身份信息，不能凭猜测杀，交给 init 或
        # reap_zombie_children() 处理。
        return False
    exe_name = os.path.basename(argv[0])
    if exe_name in BROWSER_EXE_NAMES:
        return True
    if exe_name in NODE_EXE_NAMES:
        joined = " ".join(argv).lower()
        return any(marker in joined for marker in NODE_CMDLINE_MARKERS)
    return False


def list_browser_processes(proc_root: str = PROC_ROOT) -> list[int]:
    """列出疑似泄漏的 Chromium / Node 驱动进程 PID。"""
    root = Path(proc_root)
    if not root.is_dir():
        return []
    own_pid = os.getpid()
    pids: list[int] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 1 or pid == own_pid:
            continue
        if _is_browser_process(entry):
            pids.append(pid)
    return sorted(pids)


def _signal_process(pid: int, sig: int, send_signal) -> bool:
    try:
        send_signal(pid, sig)
        return True
    except OSError:
        return False


def kill_browser_processes(
    proc_root: str = PROC_ROOT,
    *,
    send_signal=os.kill,
    sleep=time.sleep,
    proc_lister=None,
) -> int:
    """杀掉所有匹配的浏览器进程，返回见过的 PID 数。

    先 SIGTERM 让 Chromium 自己把子进程带走；直接 SIGKILL 主进程会把 renderer
    变成新的孤儿，所以只有 TERM 之后还赖着不走的才升级到 KILL。
    """
    lister = proc_lister if proc_lister is not None else (lambda: list_browser_processes(proc_root))
    seen: set[int] = set()
    for round_index in range(MAX_ROUNDS):
        pids = lister()
        if not pids:
            break
        sig = signal.SIGTERM if round_index == 0 else SIGKILL
        for pid in pids:
            if pid <= 1 or pid == os.getpid():
                continue
            _signal_process(pid, sig, send_signal)
        seen.update(pid for pid in pids if pid > 1 and pid != os.getpid())
        sleep(TERMINATE_GRACE_SECONDS if round_index == 0 else KILL_GRACE_SECONDS)
    if seen:
        reap_zombie_children(pids=sorted(seen))
    return len(seen)


def reap_zombie_children(*, waitpid=os.waitpid, pids: list[int] | None = None) -> int:
    """回收已死的子进程，避免它们以僵尸状态占着 PID。

    给了 pids 就只对这些 PID 做 waitpid：不会抢走 asyncio 自己的子进程。
    不给 pids 就扫全部子进程，此时必须由调用方保证没有任何任务在跑——
    本项目除 Playwright 外不 spawn 任何子进程，空闲时 waitpid 到的子进程
    必然都是泄漏残留。
    """
    if not CAN_REAP:
        return 0
    reaped = 0
    if pids is not None:
        for pid in pids:
            while True:
                try:
                    done, _status = waitpid(pid, WNOHANG)
                except (ChildProcessError, OSError):
                    break
                if done == 0:
                    break
                reaped += 1
        return reaped
    while True:
        try:
            done, _status = waitpid(-1, WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if done == 0:
            break
        reaped += 1
    return reaped
