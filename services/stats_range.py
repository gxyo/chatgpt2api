"""按天/按小时统计共用的时间区间工具。

生图统计（``stats_service``）与注册统计（``register_stats_service``）都要把「一个时间区间」
展开成一串按天或按小时的桶，展开与校验的规则必须完全一致，否则两张波形图的「今天」
会指向不同的东西。这些函数原本住在 ``services/stats_service.py`` 里，但那个模块在 import
时会实例化 ``ImageStatsService``（并读取真实的日志快照），注册侧的调用方不应该为了几个
纯函数被拖进这个副作用，所以单独抽到这里。

纯函数，无模块级状态、无 I/O。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from utils.beijing_time import beijing_now

__all__ = [
    "GRANULARITY_DAY",
    "GRANULARITY_HOUR",
    "MAX_SERIES_POINTS",
    "SCOPE_ALL",
    "SCOPE_RANGE",
    "beijing_today",
    "iter_days",
    "normalize_range",
    "parse_day",
]

GRANULARITY_HOUR = "hour"
GRANULARITY_DAY = "day"
# 单次查询最多返回的每日数据点，超出时只统计最近的这一段。
MAX_SERIES_POINTS = 1000
# scope=all：不从入参取日期，改为从聚合里已知的最早一天算到今天。
SCOPE_ALL = "all"
SCOPE_RANGE = "range"


def beijing_today() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def parse_day(value: str):
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


def normalize_range(start_date: str, end_date: str) -> tuple[str, str]:
    start = (start_date or "").strip()
    end = (end_date or "").strip()
    if not start and not end:
        start = end = beijing_today()
    elif not start:
        start = end
    elif not end:
        end = start

    for value in (start, end):
        if parse_day(value) is None:
            raise ValueError("日期格式不正确，应为 YYYY-MM-DD")
    if end < start:
        start, end = end, start
    return start, end


def iter_days(start: str, end: str) -> list[str]:
    first = parse_day(start)
    last = parse_day(end)
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
