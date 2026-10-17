from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

from services.log_service import (
    LOG_STATUS_FAILED,
    LOG_STATUS_POLICY_VIOLATION,
    LOG_STATUS_SUCCESS,
    LOG_TYPE_CALL,
    log_service,
)
from services.stats_range import (
    GRANULARITY_DAY,
    GRANULARITY_HOUR,
    MAX_SERIES_POINTS,
    SCOPE_ALL,
    SCOPE_RANGE,
    beijing_today,
    iter_days,
    normalize_range,
    parse_day,
)
from utils.beijing_time import beijing_now

# 直接调用 /v1/images/* 的请求，成功与失败都会写入日志。
IMAGE_ENDPOINT_MODES = {
    "/v1/images/generations": "generate",
    "/v1/images/edits": "edit",
}
# 网页端的生图任务只在「本地审核拦截 / 任务启动失败」时以这两个 endpoint 记日志；
# 任务真正跑完后由 image_task_service 以 /v1/images/* 记日志，因此这里只统计
# failed / policy_violation，避免同一次请求被重复计数。
IMAGE_TASK_ENDPOINT_MODES = {
    "/api/image-tasks/generations": "generate",
    "/api/image-tasks/edits": "edit",
}
MODE_LABELS = {"generate": "文生图", "edit": "图生图"}

_STATUS_SUCCESS = LOG_STATUS_SUCCESS
_STATUS_FAILED = LOG_STATUS_FAILED
_STATUS_POLICY_VIOLATION = LOG_STATUS_POLICY_VIOLATION
_STATUSES = (_STATUS_SUCCESS, _STATUS_FAILED, _STATUS_POLICY_VIOLATION)

# 聚合快照的落盘格式版本。格式变了就换版本号。
# v2 起小时桶增加 images/models；v1 快照会被加载后做一次模型数据回填，不丢请求统计。
# v3 起小时桶增加 revenue/models_revenue/legacy_success：收入在消费日志时按当时的
# 单价计入，之后调价只影响新请求，已计入的收入不会因为改价而变化。v1/v2 快照加载后
# 会按迁移这一刻的定价补一次收入。计费单位是成功返回的图片数（detail.urls 长度）。
# v4 起调用方式的桶增加 images：统计页按「成功图片数」而不是请求数展示，v1~v3 快照
# 升级时按成功请求占比把桶里的图片数分摊到各调用方式。
# v5 起对「有成功请求但没有图片数」的历史桶按 1 次成功 = 1 张图估算（日志已清理、
# 锚点丢失导致无法回填时的兜底），否则统计页会把整段历史显示成 0 张成功图片。
SNAPSHOT_VERSION = 5
CURRENCY = "CNY"
# 两次落盘之间的最小间隔：统计每次查询都会刷新，不能每次都写盘。
# 因为游标和聚合是一起写入的，崩溃时两者一起回退到上一个一致点，重放日志即可，不会重复计数。
SNAPSHOT_INTERVAL_SECONDS = 30


# 区间解析/展开已抽到 services/stats_range.py（注册统计也要用同一套口径）。
# 这里保留私有别名，本模块其余代码与既有测试无需改动。
_parse_day = parse_day
_normalize_range = normalize_range
_iter_days = iter_days


def _empty_mode_bucket() -> dict[str, int]:
    return {"requests": 0, "success": 0, "failed": 0, "policy_violation": 0, "images": 0}


def _empty_bucket() -> dict[str, Any]:
    return {
        "requests": 0,
        "success": 0,
        "failed": 0,
        "policy_violation": 0,
        "duration_ms": 0,
        # 成功请求返回的图片数：统计页主指标，也是计费单位。
        "images": 0,
        "models": {},
        # 已计收入（元）。只增不改：按请求被消费时的定价累计。
        "revenue": 0.0,
        # 能归到模型名下的收入；差额（没有模型信息的旧记录）只计入 revenue。
        "models_revenue": {},
        # 没有模型信息、按成功请求数估算收入的次数，仅用于展示口径说明。
        "legacy_success": 0,
        "modes": {mode: _empty_mode_bucket() for mode in MODE_LABELS},
    }


def _restore_bucket(value: Any) -> dict[str, Any]:
    """把快照里读回来的桶重新整成内部结构，字段缺失或类型不对的按 0 处理。"""
    source = value if isinstance(value, dict) else {}
    bucket = _empty_bucket()
    for key in ("requests", "success", "failed", "policy_violation", "duration_ms", "images"):
        try:
            bucket[key] = max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            bucket[key] = 0
    models = source.get("models")
    if isinstance(models, dict):
        for model, count in models.items():
            name = str(model or "").strip()
            if not name:
                continue
            try:
                normalized = max(0, int(count or 0))
            except (TypeError, ValueError):
                normalized = 0
            if normalized:
                bucket["models"][name] = normalized
    try:
        bucket["legacy_success"] = max(0, int(source.get("legacy_success") or 0))
    except (TypeError, ValueError):
        bucket["legacy_success"] = 0
    bucket["revenue"] = _parse_amount(source.get("revenue"))
    models_revenue = source.get("models_revenue")
    if isinstance(models_revenue, dict):
        for model, amount in models_revenue.items():
            name = str(model or "").strip()
            if not name:
                continue
            normalized_amount = _parse_amount(amount)
            if normalized_amount > 0:
                bucket["models_revenue"][name] = normalized_amount
    modes = source.get("modes")
    if isinstance(modes, dict):
        for mode, mode_value in modes.items():
            target = bucket["modes"].setdefault(str(mode), _empty_mode_bucket())
            mode_source = mode_value if isinstance(mode_value, dict) else {}
            for key in ("requests", "success", "failed", "policy_violation", "images"):
                try:
                    target[key] = max(0, int(mode_source.get(key) or 0))
                except (TypeError, ValueError):
                    target[key] = 0
    return bucket


def _accumulate(
    bucket: dict[str, Any],
    *,
    mode: str,
    status: str,
    duration_ms: int,
    model: str,
    images: int,
    revenue: float = 0.0,
) -> None:
    bucket["requests"] += 1
    bucket[status] += 1
    bucket["duration_ms"] += duration_ms
    mode_bucket = bucket["modes"].setdefault(mode, _empty_mode_bucket())
    mode_bucket["requests"] += 1
    mode_bucket[status] += 1
    if images > 0:
        bucket["images"] += images
        mode_bucket["images"] += images
        if model:
            bucket["models"][model] = bucket["models"].get(model, 0) + images
    if revenue > 0:
        bucket["revenue"] = round(bucket["revenue"] + revenue, 6)
        if model:
            current = bucket["models_revenue"].get(model, 0.0)
            bucket["models_revenue"][model] = round(current + revenue, 6)


def _parse_amount(value: object) -> float:
    """快照里的金额：非法、负数、非有限值都按 0 处理。"""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        amount = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount) or amount < 0:
        return 0.0
    return round(amount, 6)


def _parse_price(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(price) or price < 0:
        return None
    return price


def _build_model_breakdown(
    models: dict[str, int],
    models_revenue: dict[str, float],
    prices: dict[str, float],
) -> list[dict[str, Any]]:
    """合并「模型 -> 图片数」与「模型 -> 已计收入」。

    收入是消费日志时按当时单价累计的，这里不再用当前单价重算；`price` 只作为
    当前配置的参考单价返回，调价后它与收入 / 图片数不一定对得上。
    """
    by_model: list[dict[str, Any]] = []
    for model in set(models) | set(models_revenue):
        images = models.get(model, 0)
        revenue = round(models_revenue.get(model, 0.0), 2)
        if images <= 0 and revenue <= 0:
            continue
        by_model.append({
            "model": model,
            "images": images,
            "price": _parse_price(prices.get(model)),
            "revenue": revenue,
        })
    by_model.sort(key=lambda item: (-item["revenue"], -item["images"], item["model"]))
    return by_model


def _parse_duration(detail: dict[str, Any]) -> int:
    try:
        return max(0, int(detail.get("duration_ms") or 0))
    except (TypeError, ValueError):
        return 0


def _parse_images(detail: dict[str, Any], status: str) -> int:
    """成功返回的图片数按 detail.urls 计，统计页主指标与计费单位都用它。"""
    if status != _STATUS_SUCCESS:
        return 0
    urls = detail.get("urls")
    if not isinstance(urls, list):
        return 0
    return sum(1 for url in urls if isinstance(url, str) and url.strip())


def _parse_image_event(raw_line: str) -> tuple[str, str, str, int, str, int] | None:
    """把一行日志解析成 (小时键, 模式, 状态, 耗时毫秒, 模型, 图片数)，不是生图调用则返回 None。"""
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
    raw_status = str(detail.get("status") or "")
    status = raw_status if raw_status in _STATUSES else _STATUS_FAILED
    mode = IMAGE_ENDPOINT_MODES.get(endpoint)
    if mode is None:
        mode = IMAGE_TASK_ENDPOINT_MODES.get(endpoint)
        # 任务接口只在「失败 / 违反安全策略」时记日志；成功由任务服务以
        # /v1/images/* 再记一次，这里必须忽略，否则同一次请求会被数两遍。
        if mode is None or status == _STATUS_SUCCESS:
            return None

    time_text = str(item.get("time") or "")
    if len(time_text) < 13 or time_text[10] != " " or _parse_day(time_text[:10]) is None:
        return None
    hour_text = time_text[11:13]
    if not hour_text.isdigit() or int(hour_text) > 23:
        return None
    model = str(detail.get("model") or "").strip()
    return f"{time_text[:10]}T{hour_text}", mode, status, _parse_duration(detail), model, _parse_images(detail, status)


def _merge_day(hours: dict[str, dict[str, Any]], day: str) -> dict[str, Any]:
    merged = _empty_bucket()
    for hour in range(24):
        bucket = hours.get(f"{day}T{hour:02d}")
        if bucket is None:
            continue
        merged["requests"] += bucket["requests"]
        for key in _STATUSES:
            merged[key] += bucket[key]
        merged["duration_ms"] += bucket["duration_ms"]
        merged["images"] += bucket["images"]
        for model, count in bucket["models"].items():
            merged["models"][model] = merged["models"].get(model, 0) + count
        merged["revenue"] += bucket["revenue"]
        merged["legacy_success"] += bucket["legacy_success"]
        for model, amount in bucket["models_revenue"].items():
            merged["models_revenue"][model] = merged["models_revenue"].get(model, 0.0) + amount
        for mode, mode_bucket in bucket["modes"].items():
            target = merged["modes"].setdefault(mode, _empty_mode_bucket())
            for key in ("requests", "images", *_STATUSES):
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
        "policy_violation": source["policy_violation"],
        "images": source["images"],
    }


def _merge_totals(totals: dict[str, Any], bucket: dict[str, Any]) -> None:
    totals["requests"] += bucket["requests"]
    for key in _STATUSES:
        totals[key] += bucket[key]
    totals["duration_ms"] += bucket["duration_ms"]
    totals["images"] += bucket["images"]
    for model, count in bucket["models"].items():
        totals["models"][model] = totals["models"].get(model, 0) + count
    totals["revenue"] += bucket["revenue"]
    totals["legacy_success"] += bucket["legacy_success"]
    for model, amount in bucket["models_revenue"].items():
        totals["models_revenue"][model] = totals["models_revenue"].get(model, 0.0) + amount
    for mode, mode_bucket in bucket["modes"].items():
        target = totals["modes"].setdefault(mode, _empty_mode_bucket())
        for key in ("requests", "images", *_STATUSES):
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
        self._needs_model_backfill = False
        self._needs_revenue_bootstrap = False
        self._needs_image_estimate = False
        self._needs_mode_backfill = False
        self._migration_dirty = False

    def _load_snapshot(self) -> None:
        """载入上次落盘的聚合与游标。读不到或格式对不上就从零开始重建。"""
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(snapshot, dict):
            return
        version = snapshot.get("version")
        # v1 缺 images/models，加载后先从现存日志回填模型数据；
        # v1/v2 都缺收入字段，按迁移这一刻的定价补一次（之后调价不再回改）。
        # v1~v4 的调用方式桶可能缺 images，升级时按成功请求占比分摊一次；
        # 有成功请求却没有图片数的历史桶按成功次数估算图片数。
        if version not in (1, 2, 3, 4, SNAPSHOT_VERSION):
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
        self._needs_model_backfill = version == 1
        self._needs_revenue_bootstrap = version in (1, 2)
        self._needs_image_estimate = version in (1, 2, 3, 4)
        self._needs_mode_backfill = version in (1, 2, 3, 4)

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

    def _resync(self) -> bool:
        """文件被改写后重新对齐游标，已计入的聚合不回退。

        按时间清理日志只是把最老的一段前缀删掉，保留下来的尾部字节与上次读到的完全一致，
        所以靠尾部锚点就能算出新的游标位置，既不重复计数也不丢历史。
        逐条删除（中间挖洞）会让后面的字节整体前移，锚点也随之移动，同样能找回来。
        锚点确实找不到时（内容被大改）只能退到最后一个完整行：宁可漏掉少数尚未计入的行，
        也不重复计数。

        返回是否靠锚点找回游标；只有找回时，[0, offset) 才能确定仍是已计入的数据。
        """
        try:
            data = self.path.read_bytes()
        except OSError:
            return False
        if self._anchor:
            # 取最靠后的一处：万一这段字节在文件里出现过多次，靠后的那个才不会重复计数。
            found = data.rfind(self._anchor)
            if found >= 0:
                self._offset = found + len(self._anchor)
                return True
        cut = data.rfind(b"\n")
        self._offset = cut + 1 if cut >= 0 else 0
        self._anchor = data[max(0, self._offset - self._ANCHOR_BYTES) : self._offset]
        return False

    def _migrate_model_stats(self) -> None:
        """把 v1 快照缺的 images/models 从现存日志里补回来。

        只扫描快照游标之前、确定已经计入过请求计数的字节，并且只写模型字段，
        绝不重算 requests 等计数。锚点找不回时宁可不补，也不虚增收入。
        """
        if not self._needs_model_backfill:
            return
        try:
            self.path.stat()
        except FileNotFoundError:
            # 日志已经被清理干净，没有可回填的数据了。
            self._needs_model_backfill = False
            self._migration_dirty = True
            return
        except OSError:
            return
        if not self._anchor_matches() and not self._resync():
            # 内容被大改，无法确认 [0, offset) 与旧聚合的对应关系，放弃补模型数据。
            self._needs_model_backfill = False
            self._migration_dirty = True
            return
        for bucket in self._hours.values():
            bucket["images"] = 0
            bucket["models"] = {}
        try:
            with self.path.open("rb") as handle:
                while handle.tell() < self._offset:
                    raw_line = handle.readline()
                    if not raw_line or handle.tell() > self._offset:
                        break
                    event = _parse_image_event(raw_line.decode("utf-8", errors="replace"))
                    if event is None:
                        continue
                    hour_key, _mode, status, _duration_ms, model, images = event
                    bucket = self._hours.get(hour_key)
                    if bucket is None or status != _STATUS_SUCCESS or images <= 0:
                        continue
                    bucket["images"] += images
                    if model:
                        bucket["models"][model] = bucket["models"].get(model, 0) + images
        except OSError:
            # 读日志失败：保持待迁移状态，下次查询再试。
            return
        self._needs_model_backfill = False
        self._migration_dirty = True

    def _fallback_price(self, prices: dict[str, float]) -> float | None:
        """没有模型信息的旧记录用哪个单价估算：主要模型 > auto > 最低已配置价。"""
        image_totals: dict[str, int] = {}
        for bucket in self._hours.values():
            for model, images in bucket["models"].items():
                image_totals[model] = image_totals.get(model, 0) + images
        if image_totals:
            primary = max(image_totals, key=lambda model: (image_totals[model], model))
            primary_price = _parse_price(prices.get(primary))
            if primary_price is not None:
                return primary_price
        auto_price = _parse_price(prices.get("auto"))
        if auto_price is not None:
            return auto_price
        values = [
            price
            for price in (_parse_price(value) for value in prices.values())
            if price is not None
        ]
        return min(values) if values else None

    def _bootstrap_revenue(self, prices: dict[str, float]) -> None:
        """给 v1/v2 快照补一次收入，口径是迁移这一刻的单价。

        已有 images/models 的桶按模型单价折算；没有模型信息的旧记录用
        `_fallback_price` 的单价乘成功请求数。迁移落盘后版本升到 v5，之后的
        调价只影响新消费的日志，这些金额保持冻结。
        """
        if not self._needs_revenue_bootstrap:
            return
        fallback_price = self._fallback_price(prices)
        for bucket in self._hours.values():
            revenue = 0.0
            models_revenue: dict[str, float] = {}
            for model, images in bucket["models"].items():
                price = _parse_price(prices.get(model))
                if price is None or images <= 0:
                    continue
                amount = round(images * price, 6)
                if amount > 0:
                    models_revenue[model] = amount
                    revenue += amount
            # 有图片但没有模型名的部分，按回退单价补上。
            extra_images = max(0, bucket["images"] - sum(bucket["models"].values()))
            if extra_images and fallback_price is not None:
                revenue += round(extra_images * fallback_price, 6)
            # 更早的记录连 images 都没有，只能按成功请求数估算。
            legacy_success = 0
            if bucket["images"] == 0 and bucket["success"] > 0:
                legacy_success = bucket["success"]
                if fallback_price is not None:
                    revenue += round(legacy_success * fallback_price, 6)
            bucket["revenue"] = round(revenue, 6)
            bucket["models_revenue"] = models_revenue
            bucket["legacy_success"] = legacy_success
        self._needs_revenue_bootstrap = False
        self._migration_dirty = True

    def _bootstrap_legacy_images(self) -> None:
        """给「有成功请求但没有图片数」的历史桶补一个下限：1 次成功至少 1 张图。

        旧快照的请求/成功计数是可靠的，但日志被清理或锚点丢失时无法从日志回填
        `images`，这些桶就会显示成 0 张成功图片。用 success 作为图片数下限把成功
        历史还原出来；真实多图请求会略少算，但不会虚增。收入已经单独冻结，这里不动。
        """
        if not self._needs_image_estimate:
            return
        for bucket in self._hours.values():
            if bucket["images"] == 0 and bucket["success"] > 0:
                bucket["images"] = bucket["success"]
                self._migration_dirty = True
        self._needs_image_estimate = False

    def _bootstrap_mode_images(self) -> None:
        """给 v1~v4 快照的调用方式桶补上成功图片数。

        旧快照只按调用方式记了请求数，没有图片数；用同一小时桶里的成功请求占比
        把 `images` 分摊到各方式，总量与桶里的图片数严格一致。新快照已经有这个
        字段，不需要再分。
        """
        if not self._needs_mode_backfill:
            return
        for bucket in self._hours.values():
            total_images = bucket["images"]
            modes = bucket["modes"]
            if total_images <= 0 or not modes:
                continue
            if any(mode.get("images", 0) > 0 for mode in modes.values()):
                continue
            weighted = [
                (mode_key, mode_bucket.get("success", 0))
                for mode_key, mode_bucket in modes.items()
                if mode_bucket.get("success", 0) > 0
            ]
            if not weighted:
                weighted = [
                    (mode_key, mode_bucket.get("requests", 0))
                    for mode_key, mode_bucket in modes.items()
                    if mode_bucket.get("requests", 0) > 0
                ]
            weight_total = sum(weight for _, weight in weighted)
            if weight_total <= 0:
                continue
            ranked = sorted(weighted, key=lambda item: (-item[1], item[0]))
            remaining = total_images
            for mode_key, weight in ranked:
                if remaining <= 0:
                    break
                allocated = min(remaining, int(round(total_images * weight / weight_total)))
                if allocated <= 0:
                    continue
                modes[mode_key]["images"] = modes[mode_key].get("images", 0) + allocated
                remaining -= allocated
            # 四舍五入剩下的零头给权重最大的方式，保证各方式之和等于桶内图片数。
            if remaining > 0:
                modes[ranked[0][0]]["images"] = modes[ranked[0][0]].get("images", 0) + remaining
        self._needs_mode_backfill = False
        self._migration_dirty = True

    def _migrate_snapshots(self, prices: dict[str, float]) -> None:
        """迁移持锁执行：补模型数据、收入与调用方式图片数，最后一次性落盘。"""
        self._migrate_model_stats()
        if self._needs_model_backfill:
            # 模型回填没完成（读日志失败），收入也等下一轮，避免按错误的模型数据计费。
            return
        self._bootstrap_revenue(prices)
        self._bootstrap_legacy_images()
        self._bootstrap_mode_images()
        if self._migration_dirty:
            self._migration_dirty = False
            self._save_snapshot(force=True)

    def _refresh(self, prices: dict[str, float]) -> None:
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
                self._consume(consumed.decode("utf-8", errors="replace"), prices)
                self._offset += len(consumed)
                self._anchor = (self._anchor + consumed)[-self._ANCHOR_BYTES :]
        # 游标动过就必须落盘，否则重启后会退回旧游标，把已经计入的行再数一遍。
        self._save_snapshot(force=resynced)

    def _consume(self, text: str, prices: dict[str, float]) -> None:
        for raw_line in text.splitlines():
            event = _parse_image_event(raw_line)
            if event is None:
                continue
            hour_key, mode, status, duration_ms, model, images = event
            # 计费发生在消费日志的这一刻：以后调价不会回头改已计入的金额。
            # 按成功返回的图片数计费；失败与安全策略不收费。
            price = _parse_price(prices.get(model)) if model else None
            revenue = round(images * price, 6) if price is not None and images > 0 else 0.0
            bucket = self._hours.setdefault(hour_key, _empty_bucket())
            _accumulate(
                bucket,
                mode=mode,
                status=status,
                duration_ms=duration_ms,
                model=model,
                images=images,
                revenue=revenue,
            )

    def summary(
        self,
        start_date: str = "",
        end_date: str = "",
        scope: str = "",
        pricing: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        scope = SCOPE_ALL if (scope or "").strip().lower() == SCOPE_ALL else SCOPE_RANGE
        prices = pricing or {}

        # 汇总全程持锁：既避免并发查询各自重扫一遍，也避免读到正被 _consume 改写的桶。
        # scope=all 的起点要读聚合，所以区间归一化也一并放进锁里。
        with self._lock:
            # 快照迁移必须在消费新日志之前完成：回填只扫描尚未被 _refresh 推进的游标区间。
            # 新日志也在这里按当前定价计费，之后调价不影响已计入的收入。
            self._migrate_snapshots(prices)
            self._refresh(prices)
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

            # 累计口径：不受查询区间影响，始终是聚合里全部小时桶的和。
            lifetime_images = 0
            lifetime_revenue = 0.0
            for bucket in hours.values():
                lifetime_images += bucket["images"]
                lifetime_revenue += bucket["revenue"]

        # 统计页主线是成功图片数，峰值也按图片算；没有图片记录时退到请求数。
        peak = max(series, key=lambda item: item["images"], default=None)
        if peak is not None and peak["images"] <= 0:
            peak = max(series, key=lambda item: item["requests"], default=None)
            if peak is not None and peak["requests"] <= 0:
                peak = None

        requests = totals["requests"]
        # 成功率按「成功图片 + 失败」算：违反安全策略是上游拒绝生成，不是我们这边
        # 的失败，混进分母会把成功率无端拉低。更早的记录没有图片数时退回成功请求数。
        rate_success = totals["images"] if totals["images"] > 0 else totals["success"]
        rate_base = rate_success + totals["failed"]
        by_mode = [
            {
                "mode": mode,
                "label": label,
                "requests": totals["modes"][mode]["requests"],
                "success": totals["modes"][mode]["success"],
                "failed": totals["modes"][mode]["failed"],
                "policy_violation": totals["modes"][mode]["policy_violation"],
                "images": totals["modes"][mode]["images"],
            }
            for mode, label in MODE_LABELS.items()
        ]
        by_model = _build_model_breakdown(totals["models"], totals["models_revenue"], prices)
        revenue = round(totals["revenue"], 2)
        # 没有模型归属的收入（历史请求缺模型信息）：明细表里对不上，
        # 单独给前端做口径说明，合计仍以这里的总数为准。
        legacy_revenue = round(max(0.0, totals["revenue"] - sum(totals["models_revenue"].values())), 2)

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
                "policy_violation": totals["policy_violation"],
                "success_rate": (rate_success / rate_base) if rate_base else 0.0,
                "avg_duration_ms": int(totals["duration_ms"] / requests) if requests else 0,
                "images": totals["images"],
                "revenue": revenue,
                # 没有模型归属的历史收入，按回退单价估算，单独给前端做口径说明。
                "legacy_revenue": legacy_revenue,
                "legacy_success": totals["legacy_success"],
            },
            # 总收入：全部记录的和，不随日期 / 区间筛选变化。
            "lifetime": {
                "images": lifetime_images,
                "revenue": round(lifetime_revenue, 2),
            },
            "currency": CURRENCY,
            "by_mode": by_mode,
            "by_model": by_model,
            "series": series,
            "peak": peak,
        }


image_stats_service = ImageStatsService(log_service.path)


def build_image_stats(start_date: str = "", end_date: str = "", scope: str = "") -> dict[str, Any]:
    # 延迟导入：统计模块的解析逻辑要能在没有配置（甚至没有 auth-key）的测试里独立使用。
    from services.config import config

    return image_stats_service.summary(start_date, end_date, scope, pricing=config.model_pricing)


__all__ = [
    "SCOPE_ALL",
    "SCOPE_RANGE",
    "ImageStatsService",
    "beijing_now",
    "beijing_today",
    "build_image_stats",
    "image_stats_service",
]
