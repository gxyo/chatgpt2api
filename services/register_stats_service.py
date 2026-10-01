"""注册结果的按小时账本，为注册统计页的波形图提供时间序列。

``register.json`` 里只有按域名的**累计**计数（``cloudflare_domain_stats``），``updated_at``
每次都被覆盖，因此「什么时候注册了多少」这个信息在原数据里根本不存在。这里为每次注册结果
补记一个北京时间的小时桶，专门服务波形图。

两个刻意的设计：

1. **存在旁路文件里，不写进 ``register.json``。** ``RegisterService.get()`` 的整个快照每 0.5 秒
   会经 ``/api/register/events`` SSE 全量广播，并且是靠 ``json.dumps`` 逐字节比对来决定要不要推的，
   任何新增的顶层键都会被 ``_normalize()`` 带进每次推送；历史账本还只增不减，会把推送越撑越大。
2. **账本只增不减，且不归 ``reset()`` / ``update()`` 管。** 注册统计页明确承诺
   「「重置」不会清空这里的数据」，和 ``cloudflare_domain_stats`` 是同一个口径。
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from services.stats_range import (
    GRANULARITY_DAY,
    GRANULARITY_HOUR,
    SCOPE_ALL,
    SCOPE_RANGE,
    beijing_today,
    iter_days,
    normalize_range,
)
from utils.beijing_time import beijing_now

# 落盘格式版本。格式变了就换版本号，旧文件会被忽略并按空账本重建。
SNAPSHOT_VERSION = 1
# 小时桶的保留天数。scope=all 的起点取自账本里最早的一天，所以保留期就是「全部」的地平线，
# 放宽到一年以上，让「全部」在可用体量内约等于长期（约 24 * 400 个桶，几百 KB）。
RETENTION_DAYS = 400


def _safe_count(value: object) -> int:
    """把计数收敛成非负整数，字段缺失/类型不对一律按 0。

    账本是手工可改的 JSON，``int()`` 抛的 ValueError 不该让 /api/register/stats 直接 500。
    """
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class RegisterStatsStore:
    """按小时累计注册成功/失败数，并能按区间展开成波形序列。"""

    def __init__(self, snapshot_path: Path, *, now: Callable[[], Any] = beijing_now):
        self.snapshot_path = snapshot_path
        self._now = now
        # 自己的锁，不复用 RegisterService 的：注册跑起来时 record() 会被 N 个池线程并发调用，
        # 而 .tmp 是固定文件名，写入必须串行。
        self._lock = threading.Lock()
        self._hours: dict[str, dict[str, int]] = {}
        self._last_hour = ""
        self._load()

    def _hour_key(self) -> str:
        # 形状必须是 YYYY-MM-DDTHH，和 _merge_day / summary 消费的键一致。
        return self._now().strftime("%Y-%m-%dT%H")

    def _load(self) -> None:
        """载入历史账本。读不到或格式对不上就当空账本，绝不让脏数据打断服务启动。"""
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION:
            return
        hours = snapshot.get("hours")
        if not isinstance(hours, dict):
            return
        restored: dict[str, dict[str, int]] = {}
        for key, value in hours.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            success = _safe_count(value.get("success"))
            fail = _safe_count(value.get("fail"))
            if not success and not fail:
                continue
            restored[key] = {"success": success, "fail": fail}
        self._hours = restored
        self._last_hour = self._hour_key()
        self._prune()

    def _prune(self) -> None:
        """丢弃超出保留期的小时桶。

        只在载入和小时翻转时调用，不搭每次注册的热路径——账本最多也就
        ``24 * RETENTION_DAYS`` 个键，一小时扫一次可以忽略。
        """
        cutoff_day = (self._now().date() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        # 键是补零的 YYYY-MM-DDTHH，字典序即时间序，比较前 10 位即可。
        self._hours = {key: value for key, value in self._hours.items() if key[:10] >= cutoff_day}

    def record(self, *, success: bool) -> None:
        """记一次注册结果。成功与失败分别累计，钉在北京时间的小时桶上。

        每次记录都落盘，不做节流：一份注册结果是跑完整套注册流程才拿到的，
        而这个账本不像生图统计那样有日志可以重放重建，缓冲里丢掉就是永久丢掉。
        调用方 RegisterService._record_mailbox_result 本来也会每个事件重写一次
        register.json，两次写的量级相同，且注册本身是秒级以上的操作，不构成热点。
        """
        key = self._hour_key()
        with self._lock:
            bucket = self._hours.setdefault(key, {"success": 0, "fail": 0})
            bucket["success" if success else "fail"] += 1
            if key != self._last_hour:
                self._last_hour = key
                self._prune()
            self._save_snapshot()

    def _save_snapshot(self) -> None:
        """原子落盘（临时文件 + replace），写失败只影响下次重启，不该让注册流程报错。"""
        payload = {"version": SNAPSHOT_VERSION, "hours": self._hours}
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.snapshot_path.with_suffix(f"{self.snapshot_path.suffix}.tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            temp_path.replace(self.snapshot_path)
        except OSError:
            return

    @staticmethod
    def _point(key: str, label: str, full_label: str, bucket: dict[str, int] | None) -> dict[str, Any]:
        success = bucket["success"] if bucket else 0
        fail = bucket["fail"] if bucket else 0
        return {
            "key": key,
            "label": label,
            "full_label": full_label,
            "requests": success + fail,
            "success": success,
            "failed": fail,
        }

    def _merge_day(self, day: str) -> dict[str, int]:
        merged = {"success": 0, "fail": 0}
        for hour in range(24):
            bucket = self._hours.get(f"{day}T{hour:02d}")
            if bucket is None:
                continue
            merged["success"] += bucket["success"]
            merged["fail"] += bucket["fail"]
        return merged

    def history(self, start_date: str = "", end_date: str = "", scope: str = "") -> dict[str, Any]:
        """按区间展开波形序列：单日按小时（24 点），跨天按天。口径与生图统计一致。"""
        scope = SCOPE_ALL if (scope or "").strip().lower() == SCOPE_ALL else SCOPE_RANGE

        with self._lock:
            if scope == SCOPE_ALL:
                # 账本只增不减，所以这里给的是「有记录以来的全部」。
                start = min(self._hours)[:10] if self._hours else beijing_today()
                end = beijing_today()
                if end < start:
                    start = end
            else:
                start, end = normalize_range(start_date, end_date)
            days = iter_days(start, end)
            if days:
                # 区间超上限时只统计最近的一段，返回的 range 与实际口径保持一致。
                start, end = days[0], days[-1]

            hours = self._hours
            granularity = GRANULARITY_HOUR if start == end else GRANULARITY_DAY

            series: list[dict[str, Any]] = []
            totals = {"success": 0, "fail": 0}
            if granularity == GRANULARITY_HOUR:
                for hour in range(24):
                    key = f"{start}T{hour:02d}"
                    label = f"{hour:02d}:00"
                    bucket = hours.get(key)
                    series.append(self._point(key, label, f"{start} {label}", bucket))
                    if bucket:
                        totals["success"] += bucket["success"]
                        totals["fail"] += bucket["fail"]
            else:
                for day in days:
                    bucket = self._merge_day(day)
                    series.append(self._point(day, day[5:], day, bucket))
                    totals["success"] += bucket["success"]
                    totals["fail"] += bucket["fail"]

        # 峰值按成功数取——波形图的主线就是成功数，这样徽标和图表上的峰值标记指向同一个桶。
        peak = max(series, key=lambda item: item["success"], default=None)
        if peak is not None and peak["success"] <= 0:
            peak = None

        requests = totals["success"] + totals["fail"]
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
                "failed": totals["fail"],
                "success_rate": (totals["success"] / requests) if requests else 0.0,
            },
            "series": series,
            "peak": peak,
        }
