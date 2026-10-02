"""把一次注册放进独立子进程执行，用完即弃。

以前每个任务跑在注册服务自己的线程里：新建 asyncio 事件循环 → 起 Playwright 的
Node 驱动 → 起一整棵 Chromium 进程树 → 收尾时 loop.close()。只要收尾没走完
（步骤卡死、驱动被超时打断），Chromium 就变成孤儿挂在容器里，越攒越多，最后
fork 时拿不到 PID（`Resource temporarily unavailable`）——"PID 用尽"。

改成子进程后，任务的生命周期和进程的生命周期绑死：
- 独立进程组（start_new_session），超时就对整个组 SIGKILL。Node 驱动和整棵
  Chromium 都在这组里，由内核回收，不存在"清理漏了哪个子进程"的可能；
- 超时有了硬上限，卡死的流程不会再永久占着线程和那棵进程树；
- 注册引擎再怎么抽风也只脏一个用完即弃的子进程，主进程不受影响。

进程间协议：子进程把结果和日志以 JSON 行写回 stdout（fd 1 由协议独占），
人类可读的输出（含 openai_register.log 的 print）全部改走 stderr 并继承给
容器日志，所以控制台观感和以前一致。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from services.register import mail_provider, openai_register

# 单次注册的硬上限。正常一个号 1~2 分钟，卡死的流程在这里被强行结束，
# 而不是无限期占着线程和槽位。
TASK_TIMEOUT_SECONDS = 600
# 强杀之后留给内核回收进程组的时间。
KILL_REAP_TIMEOUT_SECONDS = 30
_CONFIG_KEYS = ("mail", "proxy", "total", "threads", "engine")

# 子进程里指向真正的 stdout（fd 1）。模块级是因为日志 sink 是个回调。
_protocol_stream: Any = None


# --------------------------------------------------------------------------
# 父进程侧：起子进程、转发输出、收结果
# --------------------------------------------------------------------------


def _spawn_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        # 关键：独立进程组，超时才能一次性带走 Node 驱动和整棵 Chromium。
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # 子进程的输出要跨管道传回来，固定 UTF-8，别让 Windows 的 locale 编码把中文搅碎。
    env["PYTHONIOENCODING"] = "utf-8"
    repo_root = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = repo_root if not existing else os.pathsep.join([repo_root, existing])
    return env


def _child_command() -> list[str]:
    # 用 -m 而不是文件路径：直接跑文件的话 sys.path[0] 是 services/register，
    # 里面 import services.* 会失败。PYTHONPATH 里已经补了仓库根目录。
    return [sys.executable, "-u", "-m", "services.register.task_process"]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """整组带走。失败再退化成只杀子进程本身。"""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), 9)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


class _TaskOutput:
    """把子进程 stdout 上的 JSON 行分发到日志 sink、邮箱结果 sink 和结果槽。"""

    def __init__(
        self,
        on_log: Callable[[str, str], None] | None = None,
        on_mailbox_result: Callable[[dict], None] | None = None,
    ) -> None:
        self._on_log = on_log
        self._on_mailbox_result = on_mailbox_result
        self.result: dict[str, Any] | None = None

    def feed(self, raw: str) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            # 第三方库或 Chromium 直接往 fd 1 写的东西：当普通日志转发，别丢。
            self._forward_log(line)
            return
        kind = str(message.get("type") or "")
        if kind == "log":
            self._forward_log(str(message.get("text") or ""), str(message.get("color") or ""))
        elif kind == "mailbox_result":
            if self._on_mailbox_result is not None:
                try:
                    self._on_mailbox_result(message)
                except Exception:
                    pass
        elif kind == "result":
            self.result = message
        else:
            self._forward_log(line)

    def _forward_log(self, text: str, color: str = "") -> None:
        if self._on_log is None:
            return
        try:
            self._on_log(text, color)
        except Exception:
            pass


def _close_pipe(pipe: Any) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except OSError:
        pass


def _pump_output(proc: subprocess.Popen, output: _TaskOutput, name: str) -> threading.Thread:
    def reader() -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                output.feed(raw)
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=reader, daemon=True, name=name)
    thread.start()
    return thread


def run_registration_task(
    index: int,
    config_payload: dict[str, Any],
    *,
    timeout: float = TASK_TIMEOUT_SECONDS,
    command: list[str] | None = None,
    on_log: Callable[[str, str], None] | None = None,
    on_mailbox_result: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """跑一次注册，返回 {"ok", "result"|"error", "fatal"}。

    配置走 stdin 而不是命令行参数：邮箱池和代理凭据不能出现在 ps 里。
    """
    output = _TaskOutput(on_log, on_mailbox_result)
    payload = json.dumps({"index": index, "config": config_payload}, ensure_ascii=False)
    proc = subprocess.Popen(
        command or _child_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # stderr 继承：子进程的人类可读输出直接进容器日志，父进程不必再转发一遍。
        stderr=None,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=_child_env(),
        **_spawn_kwargs(),
    )
    reader = _pump_output(proc, output, f"register-task-{index}")
    try:
        if proc.stdin is not None:
            proc.stdin.write(payload)
            proc.stdin.close()
    except (BrokenPipeError, OSError):
        # 子进程启动就死了，下面按"没返回结果"处理。
        pass

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=KILL_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    finally:
        # 管道不显式关掉就是每个任务漏两个 fd：跑几百个号会把进程的 fd 耗光。
        reader.join(timeout=5)
        _close_pipe(proc.stdin)
        _close_pipe(proc.stdout)

    if timed_out:
        return {
            "ok": False,
            "fatal": False,
            "error": f"注册超过 {int(timeout)}s 未完成，已终止并回收进程组",
        }
    if output.result is None:
        return {
            "ok": False,
            "fatal": False,
            "error": f"注册子进程异常退出（退出码 {proc.returncode}）且未返回结果",
        }
    if not output.result.get("ok"):
        return {
            "ok": False,
            "fatal": bool(output.result.get("fatal")),
            "error": str(output.result.get("error") or "注册失败"),
        }
    result = output.result.get("result")
    return {"ok": True, "result": result if isinstance(result, dict) else {}}


# --------------------------------------------------------------------------
# 子进程侧：装协议、跑注册
# --------------------------------------------------------------------------


def _emit(message: dict[str, Any]) -> None:
    stream = _protocol_stream
    if stream is None:
        return
    try:
        stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def _install_child_protocol() -> None:
    """把 fd 1 让给协议，人类可读输出全部改道 stderr。"""
    global _protocol_stream
    _protocol_stream = sys.stdout
    sys.stdout = sys.stderr
    openai_register.register_log_sink = lambda text, color="": _emit(
        {"type": "log", "text": text, "color": color}
    )
    mail_provider.mailbox_result_sink = lambda mailbox, *, success, error=None: _emit(
        {
            "type": "mailbox_result",
            # 只带统计需要的字段：凭据没有过管道的必要。
            "mailbox": {
                "provider": str(mailbox.get("provider") or ""),
                "address": str(mailbox.get("address") or ""),
            },
            "success": bool(success),
        }
    )


def _run_registration(index: int) -> dict[str, Any]:
    """子进程里真正干活：按引擎跑一次注册，返回账号信息。"""
    engine = str(openai_register.config.get("engine") or "playwright").strip()
    if engine == "playwright":
        from services.register.playwright_register import register as playwright_register

        return playwright_register(index, openai_register.config["proxy"])
    registrar = openai_register.PlatformRegistrar(openai_register.config["proxy"])
    try:
        return registrar.register(index)
    finally:
        registrar.close()


def main() -> int:
    raw = sys.stdin.read() if sys.stdin is not None else ""
    payload = json.loads(raw) if raw.strip() else {}
    index = int(payload.get("index") or 0)
    config_payload = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    # 子进程是独立进程，拿不到主进程里那份 config 全局，必须由父进程显式传进来。
    openai_register.config.update({key: config_payload[key] for key in _CONFIG_KEYS if key in config_payload})
    _install_child_protocol()
    try:
        result = _run_registration(index)
    except Exception as error:
        message, fatal = openai_register._classify_worker_error(error)
        _emit({"type": "result", "ok": False, "error": message, "fatal": fatal})
    else:
        _emit({"type": "result", "ok": True, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
