from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from services.log_service import LOG_TYPE_CALL, log_service
from utils.beijing_time import beijing_now

# 直接调用 /v1/images/* 的请求，成功与失败都会写入日志。
IMAGE_ENDPOINT_MODES = {
    "/v1/images/generations": "generate",
    "/v1/images/edits": "edit",
}
# 网页端的生图任务只在「内容审核拦截」时以这两个 endpoint 记一次失败日志；
# 任务真正跑完后由 image_task_service 以 /v1/images/* 记日志，因此这里只统计 failed，
# 避免同一次请求被重复计数。
IMAGE_TASK_ENDPOINT_MODES = {
    "/api/image-tasks/generations": "generate",
    "/api/image-tasks/edits": "edit",
}
MODE_LABELS = {"generate": "文生图", "edit": "图生图"}

GRANULARITY_HOUR = "hour"
GRANULARITY_DAY = "day"
# 单次查询最多返回的每日数据点，超出时只统计最近的这一段。
MAX_SERIES_POINTS = 1000
_STATUS_SUCCESS = "success"
# scope=all：不从入参取日期，改为从聚合里已知的最早一天算到今天。
SCOPE_ALL = "all"
SCOPE_RANGE = "range"

# 聚合快照的落盘格式版本。格式变了就换版本号，旧快照会被忽略并重建。
SNAPSHOT_VERSION = 1
# 两次落盘之间的最小间隔：统计每次查询都会刷新，不能每次都写盘。
# 因为游标和聚合是一起写入的，崩溃时两者一起回退到上一个一致点，重放日志即可，不会重复计数。
SNAPSHOT_INTERVAL_SECONDS = 30


def beijing_today() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def _parse_day(value: str):
    # strptime 接受 "2026-9-28" 这类未补零的写法，但它匹配不上日志里补零后的小时键，
    # 会静默统计成 0，所以这里额外要求严格的 YYYY-MM-DD 形状。
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    if not (value[:4].isdigit() and value[5:7].isdigit() and value[8:10].isdigit()):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _empty_mode_bucket() -> dict[str, int]:
    return {"requests": 0, "success": 0, "failed": 0}


def _empty_bucket() -> dict[str, Any]:
    return {
        "requests": 0,
        "success": 0,
        "failed": 0,
        "duration_ms": 0,
        "modes": {mode: _empty_mode_bucket() for mode in MODE_LABELS},
    }


def _restore_bucket(value: Any) -> dict[str, Any]:
    """把快照里读回来的桶重新整成内部结构，字段缺失或类型不对的按 0 处理。"""
    source = value if isinstance(value, dict) else {}
    bucket = _empty_bucket()
    for key in ("requests", "success", "failed", "duration_ms"):
        try:
            bucket[key] = max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            bucket[key] = 0
    modes = source.get("modes")
    if isinstance(modes, dict):
        for mode, mode_value in modes.items():
            target = bucket["modes"].setdefault(str(mode), _empty_mode_bucket())
            mode_source = mode_value if isinstance(mode_value, dict) else {}
            for key in ("requests", "success", "failed"):
                try:
                    target[key] = max(0, int(mode_source.get(key) or 0))
                except (TypeError, ValueError):
                    target[key] = 0
    return bucket


def _accumulate(bucket: dict[str, Any], *, mode: str, status: str, duration_ms: int) -> None:
    bucket["requests"] += 1
    bucket[status] += 1
    bucket["duration_ms"] += duration_ms
    mode_bucket = bucket["modes"].setdefault(mode, _empty_mode_bucket())
    mode_bucket["requests"] += 1
    mode_bucket[status] += 1


def _parse_duration(detail: dict[str, Any]) -> int:
    try:
        return max(0, int(detail.get("duration_ms") or 0))
    except (TypeError, ValueError):
        return 0


def _parse_image_event(raw_line: str) -> tuple[str, str, str, int] | None:
    """把一行日志解析成 (小时键, 模式, 状态, 耗时毫秒)，不是生图调用则返回 None。"""
    if "/v1/images/" not in raw_line and "/api/image-tasks/" not in raw_line:
        return None
    try:
        item = json.loads(raw_line)
    except Exception:
        return None
    if not isinstance(item, dict) or item.get("type") != LOG_TYPE_CALL:
        return None

    detail = item.get("detail")
    if not isinstance(detail, dict):
        return None

    endpoint = str(detail.get("endpoint") or "")
    status = _STATUS_SUCCESS if str(detail.get("status") or "") == _STATUS_SUCCESS else "failed"
    mode = IMAGE_ENDPOINT_MODES.get(endpoint)
    if mode is None:
        mode = IMAGE_TASK_ENDPOINT_MODES.get(endpoint)
        if mode is None or status != "failed":
            return None

    time_text = str(item.get("time") or "")
    if len(time_text) < 13 or time_text[10] != " " or _parse_day(time_text[:10]) is None:
        return None
    hour_text = time_text[11:13]
    if not hour_text.isdigit() or int(hour_text) > 23:
        return None
    return f"{time_text[:10]}T{hour_text}", mode, status, _parse_duration(detail)


def _normalize_range(start_date: str, end_date: str) -> tuple[str, str]:
    start = (start_date or "").strip()
    end = (end_date or "").strip()
    if not start and not end:
        start = end = beijing_today()
    elif not start:
        start = end
    elif not end:
        end = start

    for value in (start, end):
        if _parse_day(value) is None:
            raise ValueError("日期格式不正确，应为 YYYY-MM-DD")
    if end < start:
        start, end = end, start
    return start, end


def _iter_days(start: str, end: str) -> list[str]:
    first = _parse_day(start)
    last = _parse_day(end)
    if first is None or last is None:
        return []
    # 区间过长时只保留最近的一段，保证返回的 range 与实际统计口径一致。
    if (last - first).days + 1 > MAX_SERIES_POINTS:
        first = last - timedelta(days=MAX_SERIES_POINTS - 1)
    days: list[str] = []
    current = first
    while current <= last:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days


def _merge_day(hours: dict[str, dict[str, Any]], day: str) -> dict[str, Any]:
    merged = _empty_bucket()
    for hour in range(24):
        bucket = hours.get(f"{day}T{hour:02d}")
        if bucket is None:
            continue
        merged["requests"] += bucket["requests"]
        merged["success"] += bucket["success"]
        merged["failed"] += bucket["failed"]
        merged["duration_ms"] += bucket["duration_ms"]
        for mode, mode_bucket in bucket["modes"].items():
            target = merged["modes"].setdefault(mode, _empty_mode_bucket())
            for key in ("requests", "success", "failed"):
                target[key] += mode_bucket.get(key, 0)
    return merged


def _point(key: str, label: str, full_label: str, bucket: dict[str, Any] | None) -> dict[str, Any]:
    source = bucket or _empty_bucket()
    return {
        "key": key,
        "label": label,
        "full_label": full_label,
        "requests": source["requests"],
        "success": source["success"],
        "failed": source["failed"],
    }


def _merge_totals(totals: dict[str, Any], bucket: dict[str, Any]) -> None:
    totals["requests"] += bucket["requests"]
    totals["success"] += bucket["success"]
    totals["failed"] += bucket["failed"]
    totals["duration_ms"] += bucket["duration_ms"]
    for mode, mode_bucket in bucket["modes"].items():
        target = totals["modes"].setdefault(mode, _empty_mode_bucket())
        for key in ("requests", "success", "failed"):
            target[key] += mode_bucket.get(key, 0)


class ImageStatsService:
    """按小时增量聚合生图调用日志，为统计页面提供汇总与峰值序列。

    logs.jsonl 是只增日志——每来一个请求就追加一行——所以不能按 mtime/大小做缓存：
    那种指纹在一个有流量的实例上几乎每次查询都会变，等于每次查询都在全量重扫。
    这里改为记住「已经聚合到第几个字节」，每次只解析新追加的尾部。

    全量重扫的代价随日志体积线性增长（实测：5 千行 45ms、5 万行 465ms、
    20 万行 2.1s、50 万行 8.9s），增量之后单次查询只与两次查询之间新增的日志量有关。

    聚合结果连同游标一起落盘（默认与日志同目录的 `image_stats.json`），因此统计的寿命
    不取决于日志的寿命：日志按保留期清理掉之后，已计入的统计仍然保留，重启也不会丢。

    对应地，**聚合只增不减**——删日志是存储清理，不代表这些请求没发生过。而且日志被
    按时间截断后，即便重建也只能看到剩下的那一段，重建等于把更早的历史一并抹掉。
    """

    # 已消费内容的尾部锚点。文件被重写、截断或轮转时这段字节会对不上，据此重新对齐游标。
    _ANCHOR_BYTES = 64

    def __init__(self, path: Path, snapshot_path: Path | None = None):
        self.path = path
        self.snapshot_path = snapshot_path or path.parent / "image_stats.json"
        self._lock = threading.Lock()
        self._reset()
        self._load_snapshot()

    def _reset(self) -> None:
        self._hours: dict[str, dict[str, Any]] = {}
        self._offset = 0
        self._anchor = b""
        self._last_save = 0.0

    def _load_snapshot(self) -> None:
        """载入上次落盘的聚合与游标。读不到或格式对不上就从零开始重建。"""
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION:
            return
        hours = snapshot.get("hours")
        if not isinstance(hours, dict):
            return
        restored: dict[str, dict[str, Any]] = {}
        for key, value in hours.items():
            if not isinstance(key, str):
                continue
            restored[key] = _restore_bucket(value)
        try:
            self._offset = max(0, int(snapshot.get("offset") or 0))
        except (TypeError, ValueError):
            self._offset = 0
        try:
            self._anchor = bytes.fromhex(str(snapshot.get("anchor") or ""))
        except ValueError:
            self._anchor = b""
        # 载入时游标与聚合是对齐的，不必马上回写。
        self._hours = restored
        self._last_save = time.monotonic()

    def _save_snapshot(self, *, force: bool = False) -> None:
        """把聚合和游标一起原子落盘。写失败只影响下次重启，不该让查询出错。"""
        now = time.monotonic()
        if not force and now - self._last_save < SNAPSHOT_INTERVAL_SECONDS:
            return
        payload = {
            "version": SNAPSHOT_VERSION,
            "hours": self._hours,
            "offset": self._offset,
            "anchor": self._anchor.hex(),
        }
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.snapshot_path.with_suffix(f"{self.snapshot_path.suffix}.tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            temp_path.replace(self.snapshot_path)
        except OSError:
            return
        self._last_save = now

    def _anchor_matches(self) -> bool:
        """确认 [offset-len(anchor), offset) 这段仍是上次读到的内容。"""
        if not self._anchor:
            return self._offset == 0
        start = self._offset - len(self._anchor)
        if start < 0:
            return False
        try:
            with self.path.open("rb") as handle:
                handle.seek(start)
                return handle.read(len(self._anchor)) == self._anchor
        except OSError:
            return False

    def _resync(self) -> None:
        """文件被改写后重新对齐游标，已计入的聚合不回退。

        按时间清理日志只是把最老的一段前缀删掉，保留下来的尾部字节与上次读到的完全一致，
        所以靠尾部锚点就能算出新的游标位置，既不重复计数也不丢历史。
        逐条删除（中间挖洞）会让后面的字节整体前移，锚点也随之移动，同样能找回来。
        锚点确实找不到时（内容被大改）只能退到最后一个完整行：宁可漏掉少数尚未计入的行，
        也不重复计数。
        """
        try:
            data = self.path.read_bytes()
        except OSError:
            return
        if self._anchor:
            # 取最靠后的一处：万一这段字节在文件里出现过多次，靠后的那个才不会重复计数。
            found = data.rfind(self._anchor)
            if found >= 0:
                self._offset = found + len(self._anchor)
                return
        cut = data.rfind(b"\n")
        self._offset = cut + 1 if cut >= 0 else 0
        self._anchor = data[max(0, self._offset - self._ANCHOR_BYTES) : self._offset]

    def _refresh(self) -> None:
        """把新追加的日志并进聚合结果；文件被改写时只重新对齐游标，不重建聚合。"""
        try:
            size = self.path.stat().st_size
        except OSError:
            # 文件暂时读不到（尚未创建等）：保持现状，等下次查询再试。
            return
        # 变小说明被截断（例如按保留期清理）；锚点对不上说明内容被改写（例如逐条删除）。
        resynced = size < self._offset or not self._anchor_matches()
        if resynced:
            self._resync()
            try:
                size = self.path.stat().st_size
            except OSError:
                return
        if size > self._offset:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                payload = handle.read()
            # 只消费到最后一个换行符：日志是边写边追加的，末尾可能留了半行。
            cut = payload.rfind(b"\n")
            if cut >= 0:
                consumed = payload[: cut + 1]
                self._consume(consumed.decode("utf-8", errors="replace"))
                self._offset += len(consumed)
                self._anchor = (self._anchor + consumed)[-self._ANCHOR_BYTES :]
        # 游标动过就必须落盘，否则重启后会退回旧游标，把已经计入的行再数一遍。
        self._save_snapshot(force=resynced)

    def _consume(self, text: str) -> None:
        for raw_line in text.splitlines():
            event = _parse_image_event(raw_line)
            if event is None:
                continue
            hour_key, mode, status, duration_ms = event
            bucket = self._hours.setdefault(hour_key, _empty_bucket())
            _accumulate(bucket, mode=mode, status=status, duration_ms=duration_ms)

    def summary(self, start_date: str = "", end_date: str = "", scope: str = "") -> dict[str, Any]:
        scope = SCOPE_ALL if (scope or "").strip().lower() == SCOPE_ALL else SCOPE_RANGE

        # 汇总全程持锁：既避免并发查询各自重扫一遍，也避免读到正被 _consume 改写的桶。
        # scope=all 的起点要读聚合，所以区间归一化也一并放进锁里。
        with self._lock:
            self._refresh()
            if scope == SCOPE_ALL:
                # 全部：从聚合里最早的一天算到今天。聚合只增不减，所以这里给的是「有记录以来的全部」，
                # 日志过了保留期被清掉也不影响更早的统计。
                start = min(self._hours)[:10] if self._hours else beijing_today()
                end = beijing_today()
                if end < start:
                    start = end
            else:
                start, end = _normalize_range(start_date, end_date)
            days = _iter_days(start, end)
            if days:
                # 超出上限时只统计最近的一段，返回的 range 与统计口径保持一致。
                start, end = days[0], days[-1]

            hours = self._hours
            granularity = GRANULARITY_HOUR if start == end else GRANULARITY_DAY

            series: list[dict[str, Any]] = []
            totals = _empty_bucket()
            if granularity == GRANULARITY_HOUR:
                for hour in range(24):
                    key = f"{start}T{hour:02d}"
                    label = f"{hour:02d}:00"
                    bucket = hours.get(key)
                    series.append(_point(key, label, f"{start} {label}", bucket))
                    _merge_totals(totals, bucket or _empty_bucket())
            else:
                for day in days:
                    bucket = _merge_day(hours, day)
                    series.append(_point(day, day[5:], day, bucket))
                    _merge_totals(totals, bucket)

        peak = max(series, key=lambda item: item["requests"], default=None)
        if peak is not None and peak["requests"] <= 0:
            peak = None

        requests = totals["requests"]
        by_mode = [
            {
                "mode": mode,
                "label": label,
                "requests": totals["modes"][mode]["requests"],
                "success": totals["modes"][mode]["success"],
                "failed": totals["modes"][mode]["failed"],
            }
            for mode, label in MODE_LABELS.items()
        ]

        return {
            "range": {
                "start_date": start,
                "end_date": end,
                "granularity": granularity,
                "days": len(days),
                "scope": scope,
            },
            "totals": {
                "requests": requests,
                "success": totals["success"],
                "failed": totals["failed"],
                "success_rate": (totals["success"] / requests) if requests else 0.0,
                "avg_duration_ms": int(totals["duration_ms"] / requests) if requests else 0,
            },
            "by_mode": by_mode,
            "series": series,
            "peak": peak,
        }


image_stats_service = ImageStatsService(log_service.path)


def build_image_stats(start_date: str = "", end_date: str = "", scope: str = "") -> dict[str, Any]:
    return image_stats_service.summary(start_date, end_date, scope)


__all__ = [
    "SCOPE_ALL",
    "SCOPE_RANGE",
    "ImageStatsService",
    "beijing_now",
    "beijing_today",
    "build_image_stats",
    "image_stats_service",
]
