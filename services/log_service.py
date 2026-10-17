from __future__ import annotations

import hashlib
import json
import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from services.config import DATA_DIR, config
from services.protocol.error_response import anthropic_error_response, openai_error_response
from utils.beijing_time import beijing_now_text, beijing_text_from_timestamp
from utils.helper import anthropic_sse_stream, describe_exception, public_error_message, sse_json_stream

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
# 注册机失败日志：只记失败（成功的注册没有排查价值），单独一个日志页看，
# 走的是同一份 logs.jsonl 与同一套筛选/导出/清理，新增一种类型即可。
LOG_TYPE_REGISTER = "register"

# 调用结果（detail.status）。「违反安全策略」既不是成功也不是失败：请求本身
# 被上游内容审核拦下了，单独一类，免得混进失败率里污染排障口径。
LOG_STATUS_SUCCESS = "success"
LOG_STATUS_FAILED = "failed"
LOG_STATUS_POLICY_VIOLATION = "policy_violation"
# ImageGenerationError.code：conversation 把上游内容审核拦截统一标成这个 code。
POLICY_VIOLATION_CODE = "content_policy_violation"
INTERNAL_RESPONSE_KEYS = {
    "_account_email", "_conversation_id", "_account_trail", "_attempts_summary", "_postprocess",
    "_image_timeline", "_upstream_model_slug",
}
# 一键导出：默认最近 10 条，条数上界防止一次导出把浏览器拖死。
LOG_EXPORT_DEFAULT_LIMIT = 10
LOG_EXPORT_MAX_LIMIT = 500
_LOG_EXPORT_SEPARATOR = "=" * 78


class LogService:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _legacy_id(raw_line: str, line_number: int) -> str:
        payload = f"{line_number}:{raw_line}".encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()[:24]

    def _parse_line(self, raw_line: str, line_number: int) -> dict[str, Any] | None:
        try:
            item = json.loads(raw_line)
        except Exception:
            return None
        if not isinstance(item, dict):
            return None
        parsed = dict(item)
        parsed["id"] = str(parsed.get("id") or self._legacy_id(raw_line, line_number))
        return parsed

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _matches_filters(
        item: dict[str, Any], *, type: str = "", status: str = "", start_date: str = "", end_date: str = "",
    ) -> bool:
        t = str(item.get("time") or "")
        day = t[:10]
        if type and item.get("type") != type:
            return False
        if status and str((item.get("detail") or {}).get("status") or "") != status:
            return False
        if start_date and day < start_date:
            return False
        if end_date and day > end_date:
            return False
        return True

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        item = {
            "id": uuid4().hex,
            "time": beijing_now_text(),
            "type": type,
            "summary": summary,
            "detail": detail or data,
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(self._serialize_item(item) + "\n")

    def list(self, type: str = "", status: str = "", start_date: str = "", end_date: str = "",
             limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for line_number in range(len(lines) - 1, -1, -1):
            item = self._parse_line(lines[line_number], line_number)
            if item is None:
                continue
            if not self._matches_filters(item, type=type, status=status, start_date=start_date, end_date=end_date):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _log_day(item: dict[str, Any]) -> str:
        day = str(item.get("time") or "")[:10]
        return day if len(day) == 10 else ""

    def build_export(self, type: str = "", status: str = "", start_date: str = "", end_date: str = "",
                     limit: int = LOG_EXPORT_DEFAULT_LIMIT) -> tuple[str, int]:
        """把最近 N 条日志排版成一份可以直接复制粘贴的纯文本，返回 (文本, 条数)。

        「完整报文」就是落盘的那条记录本身，只排版不裁剪：上游状态码、原始响应体、
        请求摘要、trace id 全在 detail 里，日志页详情弹窗显示不下的内容这里一个不落。
        顺序按时间由旧到新，方便顺着时间线看一次重试/抢救的完整过程。
        """
        count = self._normalize_export_limit(limit)
        items = self.list(type=type, status=status, start_date=start_date, end_date=end_date, limit=count)
        items.reverse()
        condition = (
            f"类型={type or '全部'}，状态={status or '全部'}，"
            f"日期={f'{start_date} ~ {end_date}' if (start_date or end_date) else '不限'}"
        )
        return self._render_export(items, condition, f"最多 {count} 条，按时间由旧到新排列")

    def build_export_by_ids(self, ids: list[str]) -> tuple[str, int]:
        """只导出勾选的那几条日志，格式和「导出最近 N 条」完全一致。

        勾选来自当前列表页的筛选结果，所以这里不再按类型/状态过滤，只认 id。
        文件本身按时间追加，按行序收集就是由旧到新。
        """
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not target_ids or not self.path.exists():
            return self._render_export([], "勾选的日志", "按时间由旧到新排列")
        items: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            item = self._parse_line(raw_line, line_number)
            if item is None:
                continue
            if str(item.get("id") or "") in target_ids:
                items.append(item)
        return self._render_export(items, "勾选的日志", "按时间由旧到新排列")

    def _render_export(self, items: list[dict[str, Any]], condition: str, note: str) -> tuple[str, int]:
        lines = [
            "ChatGPT2API 日志导出",
            f"导出时间：{beijing_now_text()}",
            f"筛选条件：{condition}",
            f"条数：{len(items)} 条（{note}）",
        ]
        if not items:
            lines.append("没有符合条件的日志。")
            return "\n".join(lines) + "\n", 0
        for index, item in enumerate(items, start=1):
            lines.extend([
                "",
                _LOG_EXPORT_SEPARATOR,
                f"[{index}/{len(items)}] {self._export_headline(item)}",
                _LOG_EXPORT_SEPARATOR,
                json.dumps(item, ensure_ascii=False, indent=2),
            ])
        return "\n".join(lines) + "\n", len(items)

    @staticmethod
    def _normalize_export_limit(limit: object) -> int:
        try:
            count = int(limit)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return LOG_EXPORT_DEFAULT_LIMIT
        return max(1, min(count, LOG_EXPORT_MAX_LIMIT))

    @staticmethod
    def _export_headline(item: dict[str, Any]) -> str:
        """每条日志顶部的一行摘要，方便在文件里快速翻到出问题的那条。"""
        detail = item.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        parts = [str(item.get("time") or "").strip()]
        summary = str(item.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        parts.append(f"type={item.get('type') or ''}")
        status = str(detail.get("status") or "").strip()
        if status:
            parts.append(f"status={status}")
        duration = detail.get("duration_ms")
        if isinstance(duration, int):
            parts.append(f"耗时={duration}ms")
        for key in ("size", "n", "quality", "account_email", "email", "mail_domain", "image_trace",
                    "conversation_id", "endpoint", "stage"):
            value = str(detail.get(key) or "").strip()
            if value:
                parts.append(f"{key}={value}")
        return " | ".join(part for part in parts if part)

    def cleanup_before(self, cutoff_day: str, include_today_before: str = "") -> dict[str, int]:
        """删除 cutoff_day 之前（不含当天）的日志。

        include_today_before 传入 ``YYYY-MM-DD HH:MM:SS`` 时，还会删除当天该时刻及之前
        写入的日志（用于「清理当天日志」开关）；留空则当天日志一律保留。

        保留下来的行按原样写回，不做重新序列化——统计服务靠比对已读过的尾部字节来推进
        增量游标，字节变了就得重找位置。日志按时间追加，要删的都是最老的一段前缀，
        因此逐行判断即可，不需要排序。
        """
        if not self.path.exists():
            return {"removed": 0, "kept": 0}
        today = include_today_before[:10] if include_today_before else ""
        kept_lines: list[str] = []
        removed = 0
        for line_number, raw_line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            item = self._parse_line(raw_line, line_number)
            day = self._log_day(item) if item is not None else ""
            time_text = str(item.get("time") or "") if item is not None else ""
            # 解析不出来的行一律留着：宁可少删几条，也不要把读不懂的内容丢掉。
            remove = bool(day) and (
                day < cutoff_day
                or (bool(today) and day == today and time_text <= include_today_before)
            )
            if remove:
                removed += 1
                continue
            kept_lines.append(raw_line)
        if removed:
            content = "\n".join(kept_lines)
            if content:
                content += "\n"
            self.path.write_text(content, encoding="utf-8")
        return {"removed": removed, "kept": len(kept_lines)}

    def delete(self, ids: list[str]) -> dict[str, int]:
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not self.path.exists() or not target_ids:
            return {"removed": 0}
        lines = self.path.read_text(encoding="utf-8").splitlines()
        kept_lines: list[str] = []
        removed = 0
        for line_number, raw_line in enumerate(lines):
            item = self._parse_line(raw_line, line_number)
            if item is None:
                kept_lines.append(raw_line)
                continue
            if str(item.get("id") or "") in target_ids:
                removed += 1
                continue
            kept_lines.append(self._serialize_item(item))
        content = "\n".join(kept_lines)
        if content:
            content += "\n"
        self.path.write_text(content, encoding="utf-8")
        return {"removed": removed}


log_service = LogService(DATA_DIR / "logs.jsonl")


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key == "urls" and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    return urls


def _collect_account_emails(value: object) -> list[str]:
    emails: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"_account_email", "account_email"} and isinstance(item, str) and item.strip():
                emails.append(item.strip())
            else:
                emails.extend(_collect_account_emails(item))
    elif isinstance(value, list):
        for item in value:
            emails.extend(_collect_account_emails(item))
    return emails


def _collect_conversation_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_conversation_id" and isinstance(item, str) and item.strip():
                ids.append(item.strip())
            else:
                ids.extend(_collect_conversation_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(_collect_conversation_ids(item))
    return ids


def _collect_account_trails(value: object) -> list[list[str]]:
    """收集结果里的换号轨迹（一次图片生成一份，按尝试顺序排列）。"""
    trails: list[list[str]] = []
    if isinstance(value, dict):
        raw = value.get("_account_trail")
        if isinstance(raw, list):
            trail = [str(item).strip() for item in raw if str(item or "").strip()]
            if trail:
                trails.append(trail)
        for key, item in value.items():
            if key != "_account_trail":
                trails.extend(_collect_account_trails(item))
    elif isinstance(value, list):
        for item in value:
            trails.extend(_collect_account_trails(item))
    return trails


def _collect_attempts_summaries(value: object) -> list[str]:
    """收集结果里的一行尝试摘要（`#1 a message_retry → #2 b success`）。"""
    summaries: list[str] = []
    if isinstance(value, dict):
        raw = value.get("_attempts_summary")
        if isinstance(raw, str) and raw.strip():
            summaries.append(raw.strip())
        for key, item in value.items():
            if key != "_attempts_summary":
                summaries.extend(_collect_attempts_summaries(item))
    elif isinstance(value, list):
        for item in value:
            summaries.extend(_collect_attempts_summaries(item))
    return summaries


def _collect_postprocess(value: object) -> list[dict[str, Any]]:
    """收集结果里的本地后处理数据（4K 超分耗时、排队、失败原因）。"""
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        raw = value.get("_postprocess")
        if isinstance(raw, dict) and raw:
            items.append(raw)
        for key, item in value.items():
            if key != "_postprocess":
                items.extend(_collect_postprocess(item))
    elif isinstance(value, list):
        for item in value:
            items.extend(_collect_postprocess(item))
    return items


def _collect_image_timelines(value: object) -> list[dict[str, Any]]:
    """收集结果里的执行时间线（`_image_timeline`，每张图一份）。"""
    groups: list[dict[str, Any]] = []
    if isinstance(value, dict):
        raw = value.get("_image_timeline")
        if isinstance(raw, list):
            groups.extend(
                item for item in raw
                if isinstance(item, dict) and isinstance(item.get("steps"), list) and item["steps"]
            )
        for key, item in value.items():
            if key != "_image_timeline":
                groups.extend(_collect_image_timelines(item))
    elif isinstance(value, list):
        for item in value:
            groups.extend(_collect_image_timelines(item))
    return groups


def _diagnostics_image_timeline(diagnostics: dict[str, Any]) -> dict[str, Any] | None:
    steps = diagnostics.get("timeline")
    if not isinstance(steps, list) or not steps:
        return None
    return {
        "index": diagnostics.get("index") or 1,
        "steps": [step for step in steps if isinstance(step, dict)],
    }


def add_image_timeline_detail(
    detail: dict[str, Any],
    *,
    result: object = None,
    exc: BaseException | None = None,
    image_timelines: list[dict[str, Any]] | None = None,
) -> None:
    """把「这次生图每一步做了什么、花了多久、为什么」写进同一条调用日志。

    成功时时间线挂在结果的 `_image_timeline` 上（流式是每个 chunk 都带一份），
    失败时随异常的 image_diagnostics.timeline 出去。多张图并行时每张一份，
    按图片序号去重后存成数组，日志详情页逐条展示。
    """
    groups = list(image_timelines or [])
    groups.extend(_collect_image_timelines(result))
    if exc is not None:
        diagnostics = _exception_image_diagnostics(exc)
        if diagnostics is not None:
            group = _diagnostics_image_timeline(diagnostics)
            if group is not None:
                groups.append(group)
        # 多图并行全部失败时，外层异常上挂着每一张图的完整时间线。
        groups.extend(_exception_image_timeline_groups(exc))
    if not groups:
        return
    by_index: dict[int, dict[str, Any]] = {}
    for group in groups:
        index = group.get("index") or 1
        steps = [step for step in (group.get("steps") or []) if isinstance(step, dict)]
        if not steps:
            continue
        # 每个 chunk 都带一份完整时间线：同一张图保留步数最多的那份（最完整）。
        current = by_index.get(index)
        if current is None or len(steps) > len(current["steps"]):
            by_index[index] = {
                "index": index,
                **({"total": group["total"]} if group.get("total") else {}),
                "steps": steps,
            }
    if by_index:
        detail["image_timeline"] = [by_index[key] for key in sorted(by_index)]


def add_postprocess_detail(
    detail: dict[str, Any],
    *,
    result: object = None,
    postprocess: list[dict[str, Any]] | None = None,
) -> None:
    """把超分耗时和失败原因平铺进调用日志，页面上直接能看到卡在哪。

    超分失败不影响请求结果（会回退成原图），所以这里不是 error，只是一组
    诊断字段：upscale_ms 很大说明 4K 放大吃掉了请求时间，upscale_error 有值
    说明是本地放大失败、返回的是 1K 原图。
    """
    merged: dict[str, Any] = {}
    for item in [*(postprocess or []), *_collect_postprocess(result)]:
        for key in ("upscale_ms", "upscale_queue_wait_ms"):
            value = item.get(key)
            if value:
                merged[key] = int(merged.get(key) or 0) + int(value)
        for key in ("upscale_target", "upscale_from_size", "upscale_to_size", "upscale_error"):
            value = item.get(key)
            if value and not merged.get(key):
                merged[key] = value
        if item.get("upscale_to_size") or item.get("upscale_error"):
            merged["upscale_images"] = int(merged.get("upscale_images") or 0) + 1
    detail.update(merged)


def add_upstream_model_detail(
    detail: dict[str, Any],
    *,
    result: object = None,
    upstream_model_slug: str = "",
) -> None:
    """记录发往上游的模型 slug，以及上游 SSE 回报的实际调度模型。

    `/backend-api/models` 只是模型选择器清单，不在清单里的旧 slug 仍可能被
    后端路由，所以「请求了什么」和「上游回报了什么」分开记，便于对账。
    """
    actual = (upstream_model_slug or "").strip()
    if not actual and isinstance(result, dict):
        actual = str(result.get("_upstream_model_slug") or "").strip()
    if not actual:
        return
    detail["upstream_model_slug"] = actual
    requested = str(config.default_upstream_model_name or "").strip()
    if requested:
        detail["upstream_model_requested"] = requested


def _flatten_account_trail(trails: list[list[str]]) -> list[str]:
    """把多份轨迹拼成一条账号顺序。

    每个流式 chunk 都会带一份完整的轨迹，多图并行时不同图片的换号顺序还可能
    不同（A→B 和 B→A）。以前只跳过「与上一份完全相同」的副本，副本一旦轮转
    （A→B、B→A、A→B…）就会拼成 A→B→A→B…——日志上看着像换了二十个号，
    实际只有两个。

    现在只保留每个账号第一次出现的位置：重复/轮转的副本（A→B、B→A、A→B…）
    和同一份轨迹内部绕回来的（A→B→A，多图 chunk 拼出来的）都只留第一圈，
    免得日志里冒出几十个假账号。
    """
    flat: list[str] = []
    seen: set[str] = set()
    for trail in trails:
        for email in trail:
            if not email or email in seen:
                continue
            seen.add(email)
            flat.append(email)
    return flat


def _exception_image_timeline_groups(exc: BaseException) -> list[dict[str, Any]]:
    """沿异常链收集多图并行失败时挂上的逐图时间线（image_timeline_groups）。"""
    groups: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        raw = getattr(current, "image_timeline_groups", None)
        if isinstance(raw, list):
            groups.extend(
                item for item in raw
                if isinstance(item, dict) and isinstance(item.get("steps"), list) and item["steps"]
            )
        current = current.__cause__ or current.__context__
    return groups


def _exception_image_diagnostics(exc: BaseException) -> dict[str, Any] | None:
    """沿异常链找第一份生图诊断。

    多图并行全部失败时，最外层是重新包装过的 ImageGenerationError，
    真正的 attempts 在 __cause__ 上的那条异常里。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostics = getattr(current, "image_diagnostics", None)
        if isinstance(diagnostics, dict) and diagnostics:
            return diagnostics
        current = current.__cause__ or current.__context__
    return None


def add_attempt_trail_detail(
    detail: dict[str, Any],
    *,
    result: object = None,
    exc: BaseException | None = None,
    account_trails: list[list[str]] | None = None,
    attempts_summaries: list[str] | None = None,
) -> None:
    """把「这次请求先后用过哪些账号」写进同一条调用日志。

    只在真的重试过时才有内容：没重试的请求只有一个账号，detail.account_email
    已经写清楚了，再列一遍没有意义。成功和失败都走这里，日志里始终只有一条记录，
    最后结果由 detail.status 表示。
    """
    trails = list(account_trails or [])
    trails.extend(_collect_account_trails(result))
    summaries = list(attempts_summaries or [])
    summaries.extend(_collect_attempts_summaries(result))
    if exc is not None:
        diagnostics = _exception_image_diagnostics(exc)
        if diagnostics is not None:
            attempts = [item for item in (diagnostics.get("attempts") or []) if isinstance(item, dict)]
            if len(attempts) > 1:
                trail = [str(item.get("account_email") or "").strip() for item in attempts]
                trail = [email for email in trail if email]
                if trail:
                    trails.append(trail)
                summary = str(diagnostics.get("attempts_summary") or "").strip()
                if summary:
                    summaries.append(summary)
    trail = _flatten_account_trail(trails)
    if trail:
        # 存成一行纯文本：日志详情弹窗只渲染标量字段，列表会掉进原始 JSON 里看不见。
        detail["account_trail"] = " → ".join(trail)
    summary_text = " | ".join(dict.fromkeys(item for item in summaries if item))
    if summary_text:
        detail["attempts_summary"] = summary_text


def _strip_internal_response_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_response_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_response_fields(item) for item in value]
    return value


def merge_extra_detail(detail: dict[str, Any], extra_detail: dict[str, Any] | None) -> None:
    """把请求侧入参平铺进 detail。

    平铺而不是嵌成子对象：日志页详情弹窗只渲染标量字段，嵌套了就看不到。
    空值跳过，免得日志里一堆 null。
    """
    for key, value in (extra_detail or {}).items():
        if value is None or value == "":
            continue
        detail.setdefault(key, value)


def _image_entries_bytes(entries: object) -> tuple[int, int]:
    """数一下参考图/蒙版有几张、一共多少字节。

    只数个数和体积，图片内容本身绝不进日志：单张就可能几 MB，落盘会把
    logs.jsonl 撑爆，而且 base64 里也可能带着用户隐私。
    """
    count = 0
    total = 0
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            data: object = entry
            # 图片元组 (bytes, filename, mime) 和裸 bytes 两种形态都支持。
            if isinstance(entry, (list, tuple)) and entry:
                data = entry[0]
            if isinstance(data, (bytes, bytearray)):
                count += 1
                total += len(data)
    return count, total


def image_input_detail(payload: dict[str, Any]) -> dict[str, Any]:
    """图生图的输入图片信息：几张参考图、多大，有没有蒙版。

    失败率高时最想确认的一件事：是不是客户端传了几 MB 的大图，上传阶段就把
    请求预算吃光了，轮到轮询时已经没时间了。
    """
    detail: dict[str, Any] = {}
    count, total = _image_entries_bytes(payload.get("images"))
    if count:
        detail["input_images"] = count
        detail["input_images_bytes"] = total
    mask_count, mask_total = _image_entries_bytes(payload.get("mask"))
    if mask_count:
        detail["input_masks"] = mask_count
        detail["input_masks_bytes"] = mask_total
    return detail


def image_request_detail(payload: dict[str, Any]) -> dict[str, Any]:
    """生图请求的入参，落进调用日志，方便事后核对客户端到底要了多大的图。

    两条生图路径（/v1/images/* 和 /api/image-tasks/*）共用，免得日志字段对不齐。
    /v1/images/edits 路由在读取参考图之前就建好了日志对象，那里还要再补一次
    image_input_detail（参考图是读完之后才有字节数可数的）。
    """
    detail = {
        "size": payload.get("size"),
        "n": payload.get("n"),
        "quality": payload.get("quality"),
        "response_format": payload.get("response_format"),
    }
    detail.update(image_input_detail(payload))
    return detail


def _request_excerpt(text: object, limit: int = 1000) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _image_error_response(exc: Exception) -> JSONResponse:
    from services.protocol.conversation import public_image_error_message
    from utils.helper import image_quota_error_text, is_image_quota_error_in_chain

    # 额度判断必须沿异常链按原始报错来：文案可以由设置项替换成任意文字，一旦替换完
    # 就认不出来了，状态码和 error.code 得继续按「额度不足」返回。
    raw_message = public_error_message(exc)
    if is_image_quota_error_in_chain(exc):
        return openai_error_response(
            {
                "error": {
                    "message": image_quota_error_text(),
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
            429,
        )
    message = public_image_error_message(raw_message)
    if hasattr(exc, "to_openai_error") and hasattr(exc, "status_code"):
        return JSONResponse(status_code=int(exc.status_code), content=exc.to_openai_error())
    return openai_error_response(message, 502)


def _protocol_error_response(exc: Exception, status_code: int, sse: str) -> JSONResponse:
    message = public_error_message(exc)
    if sse == "anthropic":
        return anthropic_error_response(message, status_code)
    return openai_error_response(message, status_code)


def _next_item(items):
    try:
        return True, next(items)
    except StopIteration:
        return False, None


def is_policy_violation_error(exc: BaseException | None) -> bool:
    """沿异常链判断这次调用是否被内容安全策略拦下。

    上游拦截会被 conversation 包装成 ImageGenerationError(code=content_policy_violation)；
    多图并行时外层还可能再包一层，所以必须顺着 __cause__/__context__ 找。
    """
    from services.openai_backend_api import ImageContentPolicyError, is_content_policy_error

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImageContentPolicyError):
            return True
        # conversation 会把「上游返回了一段文本而不是图片」统一标成
        # content_policy_violation，其中也包括普通的「本次生图失败」；只有
        # 文案本身像内容审核拒绝时才归到这一类，别把普通失败也算进来。
        if (
            str(getattr(current, "code", "") or "").strip() == POLICY_VIOLATION_CODE
            and is_content_policy_error(str(current))
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None
    # 请求侧的入参（图片尺寸、张数、质量等），落日志时平铺进 detail。
    extra_detail: dict[str, Any] | None = None

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        try:
            result = await run_in_threadpool(handler, *args)
        except ImageGenerationError as exc:
            self.log_failure(exc, account_email=getattr(exc, "account_email", ""),
                             conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log_failure(exc, error=str(exc.detail))
            raise
        except Exception as exc:
            self.log_failure(exc, account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            # 轨迹类字段只给日志看，不能回给客户端。
            response = {
                key: value for key, value in result.items()
                if key not in INTERNAL_RESPONSE_KEYS
            }
            return response

        sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        try:
            has_first, first = await run_in_threadpool(_next_item, result)
        except ImageGenerationError as exc:
            self.log_failure(exc, account_email=getattr(exc, "account_email", ""),
                             conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log_failure(exc, error=str(exc.detail))
            raise
        except Exception as exc:
            self.log_failure(exc, account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)
        if not has_first:
            self.log("流式调用结束")
            return StreamingResponse(sender(()), media_type="text/event-stream")
        return StreamingResponse(sender(self.stream(itertools.chain([first], result))), media_type="text/event-stream")

    def stream(self, items):
        urls: list[str] = []
        account_emails: list[str] = []
        conversation_ids: list[str] = []
        account_trails: list[list[str]] = []
        attempts_summaries: list[str] = []
        image_timelines: list[dict[str, Any]] = []
        postprocess: list[dict[str, Any]] = []
        upstream_model_slug = ""
        failed = False
        try:
            for item in items:
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                account_trails.extend(_collect_account_trails(item))
                attempts_summaries.extend(_collect_attempts_summaries(item))
                image_timelines.extend(_collect_image_timelines(item))
                postprocess.extend(_collect_postprocess(item))
                if isinstance(item, dict):
                    upstream_model_slug = upstream_model_slug or str(
                        item.get("_upstream_model_slug") or ""
                    ).strip()
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log_failure(
                exc,
                summary="流式调用失败",
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
                account_trails=account_trails,
                attempts_summaries=attempts_summaries,
                image_timelines=image_timelines,
                postprocess=postprocess,
                upstream_model_slug=upstream_model_slug,
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "",
                         account_trails=account_trails, attempts_summaries=attempts_summaries,
                         image_timelines=image_timelines, postprocess=postprocess,
                         upstream_model_slug=upstream_model_slug)

    def log_failure(self, exc: BaseException, summary: str = "调用失败", error: str = "",
                    **kwargs: Any) -> None:
        """记一条失败日志；内容策略拦截单独记成「违反安全策略」。"""
        violation = is_policy_violation_error(exc)
        self.log(
            "违反安全策略" if violation else summary,
            status=LOG_STATUS_POLICY_VIOLATION if violation else LOG_STATUS_FAILED,
            error=error or str(exc),
            exc=exc,
            **kwargs,
        )

    def log(self, suffix: str, result: object = None, status: str = LOG_STATUS_SUCCESS, error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "",
            exc: BaseException | None = None, account_trails: list[list[str]] | None = None,
            attempts_summaries: list[str] | None = None,
            image_timelines: list[dict[str, Any]] | None = None,
            postprocess: list[dict[str, Any]] | None = None,
            upstream_model_slug: str = "") -> None:
        detail = {
            "key_id": self.identity.get("id"),
            "key_name": self.identity.get("name"),
            "role": self.identity.get("role"),
            "endpoint": self.endpoint,
            "model": self.model,
            "started_at": beijing_text_from_timestamp(self.started),
            "ended_at": beijing_now_text(),
            "duration_ms": int((time.time() - self.started) * 1000),
            "status": status,
        }
        request_excerpt = _request_excerpt(self.request_text)
        if request_excerpt:
            detail["request_text"] = request_excerpt
        if self.request_shape:
            detail["request_shape"] = self.request_shape
        merge_extra_detail(detail, self.extra_detail)
        if error:
            detail["error"] = error
        if exc is not None:
            # error 是给用户看的文案，上游的状态码和响应体在异常链里，单独记一份。
            try:
                detail["upstream_error"] = describe_exception(exc)
            except Exception:
                # 记日志本身永远不能影响请求处理。
                pass
            # 图片链路的关联 id：容器日志里同样的 trace_id 能直接对上这一条记录。
            image_trace = str(getattr(exc, "image_trace", "") or "").strip()
            if image_trace:
                detail["image_trace"] = image_trace
            # 生图失败时的现场诊断（同账号并发数、握手排队情况等），
            # 直接从日志页导出就能看到，不必再去翻容器日志。
            image_diagnostics = getattr(exc, "image_diagnostics", None)
            if isinstance(image_diagnostics, dict) and image_diagnostics:
                detail["image_diagnostics"] = image_diagnostics
        email = str(account_email or "").strip()
        if not email:
            emails = _collect_account_emails(result)
            email = emails[0] if emails else ""
        if email:
            detail["account_email"] = email
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_ids = _collect_conversation_ids(result)
            conv_id = conv_ids[0] if conv_ids else ""
        if conv_id:
            detail["conversation_id"] = conv_id
        collected_urls = [*(urls or []), *_collect_urls(result)]
        if collected_urls and not self.endpoint.startswith("/v1/search"):
            detail["urls"] = list(dict.fromkeys(collected_urls))
        # 换号重试的轨迹（成功/失败都写），保证一次调用始终只对应一条日志。
        add_attempt_trail_detail(
            detail, result=result, exc=exc,
            account_trails=account_trails, attempts_summaries=attempts_summaries,
        )
        add_image_timeline_detail(
            detail, result=result, exc=exc, image_timelines=image_timelines,
        )
        add_postprocess_detail(detail, result=result, postprocess=postprocess)
        # 上游请求模型 / 上游 SSE 回报的实际调度模型，分开记。
        add_upstream_model_detail(detail, result=result, upstream_model_slug=upstream_model_slug)
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)
