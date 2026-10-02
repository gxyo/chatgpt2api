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

from services.config import DATA_DIR
from services.protocol.error_response import anthropic_error_response, openai_error_response
from utils.beijing_time import beijing_now_text, beijing_text_from_timestamp
from utils.helper import anthropic_sse_stream, describe_exception, public_error_message, sse_json_stream

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
INTERNAL_RESPONSE_KEYS = {"_account_email", "_conversation_id"}
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
        lines = [
            "ChatGPT2API 日志导出",
            f"导出时间：{beijing_now_text()}",
            f"筛选条件：类型={type or '全部'}，状态={status or '全部'}，"
            f"日期={f'{start_date} ~ {end_date}' if (start_date or end_date) else '不限'}",
            f"条数：{len(items)} 条（最多 {count} 条，按时间由旧到新排列）",
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
        for key in ("account_email", "image_trace", "conversation_id", "endpoint"):
            value = str(detail.get(key) or "").strip()
            if value:
                parts.append(f"{key}={value}")
        return " | ".join(part for part in parts if part)

    def cleanup_before(self, cutoff_day: str) -> dict[str, int]:
        """删除 cutoff_day 之前（不含当天）的日志。

        保留下来的行按原样写回，不做重新序列化——统计服务靠比对已读过的尾部字节来推进
        增量游标，字节变了就得重找位置。日志按时间追加，要删的都是最老的一段前缀，
        因此逐行判断即可，不需要排序。
        """
        if not self.path.exists():
            return {"removed": 0, "kept": 0}
        kept_lines: list[str] = []
        removed = 0
        for line_number, raw_line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            item = self._parse_line(raw_line, line_number)
            day = self._log_day(item) if item is not None else ""
            # 解析不出来的行一律留着：宁可少删几条，也不要把读不懂的内容丢掉。
            if day and day < cutoff_day:
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


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        try:
            result = await run_in_threadpool(handler, *args)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), exc=exc, account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail), exc=exc)
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), exc=exc, account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            response = dict(result)
            response.pop("_account_email", None)
            return response

        sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        try:
            has_first, first = await run_in_threadpool(_next_item, result)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), exc=exc, account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail), exc=exc)
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), exc=exc, account_email=getattr(exc, "account_email", ""))
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
        failed = False
        try:
            for item in items:
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log(
                "流式调用失败",
                status="failed",
                error=str(exc),
                exc=exc,
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "")

    def log(self, suffix: str, result: object = None, status: str = "success", error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "",
            exc: BaseException | None = None) -> None:
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
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)
