from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.log_service import LOG_TYPE_CALL, log_service

BEIJING_TZ = timezone(timedelta(hours=8))

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


def beijing_now() -> datetime:
    """当前北京时间（UTC+8）的 naive datetime，不受宿主机时区影响。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


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
    """按小时聚合生图调用日志，为统计页面提供汇总与峰值序列。

    日志逐行扫描的开销只发生在文件变化后的第一次查询，之后命中缓存。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._fingerprint: tuple[int, int] | None = None
        self._aggregate: dict[str, Any] | None = None

    def _current_fingerprint(self) -> tuple[int, int]:
        try:
            stat = self.path.stat()
        except OSError:
            return (0, 0)
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> dict[str, Any]:
        fingerprint = self._current_fingerprint()
        with self._lock:
            if self._aggregate is not None and self._fingerprint == fingerprint:
                return self._aggregate

        aggregate = self._build()
        with self._lock:
            self._fingerprint = fingerprint
            self._aggregate = aggregate
        return aggregate

    def _build(self) -> dict[str, Any]:
        hours: dict[str, dict[str, Any]] = {}
        totals = _empty_bucket()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"hours": hours, "totals": totals}

        for raw_line in lines:
            event = _parse_image_event(raw_line)
            if event is None:
                continue
            hour_key, mode, status, duration_ms = event
            bucket = hours.setdefault(hour_key, _empty_bucket())
            _accumulate(totals, mode=mode, status=status, duration_ms=duration_ms)
            _accumulate(bucket, mode=mode, status=status, duration_ms=duration_ms)
        return {"hours": hours, "totals": totals}

    def summary(self, start_date: str = "", end_date: str = "") -> dict[str, Any]:
        start, end = _normalize_range(start_date, end_date)
        days = _iter_days(start, end)
        if days:
            # 超出上限时只统计最近的一段，返回的 range 与统计口径保持一致。
            start, end = days[0], days[-1]

        hours = self._load()["hours"]
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


def build_image_stats(start_date: str = "", end_date: str = "") -> dict[str, Any]:
    return image_stats_service.summary(start_date, end_date)


__all__ = [
    "ImageStatsService",
    "beijing_now",
    "beijing_today",
    "build_image_stats",
    "image_stats_service",
]
