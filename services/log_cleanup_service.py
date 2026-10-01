from __future__ import annotations

import json
import threading
from datetime import timedelta

from services.config import DATA_DIR, config
from services.log_service import log_service
from utils.beijing_time import beijing_now
from utils.log import logger

# 每日自动清理的检查间隔。检查本身只是比对日期，代价可以忽略，半小时足够及时。
LOG_CLEANUP_CHECK_SECONDS = 1800
STATE_FILE = DATA_DIR / "log_cleanup_state.json"
_state_lock = threading.Lock()


def beijing_today() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def cutoff_day_for(days: int) -> str:
    """保留最近 `days` 天时，该删到哪一天为止。

    days=1 表示只留今天，删掉今天之前的所有日志。
    """
    keep = max(1, int(days))
    return (beijing_now() - timedelta(days=keep - 1)).strftime("%Y-%m-%d")


def log_storage_info() -> dict[str, object]:
    """日志文件当前的占用情况。只做 stat，不扫文件内容。"""
    try:
        stat = log_service.path.stat()
        size_bytes, updated_at = stat.st_size, int(stat.st_mtime)
    except OSError:
        size_bytes, updated_at = 0, 0
    return {
        "days": config.log_retention_days,
        "auto_cleanup": config.log_auto_cleanup,
        "size_bytes": size_bytes,
        "updated_at": updated_at,
    }


def cleanup_logs(days: int | None = None) -> dict[str, object]:
    """按保留期清理日志，返回删了多少条、还剩多少条。

    只删日志，不动统计：统计服务已经把这些请求聚合到自己的快照里了，
    日志被截断后它只会把增量游标往前挪，不会重新计数。
    """
    retention = config.log_retention_days if days is None else max(1, int(days))
    cutoff_day = cutoff_day_for(retention)
    result = log_service.cleanup_before(cutoff_day)
    try:
        size_bytes = log_service.path.stat().st_size
    except OSError:
        size_bytes = 0
    logger.info({
        "event": "log_cleanup_done",
        "cutoff_day": cutoff_day,
        "removed": result["removed"],
        "kept": result["kept"],
    })
    return {
        "days": retention,
        "cutoff_day": cutoff_day,
        "removed": result["removed"],
        "kept": result["kept"],
        "size_bytes": size_bytes,
    }


def _load_state() -> dict[str, object]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(state: dict[str, object]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(STATE_FILE)


def run_scheduled_log_cleanup_if_due() -> dict[str, object] | None:
    """每天跑一次日志清理，未开启自动清理时什么都不做。"""
    if not config.log_auto_cleanup:
        return None

    today = beijing_today()
    with _state_lock:
        state = _load_state()
        if state.get("last_run_day") == today:
            return None
        # 第一次见到这个开关时只记下日期、不立刻删：给用户一个反悔的机会，
        # 想马上清就用页面上的手动清理。之后跨天才会自动执行。
        if state.get("last_run_day") is None:
            _save_state({"last_run_day": today})
            return None
        result = cleanup_logs()
        _save_state({"last_run_day": today, "last_run_at": beijing_now().isoformat(timespec="seconds")})
        return result


def _worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            run_scheduled_log_cleanup_if_due()
        except Exception as exc:
            logger.error({"event": "scheduled_log_cleanup_failed", "error": str(exc)})
        stop_event.wait(LOG_CLEANUP_CHECK_SECONDS)


def start_log_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=_worker, args=(stop_event,), daemon=True, name="log-cleanup")
    thread.start()
    return thread


__all__ = [
    "cleanup_logs",
    "cutoff_day_for",
    "log_storage_info",
    "run_scheduled_log_cleanup_if_due",
    "start_log_cleanup_scheduler",
]
