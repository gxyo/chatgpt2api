from __future__ import annotations

import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator

import tiktoken

from services.account_service import account_service
from services.config import config
from services.image_storage_service import image_storage_service
from services.image_upscale_service import parse_size_long_edge, upscale_png
from services.openai_backend_api import (
    CONDUIT_WINDOW_TIMEOUT_SECS,
    ImageContentPolicyError,
    ImageMainlineStateError,
    ImagePollTimeoutError,
    OpenAIBackendAPI,
    is_content_policy_error,
    is_skipped_mainline_error,
    poll_timeout_diagnostics,
)
from utils.helper import (
    CHANNEL_BUSY_MESSAGE,
    IMAGE_MODELS,
    IMAGE_RETRY_MESSAGE,
    SKIPPED_MAINLINE_RE,
    UPSTREAM_PLUMBING_RE,
    UpstreamHTTPError,
    anonymize_token,
    extract_image_from_message_content,
    is_codex_image_model,
    is_retriable_upstream_error,
    is_supported_image_model,
    new_trace_id,
    public_error_message,
    sanitize_image_error_text,
    split_image_model,
)
from utils.image_tokens import count_image_content_tokens
from utils.log import logger


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id

    def to_openai_error(self) -> dict[str, Any]:
        error_dict = {
            "error": {
                "message": public_image_error_message(str(self)),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        if self.account_email:
            error_dict["error"]["account_email"] = self.account_email
        return error_dict


class ImageNoToolEventError(ImagePollTimeoutError):
    """SSE 结束，但既没有图片 id，也没有任何图片工具事件。

    正常的生图/图生图会在主链路 SSE 里带 `file_ids`/`sediment_ids`。两样都没有，
    通常说明上游这一轮根本没真正开始生成（静默 skipped_mainline、账号限流或队列
    丢弃）。这时继续冷轮询只会把剩余预算耗光，直接按可重试失败换号更快。
    """


UNSUPPORTED_APP_RESPONSE_CODE = "unsupported_app_response"
UNSUPPORTED_APP_RESPONSE_MESSAGE = "上游返回了不支持的应用响应（没有生成图片文件）"


def unsupported_app_response_message() -> str:
    """给用户看的文案：设置页可覆盖，留空用内置文案。"""
    override = str(getattr(config, "image_unsupported_response_message", "") or "").strip()
    return override or UNSUPPORTED_APP_RESPONSE_MESSAGE


class ImageUnsupportedAppResponseError(ImageGenerationError):
    """上游回的是 ChatGPT 应用/文本响应，而不是图片文件。

    典型形态：
    1. Create Pet 等应用工作流返回 DIL/生成式 UI（`model_dil_v2`、`AsyncImage`、
       “Saved in Library”），会话里没有标准 `file_ids`/`sediment_ids`；
    2. 模型直接回“我无法在这个聊天中生成图片，只能帮你整理提示词”的正文。

    这不是账号或渠道故障：不记该账号失败，但响应形态跟账号、模型路由有关，
    出现时直接中断当前流并换号重试（次数遵循设置页的「生成失败重试次数」），
    最终仍未出图才单独归类、不计入失败率。
    """


MAX_INITIAL_TRANSIENT_TEXT_ACCOUNT_ATTEMPTS = 2
MAX_INITIAL_TRANSIENT_IMAGE_ACCOUNT_ATTEMPTS = 3
# 轮询超时后自动换号重试的次数。设置页的 image_generation_retry_count 是在这之后
# 再追加的「生成失败重试」，两者独立。
MAX_POLL_TIMEOUT_RETRIES = 4
# 模型回正文而非出图时自动换号重试的次数。文本回复往往在预算快耗尽时才对上游
# 拿完流；重试只花剩余预算，不会把整次请求拖成好几份超时的长度。
MAX_TEXT_REPLY_RETRIES = 3
# 同一账号的 conduit 窗口（prepare→mainline）被占时，最多连换几个号，不做等待。
# 窗口正常只占 1 秒出头；同号并发时排队等窗口会把几十秒预算耗在「等别人」上，
# 换到别的号成功率更高。连换这么多次还拿不到，说明整个号池都在握手，再换也
# 只是空转，回退到原来的排队行为。
MAX_CONDUIT_BUSY_SWITCHES = 5
TRANSIENT_TEXT_RESCUE_WINDOW_SECS = 12.0
TRANSIENT_IMAGE_RESCUE_WINDOW_SECS = 20.0
TRANSIENT_RESCUE_SLEEP_SECS = 2.0
IMAGE_POLL_TIMEOUT_MESSAGE = "当前生图任务处理超时，请稍后重试"
# 「生成失败重试」只认这几类最终报错：上游普通失败、token 失效、轮询超时，
# 以及 skipped_mainline（见 is_retryable_image_failure 里的正则判断）。
# 内容违规（裸露/色情等）重试也不会过，换号只会白白消耗账号，必须排除在外。
RETRYABLE_IMAGE_FAILURE_MESSAGES = frozenset({
    IMAGE_RETRY_MESSAGE,
    "upstream authentication token is invalid",
    IMAGE_POLL_TIMEOUT_MESSAGE,
})
# 这两条是本地预算耗尽时自己生成的，不是上游返回的原文。时间线和日志里要分开写，
# 否则看起来像是「上游报错了」，排查时会把 80s 超时误判成渠道故障。
LOCAL_TIMEOUT_FAILURE_MESSAGES = frozenset({
    IMAGE_POLL_TIMEOUT_MESSAGE,
    "image request deadline exceeded",
})


def is_retryable_image_failure(message: object) -> bool:
    text = str(message or "").strip()
    if text in RETRYABLE_IMAGE_FAILURE_MESSAGES:
        return True
    # 上游 400 {"skipped_mainline":true}：这个号的 mainline/conduit 状态被别的
    # 并发请求吃掉了。原样重试同一个号没用，但换号重建握手是安全的恢复方式。
    return bool(SKIPPED_MAINLINE_RE.search(text))


def public_image_error_message(message: str) -> str:
    return sanitize_image_error_text(str(message or "").strip())


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


def is_token_auth_error(exc: BaseException, message: str) -> bool:
    return (
        getattr(exc, "status_code", None) == 401
        or is_token_invalid_error(message)
        or str(getattr(exc, "code", "") or "") in {"invalid_token", "token_invalid", "token_revoked"}
    )


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/SSL 连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (35)" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
    )


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    lower = text.lower()
    if is_token_invalid_error(text):
        return "upstream authentication token is invalid"
    if any(f"status={status}" in lower or f"http {status}" in lower for status in (429, 500, 502, 503, 504)):
        return CHANNEL_BUSY_MESSAGE
    if is_tls_connection_error(text) or is_connection_timeout_error(text):
        return CHANNEL_BUSY_MESSAGE
    return sanitize_image_error_text(text or "upstream completed without returning an image result")


def transient_rescue_wait(deadline: float, attempt: int, request_deadline: float | None = None) -> bool:
    remaining = deadline - time.time()
    if request_deadline is not None:
        remaining = min(remaining, request_deadline - time.monotonic())
    if remaining <= 0:
        return False
    if attempt > 0:
        time.sleep(min(TRANSIENT_RESCUE_SLEEP_SECS, remaining))
    if request_deadline is not None and time.monotonic() >= request_deadline:
        return False
    return time.time() < deadline


def image_request_deadline(timeout_secs: object = None) -> float:
    try:
        seconds = float(timeout_secs)
    except (TypeError, ValueError):
        seconds = float(config.image_poll_timeout_secs)
    return time.monotonic() + max(0.001, seconds)


def image_deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def remaining_image_deadline_secs(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def sleep_with_image_deadline(seconds: float, deadline: float | None) -> None:
    if seconds <= 0:
        return
    remaining = remaining_image_deadline_secs(deadline)
    if remaining is not None:
        seconds = min(seconds, remaining)
    if seconds > 0:
        time.sleep(seconds)


def ensure_image_deadline(deadline: float | None) -> None:
    if image_deadline_expired(deadline):
        raise ImageGenerationError(IMAGE_POLL_TIMEOUT_MESSAGE, code="upstream_timeout")


REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(
    r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}'
)


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。

    检测两种模式：
    1. 完整的工具调用 JSON（含 referenced_image_ids）
    2. 部分的工具参数 JSON（如 {"size":"1920x1088","n":1}）
    """
    if not message:
        return False
    if REFERENCED_IMAGE_IDS_RE.search(message):
        return True
    # 检测部分工具参数 JSON（模型返回了工具参数但未触发工具）
    if TOOL_PARAMS_JSON_RE.search(message):
        return True
    return False


# 「模型明确说不能在这个聊天里生成图片、只能给提示词」：这是应用/文本响应，
# 不是内容审核拒绝，也不是普通的文字回复。
_UNSUPPORTED_IMAGE_REFUSAL_RE = re.compile(
    r"(?:无法|不能|没法)[^。\n]{0,24}(?:直接)?(?:生成|创建|绘制|画)[^。\n]{0,8}(?:图片|图像|图)"
    r"|(?:cannot|can't|can not|unable to)\s+(?:directly\s+)?(?:generate|create|draw)\s+(?:an?\s+)?(?:image|picture)",
    re.IGNORECASE,
)
_UNSUPPORTED_REFUSAL_CONTEXT = (
    "这个聊天", "这个对话", "此聊天", "此对话", "当前聊天", "当前对话",
    "in this chat", "in this conversation",
)
# 注意不要收「提示语」：内容审核拒绝的文案常说「请修改提示语」，那是另一类。
_UNSUPPORTED_PROMPT_CONTEXT = ("提示词", "writingblock", "image prompt", "final image prompt", "prompt")


def unsupported_app_response_reason(message: str) -> str:
    """返回命中的判定原因；空字符串表示不是应用/文本响应。

    原因会写进日志的 `image_diagnostics.unsupported_reason` 和异常 body，
    以后排查时能直接看出是「模型拒绝出图」还是「Create Pet 的 DIL/Library」，
    不用再去翻 SSE 原文。只认比较明确的形态，避免误伤内容审核拒绝和普通正文：

    - `refusal_prompt`：「我无法在这个聊天里生成图片，只能帮你整理提示词」；
    - `dil_library`：Create Pet 等应用工作流（`Saved in Library` + `AsyncImage` /
      `model_dil_v2` / `opGenui`）；
    - `async_image` / `genui_app`：只有生成式 UI / AsyncImage 标记；
    - `writing_block_prompt`：返回 `<WritingBlock>` 提示词文档而不是图片。
    """
    text = str(message or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if _UNSUPPORTED_IMAGE_REFUSAL_RE.search(text):
        if any(marker in text for marker in _UNSUPPORTED_REFUSAL_CONTEXT) or any(
            marker in lower for marker in _UNSUPPORTED_PROMPT_CONTEXT
        ):
            return "refusal_prompt"
    # 应用工作流的 Library 产物：单独一句 “Saved in Library” 可能只是正常解说，
    # 必须同时出现 DIL/AsyncImage 这类应用 UI 标记。
    if "saved in library" in lower and (
        "asyncimage" in lower or "model_dil_v2" in lower or "opgenui" in lower
    ):
        return "dil_library"
    if "asyncimage" in lower and ("query=" in lower or "aspectratio" in lower):
        return "async_image"
    if "opgenui" in lower and ("model_dil_v2" in lower or "dil.render" in lower):
        return "genui_app"
    if "<writingblock" in lower and (
        "提示词" in text or "image prompt" in lower or "final image prompt" in lower
    ):
        return "writing_block_prompt"
    return ""


def is_unsupported_app_response(message: str) -> bool:
    """上游没有产出图片文件，而是回了 ChatGPT 应用/文本响应。"""
    return bool(unsupported_app_response_reason(message))


# 每个 delta 都跑完整正则代价太大；只有 delta 里出现这些词时才检查累计文本。
_UNSUPPORTED_DELTA_TRIGGER_RE = re.compile(
    r"无法|不能|没法|生成图片|生成图像|提示词|library|asyncimage|writingblock"
    r"|can't|cannot|can not|unable|opgenui",
    re.IGNORECASE,
)


def _record_unsupported_stream_summary(
        backend: Any,
        event: dict[str, Any] | None,
        text: str,
        conversation_id: str,
) -> None:
    """失败时也留一份「上游流结果」摘要。

    正常成功路径在 `conversation_done` 才写 `image_stream_summary`；应用响应可能在
    流中途就被判定失败，等不到那一步。这里用触发判定时的事件补一份，日志里就能
    直接看到 `tool_invoked` / `turn_use_case` / `plan_type` / 图片 id 数量。
    """
    if backend is None or not isinstance(event, dict):
        return
    if getattr(backend, "image_stream_summary", None):
        return
    file_ids = event.get("file_ids") or []
    sediment_ids = event.get("sediment_ids") or []
    summary = {
        "at": time.monotonic(),
        "conversation_id": conversation_id,
        "tool_invoked": event.get("tool_invoked"),
        "turn_use_case": event.get("turn_use_case"),
        "model_slug": event.get("model_slug"),
        "plan_type": event.get("plan_type"),
        "product_experience": event.get("product_experience"),
        "app_ui_marker": bool(event.get("app_ui_marker")),
        "file_ids": len(file_ids),
        "sediment_ids": len(sediment_ids),
        "message_chars": len(text or ""),
        "unsupported_app_response": True,
    }
    try:
        backend.image_stream_summary = summary
    except Exception:
        pass


def _unsupported_app_response_error(
        text: str,
        *,
        conversation_id: str = "",
        backend: Any = None,
        event: dict[str, Any] | None = None,
        reason: str = "",
) -> ImageUnsupportedAppResponseError:
    exc = ImageUnsupportedAppResponseError(
        unsupported_app_response_message(),
        status_code=502,
        error_type="server_error",
        code=UNSUPPORTED_APP_RESPONSE_CODE,
        conversation_id=conversation_id,
    )
    # 挂在异常上供日志/尝试轨迹读取（body 里的 upstream_text 仍按 1000 字截断）。
    exc.unsupported_reason = reason
    exc.upstream_text = text or ""
    _record_unsupported_stream_summary(backend, event, text, conversation_id)
    body: dict[str, Any] = {"unsupported_app_response": True}
    if reason:
        body["unsupported_reason"] = reason
    if text:
        body["upstream_text"] = text[:1000]
    if backend is not None:
        body = _attach_stream_context(body, backend)
    exc.body = body
    return exc


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(image_data: bytes, base_url: str | None = None) -> str:
    return image_storage_service.save(image_data, base_url).url


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def _merge_postprocess(target: dict[str, Any], source: dict[str, Any]) -> None:
    """合并多张图的本地后处理数据：耗时累加，尺寸/错误保留第一次有值的。"""
    for key in ("upscale_ms", "upscale_queue_wait_ms"):
        value = source.get(key)
        if value:
            target[key] = int(target.get(key) or 0) + int(value)
    for key in ("upscale_target", "upscale_from_size", "upscale_to_size", "upscale_error"):
        value = source.get(key)
        if value and not target.get(key):
            target[key] = value
    if source.get("upscale_to_size") or source.get("upscale_error"):
        target["upscale_images"] = int(target.get("upscale_images") or 0) + 1


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
    size: str | None = None,
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    # 超分是本地 CPU 耗时，不进上游轮询预算，但要记下来：4K 失败时得能看出
    # 时间花在 1K 出图还是本地放大上。
    postprocess: dict[str, Any] = {}
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        # 免费号只出 1K 图，落盘前放大：客户端 size 要了 2K/4K 就照办，
        # 不传或 "auto" 则维持 1K。b64_json 和 url 用同一份字节，免得两个出口不一致。
        upscale_diagnostics: dict[str, Any] = {}
        image_bytes = upscale_png(base64.b64decode(b64_json), parse_size_long_edge(size), upscale_diagnostics)
        _merge_postprocess(postprocess, upscale_diagnostics)
        url = save_image_bytes(image_bytes, base_url)
        if response_format == "b64_json":
            data.append({
                "b64_json": base64.b64encode(image_bytes).decode("ascii"),
                "url": url,
                "revised_prompt": revised_prompt,
            })
        else:
            data.append({
                "url": url,
                "revised_prompt": revised_prompt,
            })
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    if postprocess:
        result["_postprocess"] = postprocess
    return result


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    deadline: float | None = None
    progress_callback: Any = None  # Callable[[str], None] | None
    # 单次请求的关联 id（多图时形如 "<base>-<index>"）。日志详情和容器日志都用它，
    # 拿到用户贴的日志能直接定位这一条请求全链路的记录。
    trace_id: str = ""


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""
    # server_ste_metadata 里上游汇报的实际调度模型，可能和请求里写的 slug 不同。
    model_slug: str = ""
    # server_ste_metadata 里的账号计划（free/plus/team…）与产品体验（chat/image…）。
    # 免费号没有生图能力时，上游会回“只能给提示词”的正文；日志里要能直接看到。
    plan_type: str = ""
    product_experience: str = ""
    # 上游回的是 ChatGPT 应用/生成式 UI（payload 里出现 opGenui），不是普通图片结果。
    # 只在「零图片 id + 超时」的兜底判断里用，避免把普通的富文本消息误判成应用响应。
    app_ui_marker: bool = False


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""
    # 上游 SSE 汇报的实际调度模型（server_ste_metadata.model_slug）。只进调用日志。
    upstream_model_slug: str = ""
    # 本次图片生成实际试过的账号（按顺序）。只有换号重试过才有值，
    # 调用日志据此把「第 1 个号 → 第 2 个号」写进同一条记录，而不是一次尝试一条。
    account_trail: list[str] = field(default_factory=list)
    attempts_summary: str = ""
    # 逐次尝试的结构化现场（账号、结局、耗时、错误原文）。只进调用日志：
    # 日志页据此渲染可展开的「尝试轨迹」，不用再受一行摘要的长度限制。
    attempts: list[dict[str, Any]] = field(default_factory=list)
    # 本次图片生成的执行时间线：每一步做了什么、什么时候开始、花了多久、为什么。
    # 只进调用日志（日志详情里逐条展示），不返回给客户端。
    image_timeline: list[dict[str, Any]] = field(default_factory=list)
    # 多图并行时没出结果的那几张图的时间线（每张一份 group）。成功图片的 chunk 顺带
    # 把这些带上，调用日志才能同时看到「哪张成功了、哪张失败在哪一步」，而不是只留成功的那份。
    failed_image_timelines: list[dict[str, Any]] = field(default_factory=list)
    # 这条 message 是不是「上游回了正文、没有出图」。只有这种才按文本回复处理
    # （可用于换号重试）；blocked/任务错误等真正的报错文案走内容策略分支。
    text_reply: bool = False
    # 这条 message 的正文其实是上游管道错误（例如 400 {"skipped_mainline":true} 被
    # 解析进了 assistant 文本）。它不是模型正文，不能按 text_reply 重试/展示。
    plumbing_error: bool = False
    # 本地后处理（4K 超分）的耗时与失败原因，只进调用日志，不返回给客户端。
    postprocess: dict[str, Any] = field(default_factory=dict)

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.upstream_model_slug:
            chunk["_upstream_model_slug"] = self.upstream_model_slug
        if self.account_trail:
            chunk["_account_trail"] = list(self.account_trail)
        if self.attempts_summary:
            chunk["_attempts_summary"] = self.attempts_summary
        if self.attempts:
            chunk["_attempts"] = [dict(item) for item in self.attempts]
        if self.image_timeline or self.failed_image_timelines:
            groups: list[dict[str, Any]] = []
            if self.image_timeline:
                groups.append({
                    "index": self.index,
                    "total": self.total,
                    "steps": list(self.image_timeline),
                })
            groups.extend(self.failed_image_timelines)
            chunk["_image_timeline"] = groups
        if self.postprocess:
            chunk["_postprocess"] = dict(self.postprocess)
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    if not state.app_ui_marker and ("opGenui" in payload or "opgenui" in payload):
        # Create Pet / 应用类响应会在 DIL 代码里读 appData.opGenui。普通图片结果
        # 不会带这一层，用它给「超时前一个图片 id 都没给」的情况兜底分类。
        # 先做大小写敏感的廉价判断，避免每个事件都对整段 payload 调 lower()。
        state.app_ui_marker = True
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)
            state.model_slug = str(metadata.get("model_slug") or state.model_slug).strip()
            state.plan_type = str(metadata.get("plan_type") or state.plan_type).strip()
            state.product_experience = str(
                metadata.get("product_experience") or state.product_experience
            ).strip()


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        "model_slug": state.model_slug,
        "plan_type": state.plan_type,
        "product_experience": state.product_experience,
        "app_ui_marker": state.app_ui_marker,
        **extra,
    }


def iter_conversation_payloads(payloads: Iterator[str], history_text: str = "",
                               history_messages: list[str] | None = None) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = build_image_prompt(prompt, size, quality) if image_model else prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
    )
    yield from iter_conversation_payloads(payloads, history_text, history_messages)


def text_backend() -> OpenAIBackendAPI:
    return OpenAIBackendAPI(access_token=account_service.get_text_access_token())


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    attempted_anon = False
    last_transient_error = False
    transient_attempts = 0
    rescue_deadline = 0.0
    rescue_attempts = 0
    emitted = False
    while True:
        in_rescue = rescue_deadline > 0
        if in_rescue:
            if not transient_rescue_wait(rescue_deadline, rescue_attempts):
                raise RuntimeError(CHANNEL_BUSY_MESSAGE)
            rescue_attempts += 1
            token = account_service.get_text_access_token()
            if not token:
                raise RuntimeError(CHANNEL_BUSY_MESSAGE)
        else:
            if token and token in attempted_tokens:
                if last_transient_error:
                    rescue_deadline = time.time() + TRANSIENT_TEXT_RESCUE_WINDOW_SECS
                    continue
                raise RuntimeError("no available text account")
            if token:
                attempted_tokens.add(token)
            elif attempted_anon:
                if last_transient_error:
                    rescue_deadline = time.time() + TRANSIENT_TEXT_RESCUE_WINDOW_SECS
                    continue
                raise RuntimeError("no available text account")
            else:
                attempted_anon = True
        try:
            active_backend = OpenAIBackendAPI(access_token=token)
            for event in conversation_events(active_backend, messages=request.messages, model=request.model, prompt=request.prompt):
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    yield delta
            account_service.mark_text_used(token)
            return
        except Exception as exc:
            error_message = str(exc)
            if token and not emitted and is_token_invalid_error(error_message):
                refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                    token = refreshed_token
                else:
                    account_service.remove_invalid_token(token, "text_stream")
                    token = account_service.get_text_access_token(attempted_tokens)
                if token:
                    continue
            if is_retriable_upstream_error(exc):
                last_transient_error = True
                transient_attempts += 1
                logger.warning({
                    "event": "text_stream_transient_fail",
                    "request_token": anonymize_token(token),
                    "emitted": emitted,
                    "transient_attempts": transient_attempts,
                    "rescue": in_rescue,
                    "error": error_message,
                })
                if in_rescue:
                    continue
                if transient_attempts >= MAX_INITIAL_TRANSIENT_TEXT_ACCOUNT_ATTEMPTS:
                    rescue_deadline = time.time() + TRANSIENT_TEXT_RESCUE_WINDOW_SECS
                    continue
                if token and not emitted:
                    token = account_service.get_text_access_token(attempted_tokens)
                    if token:
                        continue
                raise RuntimeError(CHANNEL_BUSY_MESSAGE) from exc
            raise


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": str(exc),
        })
        return ""


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    last: dict[str, Any] = {}
    # 阶段打点：失败时 backend.image_phase_diagnostics() 会折算成各阶段耗时，
    # 用来回答「这条请求的时间到底花在握手、主链路还是轮询上」。
    _mark_phase(backend, "conversation_start")
    try:
        for event in conversation_events(
                backend,
                prompt=request.prompt,
                model=request.model,
                images=request.images or [],
                size=request.size,
                quality=request.quality,
        ):
            last = event
            _remember_stream_event(backend, event)
            if event.get("type") == "conversation.delta":
                delta = str(event.get("delta") or "")
                # 应用/拒绝类响应一旦出现明确文案就直接失败，不再等完整流或轮询。
                # 只在 delta 带触发词时检查累计文本，避免每个 delta 都跑整段正则。
                if (
                    delta
                    and not event.get("file_ids")
                    and not event.get("sediment_ids")
                    and _UNSUPPORTED_DELTA_TRIGGER_RE.search(delta)
                    and not is_content_policy_error(str(event.get("text") or ""))
                ):
                    text = str(event.get("text") or "")
                    reason = unsupported_app_response_reason(text)
                    if reason:
                        raise _unsupported_app_response_error(
                            text,
                            conversation_id=str(event.get("conversation_id") or ""),
                            backend=backend,
                            event=event,
                            reason=reason,
                        )
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text=delta,
                    upstream_event_type="conversation.delta",
                )
                continue
            if event.get("type") == "conversation.event":
                raw = event.get("raw")
                raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    upstream_event_type=raw_type,
                )
    except ImagePollTimeoutError:
        # Create Pet 这类应用可能一直流 DIL，直到预算到点都没有图片 id。
        # 已经能看出是应用响应时，按「不支持的应用响应」收尾，而不是记成生图失败。
        text = str(last.get("text") or "")
        reason = unsupported_app_response_reason(text)
        marker_timeout = (
            bool(last.get("app_ui_marker"))
            and last.get("turn_use_case") != "image gen"
        )
        if (
            not last.get("file_ids")
            and not last.get("sediment_ids")
            and (reason or marker_timeout)
        ):
            raise _unsupported_app_response_error(
                text,
                conversation_id=str(last.get("conversation_id") or ""),
                backend=backend,
                event=last,
                reason=reason or "app_ui_timeout",
            ) from None
        raise

    _mark_phase(backend, "conversation_done")
    conversation_id = str(last.get("conversation_id") or "")
    upstream_model_slug = str(last.get("model_slug") or "").strip()
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    # 主链路结束时的现场。成功调用日志的时间线里直接展示，用来一眼确认
    # 「这一轮上游到底有没有调起图片工具、SSE 有没有带 id」。
    try:
        backend.image_stream_summary = {
            "at": time.monotonic(),
            "conversation_id": conversation_id,
            "tool_invoked": last.get("tool_invoked"),
            "turn_use_case": last.get("turn_use_case"),
            "model_slug": upstream_model_slug,
            "plan_type": last.get("plan_type"),
            "product_experience": last.get("product_experience"),
            "app_ui_marker": bool(last.get("app_ui_marker")),
            "file_ids": len(file_ids),
            "sediment_ids": len(sediment_ids),
            "message_chars": len(message),
        }
    except Exception:
        pass
    # 上游管道错误有时会被 SSE 包装成 assistant 正文（线上的 400
    # {"skipped_mainline":true} 就出现过这种形态）。它不是模型正文：立即按上游 400
    # 失败，交给外层换号/重建 conduit，别再花轮询预算、更别当 text_reply 重试。
    if message and not file_ids and not sediment_ids and SKIPPED_MAINLINE_RE.search(message):
        body = _attach_stream_context(
            _plumbing_message_body(message) or {"upstream_response": message[:1000]},
            backend,
        )
        logger.warning({
            "event": "image_upstream_skipped_mainline_in_text",
            "conversation_id": conversation_id,
            "message": message[:300],
            "stream_tail": body.get("stream_tail") or [],
        })
        raise UpstreamHTTPError("image stream", 400, body)
    logger.info({
        "event": "image_stream_resolve_start",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
    })
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")
    if message and not file_ids and not sediment_ids and last.get("blocked"):
        # 尝试从 /backend-api/tasks/ 获取详细错误信息
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id)
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text=sanitize_image_error_text(error_text), conversation_id=conversation_id,
                          upstream_model_slug=upstream_model_slug)
        return
    # 上游没出图，而是回了应用/拒绝类文本：不轮询，直接抛给外层中断并换号重试。
    # 放在 blocked 之后，内容审核拒绝仍然走上面的策略拦截分支。
    unsupported_reason = unsupported_app_response_reason(message)
    marker_only = (
        not message
        and bool(last.get("app_ui_marker"))
        and last.get("turn_use_case") != "image gen"
    )
    unsupported_app_response = (
        not file_ids
        and not sediment_ids
        and not is_content_policy_error(message)
        and (bool(unsupported_reason) or marker_only)
    )
    if unsupported_app_response:
        logger.warning({
            "event": "image_unsupported_app_response",
            "conversation_id": conversation_id,
            "tool_invoked": last.get("tool_invoked"),
            "turn_use_case": last.get("turn_use_case"),
            "plan_type": last.get("plan_type"),
            "product_experience": last.get("product_experience"),
            "app_ui_marker": bool(last.get("app_ui_marker")),
            "unsupported_reason": unsupported_reason or "app_ui_marker",
            "message_chars": len(message),
            "message_preview": message[:300],
            "stream_tail": _image_stream_tail(backend, IMAGE_STREAM_TAIL_IN_BODY),
        })
        raise _unsupported_app_response_error(
            message,
            conversation_id=conversation_id,
            backend=backend,
            event=last,
            reason=unsupported_reason or "app_ui_marker",
        )
    should_poll_for_image = bool(request.images) or last.get("turn_use_case") == "image gen"
    if message and not file_ids and not sediment_ids and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text=sanitize_image_error_text(message), conversation_id=conversation_id,
                          upstream_model_slug=upstream_model_slug)
        return

    # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
    # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开，
    # 图片可能正在异步生成中。需要使用更积极的轮询策略来获取结果。
    is_text_reply = bool(message and is_model_text_reply_instead_of_image(message))
    if is_text_reply:
        logger.info({
            "event": "image_detected_text_reply_with_ids",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    # 当检测到文本回复但 conversation_id 丢失时，尝试从最近对话列表中恢复
    # SSE 流太短时（模型返回文本而非触发图片工具），conversation_id 可能未被捕获，
    # 但图片已在上游异步生成。通过列出最近对话来恢复 conversation_id。
    if is_text_reply and not conversation_id:
        try:
            import time as _time
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, _time.time(), timeout_secs=5.0,
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_conversation_id_recovered",
                    "conversation_id": conversation_id,
                    "message_preview": message[:200],
                })
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })

    # 在轮询图片之前，先检查 /backend-api/tasks/ 是否有 moderation 拦截
    # 这样可以避免不必要的长时间轮询超时
    # 注意：当 should_poll_for_image 为 True 或检测到文本回复时，
    # 即使 tasks 报告了"错误"，也不能直接返回——因为上游可能将工具调用的 JSON 参数
    # （如 {"size":"1792x1024","n":1}）标记为 is_error，而实际上图片正在异步生成中。
    # 此时应继续轮询图片。
    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id:
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id, timeout_secs=5.0, wait_secs=1.0)
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                              text=sanitize_image_error_text(detailed_error), conversation_id=conversation_id,
                              upstream_model_slug=upstream_model_slug)
            return
        if detailed_error and (should_poll_for_image or is_text_reply):
            logger.info({
                "event": "image_task_error_skipped_for_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    # SSE 已经读完，但既没有图片 id、也没有任何图片工具事件：这轮上游大概率
    # 根本没开始生成。此时冷轮询查不到任何结果，只会把剩余预算耗光——直接
    # 按可重试失败换号。文本回复（referenced_image_ids / 工具参数 JSON）是
    # 已知的「图在异步生成」形态，仍交给下面的轮询。
    if (
            not file_ids
            and not sediment_ids
            and not is_text_reply
            and last.get("tool_invoked") is not True
            # 内容政策拒绝不是账号问题：换号只会继续撞墙，仍交给轮询里的
            # `_find_content_policy_error_in_conversation` / task error 识别并按 400 返回。
            and not (message and is_content_policy_error(message))
            and not (detailed_error and is_content_policy_error(detailed_error))
    ):
        logger.warning({
            "event": "image_stream_no_tool_event_skip_poll",
            "conversation_id": conversation_id,
            "tool_invoked": last.get("tool_invoked"),
            "turn_use_case": last.get("turn_use_case"),
            "message_chars": len(message),
            "task_error": detailed_error[:200] if detailed_error else "",
            "stream_tail": _image_stream_tail(backend, IMAGE_STREAM_TAIL_IN_BODY),
        })
        exc = ImageNoToolEventError(
            "上游本轮生图没有开始（SSE 结束仍无图片 id / 图片工具事件），已跳过轮询并换号重试"
        )
        setattr(exc, "conversation_id", conversation_id or "")
        raise exc

    # 当检测到文本回复（含 referenced_image_ids）时仍继续轮询图片结果，
    # 但轮询预算必须尊重请求级 deadline / image_poll_timeout_secs。
    poll_timeout = config.image_poll_timeout_secs
    if not file_ids and not sediment_ids and not is_text_reply:
        # 冷轮询：主链路一个图片 id 都没给。即使 tool_invoked=True 说明工具被调起，
        # 正常路径也已经错过了；按更短的预算等一会儿就换号，别把整个请求预算耗在这。
        cold_timeout = float(config.image_poll_cold_timeout_secs)
        if cold_timeout < poll_timeout:
            logger.info({
                "event": "image_poll_cold_budget",
                "conversation_id": conversation_id,
                "poll_timeout_secs": poll_timeout,
                "cold_timeout_secs": cold_timeout,
                "tool_invoked": last.get("tool_invoked"),
            })
            poll_timeout = cold_timeout
    if is_text_reply and conversation_id:
        logger.info({
            "event": "image_text_reply_poll",
            "conversation_id": conversation_id,
            "poll_timeout_secs": poll_timeout,
        })

    _mark_phase(backend, "poll_start")
    try:
        image_urls = backend.resolve_conversation_image_urls(
            conversation_id, file_ids, sediment_ids, poll_timeout_secs=poll_timeout,
        )
    except (ImageContentPolicyError, ImagePollTimeoutError) as exc:
        # 当检测到文本回复时，task error 不应直接判定为内容策略违规，
        # 因为图片可能仍在后台异步生成中
        if is_text_reply and isinstance(exc, ImageContentPolicyError):
            logger.warning({
                "event": "image_text_reply_task_error_ignored",
                "conversation_id": conversation_id,
                "error": str(exc),
            })
            image_urls = []
        else:
            raise
    except Exception as exc:
        # 当检测到文本回复时，首次轮询的临时网络错误不应直接中断，
        # 因为图片可能仍在后台异步生成中，后续 retry poll 会继续尝试。
        if is_text_reply and conversation_id:
            logger.warning({
                "event": "image_text_reply_first_poll_error_ignored",
                "conversation_id": conversation_id,
                "error": repr(exc)[:300],
            })
            image_urls = []
        else:
            raise

    _mark_phase(backend, "poll_done")
    if image_urls:
        if request.progress_callback:
            request.progress_callback("receiving_image")
        _mark_phase(backend, "download_start")
        image_items = [
            {"b64_json": base64.b64encode(image_data).decode("ascii")}
            for image_data in backend.download_image_bytes(image_urls)
        ]
        _mark_phase(backend, "download_done")
        formatted = format_image_result(
            image_items,
            request.prompt,
            request.response_format,
            request.base_url,
            int(time.time()),
            size=request.size,
        )
        data = formatted["data"]
        if data:
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data,
                              conversation_id=conversation_id, upstream_model_slug=upstream_model_slug,
                              postprocess=formatted.get("_postprocess") or {})
        return

    if message:
        # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
        # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开。
        # 此时应再尝试轮询图片结果，而不是直接把文本当作最终输出。
        # 当 is_text_reply 但 conversation_id 丢失时，尝试从最近对话列表恢复
        if is_text_reply and not conversation_id:
            try:
                import time as _time
                recovered_id = backend.find_conversation_by_prompt(
                    request.prompt, _time.time(), timeout_secs=5.0,
                )
                if recovered_id:
                    conversation_id = recovered_id
                    logger.info({
                        "event": "image_text_reply_conversation_id_recovered",
                        "conversation_id": conversation_id,
                        "message_preview": message[:200],
                    })
            except Exception as exc:
                logger.warning({
                    "event": "image_text_reply_conversation_id_recovery_failed",
                    "error": repr(exc)[:300],
                })
        if is_text_reply and conversation_id:
            logger.info({
                "event": "image_model_text_reply_retry_poll",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
            retry_poll_timeout = config.image_poll_timeout_secs
            MAX_POLL_RETRIES = 3
            for poll_attempt in range(1, MAX_POLL_RETRIES + 1):
                ensure_image_deadline(request.deadline)
                try:
                    polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                        conversation_id,
                        retry_poll_timeout,
                        file_ids,
                        sediment_ids,
                    )
                    file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                    sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                    break  # 轮询成功，退出重试循环
                except Exception as exc:
                    error_str = str(exc)
                    is_transient = (
                        isinstance(exc, ImagePollTimeoutError)
                        or is_tls_connection_error(error_str)
                        or "upstream" in error_str.lower()
                        or "connection" in error_str.lower()
                        or "timeout" in error_str.lower()
                    )
                    logger.warning({
                        "event": "image_model_text_reply_poll_failed",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "error": repr(exc)[:300],
                        "is_transient": is_transient,
                    })
                    # 如果还有重试次数且不是超时/内容违规错误，继续重试
                    if poll_attempt < MAX_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                        # 递增退避：30s, 60s, 90s
                        backoff = 30.0 * poll_attempt
                        logger.info({
                            "event": "image_model_text_reply_poll_retry",
                            "conversation_id": conversation_id,
                            "poll_attempt": poll_attempt,
                            "backoff_secs": backoff,
                        })
                        sleep_with_image_deadline(backoff, request.deadline)
                        continue
                    # 超时错误或重试次数用尽，停止重试
                    break

            if file_ids or sediment_ids:
                image_urls = backend.resolve_conversation_image_urls(
                    conversation_id, file_ids, sediment_ids, poll=False,
                )
                if image_urls:
                    if request.progress_callback:
                        request.progress_callback("receiving_image")
                    image_items = [
                        {"b64_json": base64.b64encode(image_data).decode("ascii")}
                        for image_data in backend.download_image_bytes(image_urls)
                    ]
                    formatted = format_image_result(
                        image_items,
                        request.prompt,
                        request.response_format,
                        request.base_url,
                        int(time.time()),
                        size=request.size,
                    )
                    data = formatted["data"]
                    if data:
                        yield ImageOutput(kind="result", model=request.model, index=index, total=total,
                                          data=data, conversation_id=conversation_id,
                                          upstream_model_slug=upstream_model_slug,
                                          postprocess=formatted.get("_postprocess") or {})
                        return
        elif is_text_reply:
            logger.warning({
                "event": "image_model_text_reply_no_image",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
        # 走到这里 = 上游回了正文，但轮询（含直接解析）始终没拿到图片。
        # 正文可能是模型「已完成」的解说词，也可能是上游把管道错误（JSON body）
        # 塞进了 assistant 文本——后者必须按上游失败处理，不能当 text_reply 重试。
        plumbing_body = _plumbing_message_body(message)
        logger.warning({
            "event": "image_text_reply_no_image",
            "conversation_id": conversation_id,
            "stream_file_ids": len(file_ids),
            "stream_sediment_ids": len(sediment_ids),
            "is_text_reply_pattern": is_text_reply,
            "plumbing_error": plumbing_body is not None,
            "message_chars": len(message),
            "message_preview": message[:300],
            "stream_tail": _image_stream_tail(backend, IMAGE_STREAM_TAIL_IN_BODY),
        })
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message,
                          conversation_id=conversation_id, text_reply=plumbing_body is None,
                          plumbing_error=plumbing_body is not None,
                          upstream_model_slug=upstream_model_slug)
        return

    # 兜底：当 message 为空且图片 URL 解析失败时，先尝试一次短延迟重试轮询
    # 然后抛出明确错误而非让调用方得到 "upstream completed without generating images" 这种模糊报错
    logger.warning({
        "event": "image_stream_no_result_fallback",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "should_poll_for_image": should_poll_for_image,
    })
    # 当 should_poll_for_image 为 True 但 conversation_id 丢失时，尝试恢复
    if should_poll_for_image and not conversation_id:
        try:
            import time as _time
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, _time.time(), timeout_secs=5.0,
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_fallback_conversation_id_recovered",
                    "conversation_id": conversation_id,
                })
        except Exception as exc:
            logger.warning({
                "event": "image_fallback_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })
    if should_poll_for_image and conversation_id:
        # 图片可能仍在异步处理中（上游 SSE 流在图片生成完成前就结束了）。
        # 后续轮询仍使用配置的超时预算，不强制延长到固定 300s；但如果一个图片
        # id 都没有，按冷轮询的更短预算处理，避免又把整个请求预算耗在等待上。
        retry_poll_timeout = config.image_poll_timeout_secs
        if not file_ids and not sediment_ids:
            retry_poll_timeout = min(retry_poll_timeout, config.image_poll_cold_timeout_secs)
        MAX_FALLBACK_POLL_RETRIES = 3
        for poll_attempt in range(1, MAX_FALLBACK_POLL_RETRIES + 1):
            ensure_image_deadline(request.deadline)
            retry_wait_secs = min(30.0 * poll_attempt, config.image_poll_initial_wait_secs * poll_attempt)
            logger.info({
                "event": "image_stream_retry_poll_after_wait",
                "conversation_id": conversation_id,
                "retry_wait_secs": retry_wait_secs,
                "poll_attempt": poll_attempt,
            })
            sleep_with_image_deadline(retry_wait_secs, request.deadline)
            ensure_image_deadline(request.deadline)
            try:
                polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                    conversation_id,
                    retry_poll_timeout,
                    file_ids,
                    sediment_ids,
                )
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                break  # 轮询成功，退出重试循环
            except Exception as exc:
                error_str = str(exc)
                is_transient = (
                    isinstance(exc, ImagePollTimeoutError)
                    or is_tls_connection_error(error_str)
                    or "upstream" in error_str.lower()
                    or "connection" in error_str.lower()
                    or "timeout" in error_str.lower()
                )
                logger.warning({
                    "event": "image_stream_retry_poll_failed",
                    "conversation_id": conversation_id,
                    "poll_attempt": poll_attempt,
                    "error": repr(exc)[:300],
                    "is_transient": is_transient,
                })
                # 如果还有重试次数且不是超时/内容违规错误，继续重试
                if poll_attempt < MAX_FALLBACK_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                    # 递增退避：30s, 60s
                    backoff = 30.0 * poll_attempt
                    logger.info({
                        "event": "image_stream_retry_poll_retry",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "backoff_secs": backoff,
                    })
                    sleep_with_image_deadline(backoff, request.deadline)
                    continue
                # 超时错误或重试次数用尽，停止重试
                break

        if file_ids or sediment_ids:
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if image_urls:
                if request.progress_callback:
                    request.progress_callback("receiving_image")
                image_items = [
                    {"b64_json": base64.b64encode(image_data).decode("ascii")}
                    for image_data in backend.download_image_bytes(image_urls)
                ]
                formatted = format_image_result(
                    image_items,
                    request.prompt,
                    request.response_format,
                    request.base_url,
                    int(time.time()),
                    size=request.size,
                )
                data = formatted["data"]
                if data:
                    yield ImageOutput(kind="result", model=request.model, index=index, total=total,
                                      data=data, conversation_id=conversation_id,
                                      upstream_model_slug=upstream_model_slug,
                                      postprocess=formatted.get("_postprocess") or {})
                    return

        # 重试后仍然失败，yield 错误消息
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation completed upstream but the result could not be retrieved. "
                               "The image may still be processing. Please try again in a moment.",
                          conversation_id=conversation_id,
                          upstream_model_slug=upstream_model_slug)
    elif message:
        plumbing_body = _plumbing_message_body(message)
        logger.warning({
            "event": "image_stream_message_fallback",
            "conversation_id": conversation_id,
            "message_preview": message[:300],
            "plumbing_error": plumbing_body is not None,
            "stream_tail": _image_stream_tail(backend, IMAGE_STREAM_TAIL_IN_BODY),
        })
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message,
                          conversation_id=conversation_id, plumbing_error=plumbing_body is not None,
                          upstream_model_slug=upstream_model_slug)
    else:
        # conversation_id 也为空时（SSE 流极短、未捕获到会话 ID），
        # 仍然 yield 一条消息，避免 stream_image_outputs_with_pool 产生
        # "upstream completed without generating images" 模糊报错
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation started upstream but the response was incomplete. "
                               "Please try again.",
                          conversation_id=conversation_id,
                          upstream_model_slug=upstream_model_slug)


def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    )))
    if not images:
        raise ImageGenerationError("No image result found in response")
    formatted = format_image_result(
        [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
        size=request.size,
    )
    data = formatted["data"]
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data,
                          postprocess=formatted.get("_postprocess") or {})
        return
    raise ImageGenerationError("No image result found in response")


def _mark_phase(backend: Any, name: str) -> None:
    """给后台记一个阶段时间点。测试里的假 backend 没有这个方法，静默跳过。"""
    marker = getattr(backend, "mark_image_phase", None)
    if not callable(marker):
        return
    try:
        marker(name)
    except Exception:
        pass


def _conduit_state(backend: Any) -> dict[str, Any]:
    """这次尝试的 conduit 握手窗口现场：窗口拿到没有、等了多少毫秒。

    这是排查 ``skipped_mainline`` 最关键的一组数——拿不到窗口还硬发主链路，
    上游就会把这条 turn 判成 skipped_mainline。
    """
    if backend is None:
        return {}
    state: dict[str, Any] = {}
    # acquired 是粘性的（拿到就记 true，不会因为正常释放被抹掉）；
    # window_held 是当前锁状态，主链路跑完后按设计就是 false，别拿它当证据。
    acquired = getattr(backend, "conduit_acquired", None)
    if acquired is not None:
        state["conduit_acquired"] = bool(acquired)
    held = getattr(backend, "conduit_window_held", None)
    if held is not None:
        state["conduit_window_held"] = bool(held)
    wait_ms = getattr(backend, "conduit_wait_ms", None)
    if isinstance(wait_ms, int):
        state["conduit_wait_ms"] = wait_ms
    hold_ms = getattr(backend, "conduit_hold_ms", 0)
    if hold_ms:
        state["conduit_hold_ms"] = int(hold_ms)
    release_reason = getattr(backend, "conduit_release_reason", "")
    if release_reason:
        state["conduit_release_reason"] = str(release_reason)
    retries = getattr(backend, "image_mainline_retries", 0)
    if retries:
        state["mainline_retries"] = retries
    return state


def _pool_diagnostics(attempted_tokens: set[str], select_kwargs: dict[str, Any]) -> dict[str, Any]:
    """失败那一刻的号池现场（几个号可用、几个在忙）。测试桩没有这个方法，跳过。

    专门用来区分「上游慢」和「号池空了」：轮询超时之后选不到号，最终报的也是超时，
    光看错误文案分不出来。
    """
    getter = getattr(account_service, "image_pool_diagnostics", None)
    if not callable(getter):
        return {}
    try:
        pool = getter(attempted_tokens, **select_kwargs)
    except Exception:
        return {}
    return pool if isinstance(pool, dict) else {}


# 生图链路各阶段的中文名 + 「这一步在干嘛」。日志详情按阶段逐条展示，
# 用户能直接看出时间花在哪一步，不用再拿 phase_*_ms 去猜。
IMAGE_PHASE_TEXT: dict[str, tuple[str, str]] = {
    "uploading": ("上传参考图", "把图生图的参考图上传到上游"),
    "bootstrapping": ("预热会话", "请求会话首页、解析 PoW 脚本；每个 backend 只做一次"),
    "getting_token": ("获取 sentinel token", "上游 chat-requirements 的 prepare/finalize 两步"),
    "preparing_conversation": ("申请 conduit token", "POST conversation/prepare，领取一次性主链路凭据"),
    "starting_generation": ("发送主链路", "POST conversation；上游在这里回 skipped_mainline 或开始生成"),
    "generating": ("等待上游生成", "主链路已被接收，读 SSE 等图（通常是最长的一段）"),
    "stream_done": ("SSE 读取结束", "主链路流结束，开始解析结果"),
    "conversation_start": ("解析对话事件", "从 SSE 事件里提取图片文件和对话状态"),
    "conversation_done": ("解析对话完成", "对话事件解析结束"),
    "poll_start": ("开始轮询取图", "SSE 没直接给出图片，改为查询上游任务结果"),
    "poll_done": ("轮询取图结束", "轮询拿到（或放弃）图片结果"),
    "download_start": ("下载图片", "从上游取回图片字节"),
    "download_done": ("下载完成", "图片字节已取回"),
}


def _append_phase_timeline(mark: Any, backend: Any) -> None:
    """把 backend 记下的阶段打点按真实时间插进执行时间线。

    `image_phases` 里每个阶段只存首次到达的 monotonic 时刻；相邻两个打点之差
    就是上一段的耗时。最后一段算到「现在」（这次尝试收尾的时刻），
    这样卡在某个阶段中间的失败也能看到那一段花了多久。
    """
    phases = getattr(backend, "image_phases", None)
    if not isinstance(phases, dict) or not phases:
        return
    ordered = sorted(phases.items(), key=lambda item: item[1])
    now = time.monotonic()
    for position, (name, at) in enumerate(ordered):
        end = ordered[position + 1][1] if position + 1 < len(ordered) else now
        label, detail = IMAGE_PHASE_TEXT.get(name, (name, ""))
        mark(
            f"phase_{name}", label, detail,
            started=at, duration_ms=int(max(0.0, end - at) * 1000),
        )


def _image_phases(backend: Any) -> dict[str, Any]:
    """本次尝试各阶段的耗时（上传/握手/主链路/轮询），失败时用来定位卡点。

    假 backend（测试桩）没有这个方法，静默跳过。
    """
    if backend is None:
        return {}
    getter = getattr(backend, "image_phase_diagnostics", None)
    if not callable(getter):
        return {}
    try:
        phases = getter()
    except Exception:
        return {}
    return phases if isinstance(phases, dict) else {}


def _image_resolve_diagnostics(backend: Any) -> dict[str, Any]:
    """取图阶段的现场：SSE 给了哪些 id、直接解析是否落空、回退轮询等了多久。

    失败时这一份会随 image_diagnostics 一起进调用日志，导出就能看到
    「图到底是没生成，还是生成了但没取回来」。
    """
    if backend is None:
        return {}
    data = getattr(backend, "image_resolve_diagnostics", None)
    if not isinstance(data, dict) or not data:
        return {}
    return dict(data)


# 上游 SSE 最近几条原始事件：失败时随诊断/异常 body 进调用日志。
# 之前只记录解析后的 state.text，上游到底发了什么（HTTP 错误被 SSE 包装、patch
# 事件把报错塞进正文等）在日志里完全看不到。条数和长度都要截断，避免刷爆日志。
IMAGE_STREAM_TAIL_LIMIT = 6
IMAGE_STREAM_TAIL_CHARS = 600
# 异常 body 会被 describe_exception 截到 2000 字符；放 2 条刚好能完整看到上游原文。
IMAGE_STREAM_TAIL_IN_BODY = 2


def _remember_stream_event(backend: Any, event: dict[str, Any]) -> None:
    """把一条上游 SSE 原始事件留在 backend 上（只保留最近几条）。

    `conversation_events` 已经把 raw/payload 挂在事件上：delta/event 带 raw，
    非 JSON 的原始行带 payload。这里只做截断和去重入队，异常一律吞掉。
    """
    if not isinstance(event, dict):
        return
    raw = event.get("raw")
    if raw is None:
        raw = event.get("payload")
    if raw is None:
        return
    try:
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(raw)
    text = text.strip()
    if not text:
        return
    if len(text) > IMAGE_STREAM_TAIL_CHARS:
        text = text[:IMAGE_STREAM_TAIL_CHARS] + "…[truncated]"
    tail = getattr(backend, "image_stream_tail", None)
    if not isinstance(tail, list):
        tail = []
        try:
            backend.image_stream_tail = tail
        except Exception:
            return
    upstream_type = ""
    if isinstance(raw, dict):
        upstream_type = str(raw.get("type") or "")
    tail.append({
        "type": str(event.get("type") or ""),
        "upstream_type": upstream_type,
        "payload": text,
    })
    del tail[:-IMAGE_STREAM_TAIL_LIMIT]


def _image_stream_tail(backend: Any, limit: int = IMAGE_STREAM_TAIL_LIMIT) -> list[dict[str, Any]]:
    if backend is None:
        return []
    tail = getattr(backend, "image_stream_tail", None)
    if not isinstance(tail, list) or not tail:
        return []
    return [dict(item) for item in tail[-max(1, limit):] if isinstance(item, dict)]


def _image_stream_summary(backend: Any) -> dict[str, Any]:
    """主链路结束时的摘要：有没有调起图片工具、SSE 带回了几个图片 id。"""
    if backend is None:
        return {}
    summary = getattr(backend, "image_stream_summary", None)
    return dict(summary) if isinstance(summary, dict) else {}


def _image_stream_diagnostics(backend: Any) -> dict[str, Any]:
    """上游 SSE 尾部现场，失败时随 image_diagnostics 一起进调用日志。"""
    diagnostics: dict[str, Any] = {}
    tail = _image_stream_tail(backend)
    if tail:
        diagnostics["stream_tail"] = tail
    summary = _image_stream_summary(backend)
    if summary:
        # `at` 是 monotonic 时间戳，只给时间线排序用，导出日志里没有意义。
        diagnostics["stream_summary"] = {key: value for key, value in summary.items() if key != "at"}
    return diagnostics


def _plumbing_message_body(text: str) -> dict[str, Any] | None:
    """判断 assistant 正文是不是上游管道错误；是则返回可挂到异常上的 body。

    典型形态：上游 400 ``{"skipped_mainline":true}`` 被 SSE patch 进了 assistant
    文本。这种文本不是模型写的，不能按 `text_reply` 换号重试，也不能当内容策略
    拒绝。工具参数 JSON（``{"size":...,"n":...}``）和带 referenced_image_ids 的
    文本是「模型没触发工具」的正常回复，交给原来的轮询/重试逻辑处理。
    """
    value = str(text or "").strip()
    if not value:
        return None
    if SKIPPED_MAINLINE_RE.search(value):
        parsed: Any = None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("skipped_mainline") is True:
            return parsed
        return {"skipped_mainline": True, "upstream_response": value[:1000]}
    if TOOL_PARAMS_JSON_RE.search(value) or REFERENCED_IMAGE_IDS_RE.search(value):
        return None
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return {"upstream_response": parsed}
    if UPSTREAM_PLUMBING_RE.search(value):
        return {"upstream_response": value[:1000]}
    return None


def _attach_stream_context(body: dict[str, Any], backend: Any) -> dict[str, Any]:
    """给异常 body 补上「上游原文 + 最近几条 SSE 事件」，日志详情直接可见。"""
    tail = _image_stream_tail(backend, IMAGE_STREAM_TAIL_IN_BODY)
    if tail:
        body["stream_tail"] = tail
    return body


def _start_image_attempt(diagnostics: dict[str, Any], token: str, account_email: str) -> dict[str, Any]:
    """开一条尝试记录：这次用的是哪个号、同账号在途多少条。

    token 可以为空——「一个号都没选出来」本身也是一次尝试，也得进轨迹。
    """
    inflight = account_service.image_inflight_count(token) if token else 0
    attempt: dict[str, Any] = {
        "n": len(diagnostics.get("attempts") or []) + 1,
        "account_email": account_email,
        "request_token": anonymize_token(token) if token else "",
        "image_inflight": inflight,
        "image_peers": max(0, inflight - 1),
        "started_ts": round(time.time(), 2),
    }
    diagnostics.setdefault("attempts", []).append(attempt)
    return attempt


def _finish_image_attempt(attempt: dict[str, Any] | None, outcome: str, error: str = "", **extra: Any) -> None:
    """给当前尝试收尾：结局、耗时、附加现场。同一个字典只写一次。"""
    if not attempt:
        return
    attempt["outcome"] = outcome or "unknown"
    started = float(attempt.get("started_ts") or 0.0)
    if started:
        attempt["dur_ms"] = int(max(0.0, time.time() - started) * 1000)
    if error:
        attempt["error"] = str(error)[:200]
    attempt.update(extra)


def _attempts_summary(attempts: list[dict[str, Any]]) -> str:
    """把尝试轨迹压成一行：试了几个号、各自卡在哪一步、分别花了多久。"""
    parts: list[str] = []
    for attempt in attempts:
        bits = [f"#{attempt.get('n')} {attempt.get('account_email') or '?'} {attempt.get('outcome') or 'unknown'}"]
        marks: list[str] = []
        for key, label in (
            ("poll_reason", "poll"),
            ("poll_attempts", "polls"),
            ("poll_last_upstream_status", "last_status"),
            ("mainline_retries", "mainline_retries"),
            ("image_peers", "peers"),
        ):
            if attempt.get(key):
                marks.append(f"{label}={attempt[key]}")
        if attempt.get("poll_elapsed_secs"):
            marks.append(f"poll_elapsed={attempt['poll_elapsed_secs']}s")
        if attempt.get("conduit_wait_ms"):
            marks.append(f"conduit_wait={attempt['conduit_wait_ms']}ms")
        # 抢没抢到 conduit 窗口是 skipped_mainline 的直接证据，单独标一个 short tag。
        if attempt.get("conduit_acquired") is False:
            marks.append("conduit=miss")
        if marks:
            bits.append("(" + ",".join(marks) + ")")
        if attempt.get("dur_ms") is not None:
            bits.append(f"{attempt['dur_ms']}ms")
        if attempt.get("error"):
            bits.append(str(attempt["error"])[:80])
        parts.append(" ".join(bits))
    return " → ".join(parts)[:600]


def _attach_attempt_trail(outputs: list[ImageOutput], diagnostics: dict[str, Any]) -> None:
    """成功返回时把换号轨迹挂到结果上，调用日志才能在同一条记录里展示。

    只有真的换号重试过（尝试次数 > 1）才挂；没重试的请求保持原来的日志形态。
    失败路径不需要——异常上已经带着同一份 image_diagnostics。
    """
    attempts = [item for item in (diagnostics.get("attempts") or []) if isinstance(item, dict)]
    if len(attempts) <= 1:
        return
    trail = [str(item.get("account_email") or "").strip() for item in attempts]
    trail = [email for email in trail if email]
    if not trail:
        return
    # 成功分支的 record_attempt_state 只补 outcome、不刷新 attempts_summary，
    # diagnostics 里存的那份可能停在上一次失败尝试上，这里按最新 attempts 重算。
    summary = _attempts_summary(attempts)
    # 结构化现场只挂到最终 chunk（result/message）：progress chunk 每次增量都带
    # 一份全量 attempts 会把流式响应撑大，日志层也会重复收集。
    for output in outputs:
        output.account_trail = list(trail)
        output.attempts_summary = summary
        if output.kind in ("result", "message"):
            output.attempts = [
                {**dict(item), "image_index": output.index}
                for item in attempts
            ]


def _attach_image_timeline(outputs: list[ImageOutput], diagnostics: dict[str, Any]) -> None:
    """成功返回时把执行时间线挂到结果上（失败时由异常带着 diagnostics 出去）。

    和换号轨迹不同，时间线每次生图都要有：用户要能看到每一步做了什么、
    花了多久、因为什么花了这么久，哪怕一次就成功。
    """
    steps = diagnostics.get("timeline")
    if not isinstance(steps, list) or not steps:
        return
    for output in outputs:
        output.image_timeline = list(steps)


def _exception_image_timeline_group(exc: BaseException, index: int) -> dict[str, Any] | None:
    """从失败异常上取回这张图的时间线，多图并行收尾时并进成功 chunck 的日志。

    异常可能被外层包过一层，所以沿 __cause__/__context__ 链找第一份 image_diagnostics。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostics = getattr(current, "image_diagnostics", None)
        if isinstance(diagnostics, dict):
            steps = diagnostics.get("timeline")
            if isinstance(steps, list) and steps:
                return {
                    "index": diagnostics.get("index") or index,
                    "steps": [step for step in steps if isinstance(step, dict)],
                }
        current = current.__cause__ or current.__context__
    return None


def _generate_single_image(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。

    抛出的异常都会带上 image_trace（本次请求的关联 id）和最后一份失败诊断
    image_diagnostics，日志详情/一键导出据此就能直接看到现场，不用去翻容器日志。
    """
    trace_id = f"{request.trace_id}-{index}" if request.trace_id else new_trace_id()
    # 诊断在里层现算现写进这个 dict；不管最后从哪一行抛出，都在这里统一挂到异常上。
    diagnostics: dict[str, Any] = {}
    try:
        outputs = _generate_single_image_attempts(request, index, total, trace_id, diagnostics)
    except BaseException as exc:
        if not getattr(exc, "image_trace", ""):
            setattr(exc, "image_trace", trace_id)
        if diagnostics and not getattr(exc, "image_diagnostics", None):
            setattr(exc, "image_diagnostics", dict(diagnostics))
        raise
    # 成功返回没有异常可以承载诊断，轨迹和时间线直接挂在结果上给调用日志看。
    _attach_attempt_trail(outputs, diagnostics)
    _attach_image_timeline(outputs, diagnostics)
    return outputs


def _generate_single_image_attempts(
        request: ConversationRequest,
        index: int,
        total: int,
        trace_id: str,
        diagnostics: dict[str, Any],
) -> list[ImageOutput]:
    # TLS 连接错误最大重试次数
    MAX_TLS_RETRIES = 3
    # 连接超时错误最大重试次数（同账号短等待重试）
    MAX_CONN_TIMEOUT_RETRIES = 3

    text_reply_retry_count = 0
    tls_retry_count = 0
    conn_timeout_retry_count = 0
    poll_timeout_retry_count = 0
    # 设置页的「生成失败重试次数」：只对普通生图失败/token 失效/轮询超时生效，
    # 每次重试都会排除已经失败过的账号（attempted_tokens），也就是换号重来。
    generation_retry_limit = config.image_generation_retry_count
    generation_retry_count = 0
    account_email = ""
    attempted_tokens: set[str] = set()
    # 选号时发现 conduit 窗口正被占的账号：本次请求里先别再选它，直接换号。
    # 只在「窗口忙」期间排除，不写进 attempted_tokens（这个号并没有失败）。
    conduit_busy_tokens: set[str] = set()
    conduit_busy_switches = 0
    # 号池整体都在握手时关掉探测，避免在「换号 → 还是忙」之间空转。
    conduit_probe_enabled = True
    conduit_probe_skip_logged = False
    last_transient_error = False
    transient_attempts = 0
    rescue_deadline = 0.0
    rescue_attempts = 0

    # 本次请求拿到的总预算（秒）。生图失败时「预算还剩多少」比「跑了多久」更能说明问题：
    # 上传 + 握手 + 主链路 SSE 都在这一个预算里扣，扣完轮询就没时间了。
    request_budget_secs = (
        max(0.0, float(request.deadline) - time.monotonic()) if request.deadline is not None else None
    )
    # 整次请求（含所有换号重试）共用同一份预算：重试只在剩余时间里跑，不续期。
    # 以前每次重试都续满一份新预算，单次 120s + 重试 3 次最坏要等 480s
    # （线上实测 489s），还把号池里的号全烧了一遍；而同样的慢生成重试几次
    # 并不会变快。需要更长的单次等待，请在设置页调大「图片轮询超时」。
    attempt_deadline = request.deadline
    # 这次尝试真正剩下的预算。日志里的「预算剩余」用这份。
    attempt_remaining_secs: float | None = None
    # 下面这几个在循环里被反复重绑，失败快照（finally 里那个闭包）读的是当前值。
    backend: OpenAIBackendAPI | None = None
    select_kwargs: dict[str, Any] = {}
    token = ""
    attempt: dict[str, Any] | None = None
    attempt_outcome = "unknown"
    attempt_error = ""
    attempt_extra: dict[str, Any] = {}
    # 执行时间线：每一步「做了什么、什么时候开始、花了多久、为什么」。
    # 成功时挂到结果上、失败时随 diagnostics 进异常，最终都写进调用日志详情。
    request_started = time.monotonic()
    timeline: list[dict[str, Any]] = []
    diagnostics["timeline"] = timeline

    def mark_timeline(
            step: str,
            label: str,
            detail: str = "",
            *,
            started: float | None = None,
            duration_ms: int | None = None,
            **fields: Any,
    ) -> None:
        at = time.monotonic() if started is None else started
        entry: dict[str, Any] = {
            "at_ms": int(max(0.0, at - request_started) * 1000),
            "step": step,
            "label": label,
        }
        if duration_ms is not None:
            entry["dur_ms"] = int(max(0, duration_ms))
        if detail:
            entry["detail"] = str(detail)[:400]
        for key, value in fields.items():
            if value is not None and value != "":
                entry[key] = value
        timeline.append(entry)

    mark_timeline(
        "request_start",
        "请求开始",
        (
            f"预算 {request_budget_secs:.0f}s；配置重试 {generation_retry_limit} 次；第 {index}/{total} 张"
            if request_budget_secs is not None
            else f"未设置预算；配置重试 {generation_retry_limit} 次；第 {index}/{total} 张"
        ),
        budget_secs=round(request_budget_secs, 1) if request_budget_secs is not None else None,
        index=index,
        total=total,
    )

    def record_attempt_state() -> None:
        """把「刚失败/刚结束的是哪一次尝试、卡在哪、号池当时什么状况」写进 diagnostics。

        以前这份现场只在通用异常分支里刷新，最终错误只要来自别的分支（轮询超时、
        deadline、选号失败），导出的诊断就停留在上一次尝试上——账号邮箱都对不上号。
        现在挂在 try 的 finally 上，任何出口都会刷新，且带尝试序号。
        """
        if attempt_outcome == "success":
            # 成功路径没有异常带着 diagnostics 出去，这份快照注定被丢掉；
            # 跳过号池快照（要抢号池锁）省点开销。之后的尝试失败时会重新算。
            _finish_image_attempt(attempt, attempt_outcome, attempt_error)
            return
        conduit = _conduit_state(backend)
        # 握手/主链路现场同时挂到这一条尝试记录上，尝试轨迹那一行才有东西可写。
        _finish_image_attempt(attempt, attempt_outcome, attempt_error, **conduit, **attempt_extra)
        # 换号重试在异常分支里就已经续了预算，这里读 attempt_deadline 会永远显示
        # 「剩余一整个预算」；优先用续期前记下的真实剩余。
        remaining = (
            attempt_remaining_secs if attempt_remaining_secs is not None
            else remaining_image_deadline_secs(attempt_deadline)
        )
        inflight = account_service.image_inflight_count(token) if token else 0
        handshake = account_service.image_handshake_diagnostics(token) if token else {}
        pool = _pool_diagnostics(attempted_tokens, select_kwargs)
        # 取图现场属于「这一次 backend 实例」。换号重试时新 backend 还没走到取图，
        # 要清掉上一轮的 resolve_* 字段，否则导出的会对不上号。
        resolve_diag = _image_resolve_diagnostics(backend)
        if resolve_diag:
            resolve_diag["resolve_attempt_n"] = (attempt or {}).get("n")
        else:
            for key in [key for key in diagnostics if key.startswith("resolve_")]:
                diagnostics.pop(key, None)
        # 上游 SSE 尾部：只在最后一次尝试的 backend 上；换号后新 backend 还没流，
        # 清掉上一轮的，避免把上一次的上游响应误当成这一次的。
        stream_diag = _image_stream_diagnostics(backend)
        if stream_diag:
            if stream_diag.get("stream_tail"):
                diagnostics["stream_tail"] = stream_diag["stream_tail"]
            else:
                diagnostics.pop("stream_tail", None)
            if stream_diag.get("stream_summary"):
                diagnostics["stream_summary"] = stream_diag["stream_summary"]
            else:
                diagnostics.pop("stream_summary", None)
        else:
            diagnostics.pop("stream_tail", None)
            diagnostics.pop("stream_summary", None)
        # 号池里的账号类型/来源也带上：免费号和订阅号在同一种失败里的处置不同，
        # 排查时不用再回账号页对照。
        get_account = getattr(account_service, "get_account", None)
        account_info = (get_account(token) if callable(get_account) and token else {}) or {}
        diagnostics.update({
            "trace_id": trace_id,
            "index": index,
            # 顶层账号取「本次尝试」的号：以前这里写的是循环变量，导出的现场
            # 常常是上一次尝试、另一个账号，跟 detail.account_email 对不上。
            "account_email": (attempt or {}).get("account_email", account_email),
            "account_type": str(account_info.get("type") or ""),
            "account_source_type": str(account_info.get("source_type") or ""),
            "request_token": anonymize_token(token) if token else "",
            "image_inflight": inflight,
            "image_peers": max(0, inflight - 1),
            "rescue": rescue_deadline > 0,
            "poll_timeout_retries": poll_timeout_retry_count,
            # 这次尝试前因为「同号握手窗口被占」直接换掉的号数。>0 说明同号并发
            # 确实在抢窗口，但这些换号是瞬时的，不花预算。
            "conduit_busy_switches": conduit_busy_switches,
            "generation_retry_count": generation_retry_count,
            "generation_retry_limit": generation_retry_limit,
            "attempts": list(diagnostics.get("attempts") or []),
            "attempts_summary": _attempts_summary(diagnostics.get("attempts") or []),
            **conduit,
            **_image_phases(backend),
            **resolve_diag,
            **handshake,
            **pool,
        })
        if request_budget_secs is not None:
            diagnostics["request_deadline_secs"] = round(request_budget_secs, 1)
            diagnostics["request_remaining_secs"] = round(remaining, 1) if remaining is not None else None
            diagnostics["request_deadline_expired"] = remaining == 0.0

    def schedule_generation_retry(failure: ImageGenerationError) -> bool:
        """按设置里的重试次数决定是否换号重来。返回 True 时调用方应 `continue`。

        只认 RETRYABLE_IMAGE_FAILURE_MESSAGES 里的三类报错，以及「不支持的应用
        响应」；内容违规、额度不足、渠道拥堵等都不在这里重试。应用响应也占用
        同一份「生成失败重试」次数：设置几次就最多换几个号。

        重试共用本次请求的剩余预算，不续期：本地超时通常正好把预算耗尽，
        此时直接返回 False，避免把同样慢的生成按重试次数重做一遍。
        """
        nonlocal generation_retry_count
        if generation_retry_limit <= 0 or generation_retry_count >= generation_retry_limit:
            return False
        unsupported = getattr(failure, "code", "") == UNSUPPORTED_APP_RESPONSE_CODE
        if not unsupported and not is_retryable_image_failure(str(failure)):
            return False
        if image_deadline_expired(attempt_deadline):
            diagnostics["retry_stopped"] = "request_deadline"
            mark_timeline(
                "retry_stopped",
                "重试停止",
                (
                    f"本次尝试失败后预算已耗尽，不再换号重试"
                    f"（已用 {generation_retry_count}/{generation_retry_limit} 次）"
                ),
                retry_count=generation_retry_count,
                retry_limit=generation_retry_limit,
                reason="request_deadline",
            )
            return False
        generation_retry_count += 1
        logger.warning({
            "event": "image_generation_failure_retry",
            "trace_id": trace_id,
            "request_token": anonymize_token(token) if token else "",
            "account_email": account_email,
            "retry_count": generation_retry_count,
            "retry_limit": generation_retry_limit,
            "index": index,
            "error": str(failure)[:200],
        })
        # 本地超时和上游报错分开措辞：前者是「没在预算内等到图」，后者才是渠道问题。
        timeout_failure = str(failure).strip() in LOCAL_TIMEOUT_FAILURE_MESSAGES
        if unsupported:
            reason = "上游回了应用/文本响应，没有产出图片"
            action = f"按设置换号重试第 {generation_retry_count}/{generation_retry_limit} 次"
        elif timeout_failure:
            reason = (
                f"本次尝试超时：{request_budget_secs:.0f}s 预算内没等到上游图片"
                if request_budget_secs
                else "本次尝试超时：预算内没等到上游图片"
            )
            action = f"换号重试第 {generation_retry_count}/{generation_retry_limit} 次"
        else:
            reason = f"上游返回可重试错误（{str(failure)[:80]}）"
            action = f"按设置换号重试第 {generation_retry_count}/{generation_retry_limit} 次"
        remaining = remaining_image_deadline_secs(attempt_deadline)
        mark_timeline(
            "retry",
            "应用响应换号" if unsupported else "换号重试",
            (
                f"{reason}；{action}"
                + (f"，用剩余 {remaining:.0f}s 重试" if remaining is not None else "")
            ),
            retry_count=generation_retry_count,
            retry_limit=generation_retry_limit,
            error=str(failure)[:200],
        )
        return True

    while True:
        attempt_remaining_secs = None
        in_rescue = rescue_deadline > 0
        # 上一次尝试如果回过应用/文本响应，别把它的原因带到这一次：换号后的最终
        # 失败可能完全是另一类（选号失败、内容策略等）。每次尝试的原文仍在 attempts 里。
        diagnostics.pop("unsupported_reason", None)
        try:
            ensure_image_deadline(attempt_deadline)
        except ImageGenerationError:
            diagnostics["retry_stopped"] = "request_deadline"
            raise
        if in_rescue:
            rescue_started = time.monotonic()
            if not transient_rescue_wait(rescue_deadline, rescue_attempts, attempt_deadline):
                diagnostics["retry_stopped"] = "rescue_window_exhausted"
                ensure_image_deadline(attempt_deadline)
                raise ImageGenerationError(CHANNEL_BUSY_MESSAGE, account_email=account_email)
            rescue_attempts += 1
            mark_timeline(
                "rescue_wait",
                "抢救窗口等待",
                f"上游连续抖动，第 {rescue_attempts} 次抢救：短暂等待后换号再试",
                started=rescue_started,
                duration_ms=int((time.monotonic() - rescue_started) * 1000),
            )
        backend = None
        attempt = None
        attempt_outcome = "unknown"
        attempt_error = ""
        attempt_extra = {}
        # token 一起清空：选号失败时若留着上一轮的 token，快照会把上一个号的
        # 并发数/握手状态算到这一次头上。
        token = ""
        select_started = time.monotonic()
        selected_email = ""
        try:
            if request.progress_callback:
                request.progress_callback("getting_account")
            plan_type, _ = split_image_model(request.model)
            codex_model = is_codex_image_model(request.model)
            select_kwargs = {
                "plan_type": plan_type,
                "source_type": "codex" if codex_model else None,
                "plan_types": ("plus", "team", "pro") if codex_model and not plan_type else None,
            }
            # 抢救窗口里优先避开已经失败过的账号；只有当别的账号一个都接不了
            # （号池全忙/全被排除）才放开排除，退化成原来的行为。
            # 另外避开「刚才 conduit 窗口被占」的号——它们没有失败，只是这一刻发不出去。
            select_started = time.monotonic()
            excluded = attempted_tokens | conduit_busy_tokens
            if excluded and not account_service.has_available_image_account(excluded, **select_kwargs):
                if in_rescue:
                    excluded = set()
                elif conduit_busy_tokens:
                    # 别的号都失败过/没额度了，只剩窗口忙的号：再换也是空转，
                    # 清掉窗口忙的排除，回到「创建 backend 后等窗口」的行为。
                    conduit_busy_tokens.clear()
                    conduit_probe_enabled = False
                    excluded = attempted_tokens
                    diagnostics["conduit_busy_fallback"] = "no_other_account"
                    mark_timeline(
                        "conduit_window", "握手窗口",
                        f"排除后已经选不出别的号了：停止换号，回到排队等窗口的行为"
                        f"（最多等 min(剩余预算, {CONDUIT_WINDOW_TIMEOUT_SECS:.0f}s)）",
                        outcome="fallback", reason="no_other_account",
                    )
            token = account_service.get_available_access_token(
                excluded,
                deadline=attempt_deadline,
                **select_kwargs,
            )
            selected_account = account_service.get_account(token) or {}
            selected_email = str(selected_account.get("email") or "").strip()
            selected_inflight = account_service.image_inflight_count(token)
            select_bits = []
            if attempted_tokens:
                select_bits.append(f"跳过 {len(attempted_tokens)} 个已失败号")
            if conduit_busy_tokens:
                select_bits.append(f"跳过 {len(conduit_busy_tokens)} 个窗口忙的号")
            if in_rescue:
                select_bits.append("抢救窗口")
            mark_timeline(
                "select_account", "选号",
                f"选中 {selected_email or anonymize_token(token)}"
                + (f"（{'，'.join(select_bits)}）" if select_bits else "")
                + f"；该号在途 {selected_inflight} 条",
                started=select_started,
                duration_ms=int((time.monotonic() - select_started) * 1000),
                account_email=selected_email,
                image_inflight=selected_inflight,
                peers=max(0, selected_inflight - 1),
            )
            # 选号之后立刻非阻塞地探一下这个号的 conduit 窗口（prepare→mainline 的
            # 串行窗口）。同号并发时窗口被占是常态，排队等窗口只会把几十秒预算耗在
            # 「等别人」上；直接把本号从本次请求的候选里排除、换下一个号，失败也一样
            # 要换号，不如当场换。窗口正常只占 1 秒出头，换号后大概率能立刻发出去。
            conduit_preacquired = False
            probe_started = time.monotonic()
            if conduit_probe_enabled and not codex_model:
                if account_service.acquire_image_handshake(token, timeout=0):
                    conduit_preacquired = True
                    mark_timeline(
                        "conduit_window", "握手窗口", "拿到窗口（零等待），选号阶段已预占",
                        started=probe_started,
                        duration_ms=int((time.monotonic() - probe_started) * 1000),
                        outcome="acquired",
                    )
                else:
                    conduit_busy_switches += 1
                    conduit_busy_tokens.add(token)
                    busy_inflight = account_service.image_inflight_count(token)
                    account_service.release_image_slot(token)
                    logger.warning({
                        "event": "image_conduit_busy_switch",
                        "trace_id": trace_id,
                        "account_email": str((account_service.get_account(token) or {}).get("email") or ""),
                        "request_token": anonymize_token(token),
                        "switch_count": conduit_busy_switches,
                        "image_inflight": busy_inflight,
                        "index": index,
                    })
                    mark_timeline(
                        "conduit_window", "握手窗口",
                        f"{selected_email or anonymize_token(token)} 的窗口正被同号其它请求占着"
                        f"（该号在途 {busy_inflight} 条）：不等待，直接换号"
                        f"（第 {conduit_busy_switches}/{MAX_CONDUIT_BUSY_SWITCHES} 次）",
                        started=probe_started,
                        duration_ms=int((time.monotonic() - probe_started) * 1000),
                        outcome="busy_switch",
                        wait_ms=0,
                        switch_count=conduit_busy_switches,
                        image_inflight=busy_inflight,
                        account_email=selected_email,
                    )
                    if conduit_busy_switches >= MAX_CONDUIT_BUSY_SWITCHES:
                        # 换了一圈都拿不到窗口，说明整个号池都在握手。再换只是空转，
                        # 停止探测、清掉排除集，后续尝试按原来的排队行为慢慢等。
                        conduit_probe_enabled = False
                        conduit_busy_tokens.clear()
                        diagnostics["conduit_busy_fallback"] = "wait_window"
                        mark_timeline(
                            "conduit_window", "握手窗口",
                            f"连续 {conduit_busy_switches} 个号的窗口都忙，说明整个号池都在握手："
                            f"停止探测，后续尝试回到排队等窗口（最多等 "
                            f"min(剩余预算, {CONDUIT_WINDOW_TIMEOUT_SECS:.0f}s)）",
                            outcome="fallback", reason="wait_window",
                        )
                    continue
            elif codex_model and not conduit_probe_skip_logged:
                conduit_probe_skip_logged = True
                mark_timeline(
                    "conduit_window", "握手窗口",
                    "codex 图片模型不走 conduit 窗口，跳过探测（不会出现同号握手等待）",
                    outcome="skipped", reason="codex_model",
                )
            if not in_rescue:
                attempted_tokens.add(token)
        except RuntimeError as exc:
            # 选号失败也要占一条尝试记录：生产上「轮询超时之后再也选不到号」最后
            # 报的也是超时文案，轨迹里少了这一条就分不清是上游慢还是号池空了。
            # 账号留空：这一步压根没选出号，写上一轮的号反而误导。
            attempt = _start_image_attempt(diagnostics, "", "")
            attempt_outcome, attempt_error = "account_select_failed", str(exc)
            mark_timeline(
                "select_account", "选号",
                f"没有可用的号：{exc}",
                started=select_started,
                duration_ms=int((time.monotonic() - select_started) * 1000),
                outcome="failed",
            )
            record_attempt_state()
            transient = last_transient_error or is_retriable_upstream_error(exc)
            poll_failure: ImageGenerationError | None = None
            if poll_timeout_retry_count > 0 and not transient:
                # 重试只花本次请求的剩余预算：先调度再检查 deadline，还有剩余时间
                # 就直接换号；预算已耗尽时 schedule_generation_retry 会返回 False，
                # 后面的 ensure_image_deadline 再抛出超时。
                poll_failure = ImageGenerationError(
                    IMAGE_POLL_TIMEOUT_MESSAGE,
                    code="upstream_timeout",
                    account_email=account_email,
                )
                if schedule_generation_retry(poll_failure):
                    continue
            ensure_image_deadline(attempt_deadline)
            if transient:
                if not in_rescue:
                    rescue_deadline = time.time() + TRANSIENT_IMAGE_RESCUE_WINDOW_SECS
                    continue
                raise ImageGenerationError(CHANNEL_BUSY_MESSAGE, account_email=account_email) from exc
            if poll_failure is not None:
                raise poll_failure from exc
            raise ImageGenerationError(
                str(exc) or "upstream completed without returning an image result",
                account_email=account_email,
            ) from exc

        emitted_for_token = False
        # 是否已经产出过最终输出（result / 非错误 message）。progress 只是在
        # 消费 SSE 时缓冲下来的增量，失败时整段丢弃、从未发给客户端；不能拿它
        # 拦住换号重试，否则真实环境里几乎每条流都会先来一串 progress，
        # 文本回复重试永远走不到。
        final_output_emitted = False
        returned_message = False
        returned_result = False
        account = account_service.get_account(token) or {}
        account_email = str(account.get("email") or "").strip()
        attempt = _start_image_attempt(diagnostics, token, account_email)
        logger.debug({
            "event": "image_account_lookup",
            "trace_id": trace_id,
            "token_prefix": token[:12] + "..." if len(token) > 12 else token,
            "account_email": account_email,
            "account_found": bool(account),
            "index": index,
        })
        try:
            ensure_image_deadline(attempt_deadline)
            backend = OpenAIBackendAPI(access_token=token, deadline=attempt_deadline)
            backend.trace_id = trace_id
            if conduit_preacquired:
                # 窗口在选号时就拿到了，交给 backend 接管；流里再抢一次会把自己锁死
                # （Lock 不可重入），也会把换号省下的时间又还回去。
                backend.adopt_conduit_window()
            if request.progress_callback:
                backend.progress_callback = request.progress_callback
            stream_fn = stream_codex_image_outputs if is_codex_image_model(request.model) else stream_image_outputs
            # 续过预算后，stream 内部的 deadline 检查（轮询兜底、sleep）也要看到新值；
            # 多图并行时线程共用同一个 request，不能直接改，复制一份传进去。
            stream_request = request if attempt_deadline == request.deadline else replace(
                request, deadline=attempt_deadline,
            )
            outputs: list[ImageOutput] = []
            retry_after_output = False
            for output in stream_fn(backend, stream_request, index, total):
                # result 已经产出说明上游生图完成，后面的耗时是本地后处理（4K 超分、
                # 落盘）。把这段时间算进上游轮询预算的话，超分慢几秒就会被误判成
                # 「当前生图任务处理超时」，还会因为 deadline 已过而不触发换号重试。
                if output.kind != "result":
                    ensure_image_deadline(attempt_deadline)
                if account_email and not output.account_email:
                    output.account_email = account_email
                if output.kind == "message" and request.message_as_error:
                    # message 输出有三种来源：
                    #   1) text_reply：上游回了正文、没出图。正文往往就是“已完成”的解说词，
                    #      不能直接当 error 返回，按文本回复分类并允许换号重试；
                    #   2) 内容策略拒绝（blocked / 对话里检测到 policy 文案）；
                    #   3) 其它上游报错（已 sanitize 成「本次生图失败，请重试。」）。
                    text = output.text or ""
                    # 上游原文完整进尝试轨迹；日志页对长文本做展开/收起，
                    # 这里不截断（截断只发生在给用户看的摘要与 error 文案上）。
                    if text.strip():
                        attempt_extra["upstream_text"] = text
                    # 正文伪装成模型文本的上游管道错误（如 {"skipped_mainline":true}）
                    # 单独一类：既不是模型正文，也不是内容策略拒绝。这里不只看
                    # output.plumbing_error，按文本兜底再判一次——旧版本产出的
                    # message 没有这个标记，但同样不能当正文重试。
                    plumbing_body = _plumbing_message_body(text) if text.strip() else None
                    if plumbing_body is None and getattr(output, "plumbing_error", False):
                        plumbing_body = {"upstream_response": text[:1000]}
                    unsupported_reason = (
                        ""
                        if plumbing_body or is_content_policy_error(text)
                        else unsupported_app_response_reason(text)
                    )
                    unsupported_app = bool(unsupported_reason)
                    text_reply = (
                        bool(output.text_reply)
                        and not plumbing_body
                        and not unsupported_app
                        and not is_content_policy_error(text)
                    )
                    policy_violation = (
                        not text_reply
                        and not plumbing_body
                        and not unsupported_app
                        and is_content_policy_error(text)
                    )
                    if unsupported_app:
                        failure = _unsupported_app_response_error(
                            text,
                            conversation_id=output.conversation_id,
                            backend=backend,
                            reason=unsupported_reason,
                        )
                        failure.account_email = account_email
                        logger.warning({
                            "event": "image_upstream_unsupported_app_response",
                            "trace_id": trace_id,
                            "conversation_id": output.conversation_id,
                            "account_email": account_email,
                            "unsupported_reason": unsupported_reason,
                            "text_chars": len(text),
                            "text_preview": text[:300],
                            "stream_tail": (failure.body or {}).get("stream_tail") or [],
                            "index": index,
                        })
                    elif text_reply:
                        failure = ImageGenerationError(
                            "Image generation failed: the upstream model returned text instead of "
                            "an image. Please try again later.",
                            status_code=502,
                            error_type="server_error",
                            code="upstream_text_reply",
                            account_email=account_email,
                            conversation_id=output.conversation_id,
                        )
                        # 模型原文只进日志（upstream_error[].body），不当作面向用户的错误文案。
                        failure.body = _attach_stream_context({"upstream_text": text[:1000]}, backend)
                        logger.warning({
                            "event": "image_upstream_text_reply",
                            "trace_id": trace_id,
                            "conversation_id": output.conversation_id,
                            "account_email": account_email,
                            "text_chars": len(text),
                            "text_preview": text[:300],
                            "stream_tail": (failure.body or {}).get("stream_tail") or [],
                            "index": index,
                        })
                    elif plumbing_body is not None:
                        failure = ImageGenerationError(
                            IMAGE_RETRY_MESSAGE,
                            status_code=502,
                            error_type="server_error",
                            code="upstream_error",
                            account_email=account_email,
                            conversation_id=output.conversation_id,
                        )
                        failure.body = _attach_stream_context(plumbing_body, backend)
                        logger.warning({
                            "event": "image_upstream_message_plumbing",
                            "trace_id": trace_id,
                            "conversation_id": output.conversation_id,
                            "account_email": account_email,
                            "text_chars": len(text),
                            "text_preview": text[:300],
                            "stream_tail": (failure.body or {}).get("stream_tail") or [],
                            "index": index,
                        })
                    else:
                        failure = ImageGenerationError(
                            text or "Image generation was rejected by upstream policy.",
                            status_code=400,
                            error_type="invalid_request_error",
                            code="content_policy_violation",
                            account_email=account_email,
                            conversation_id=output.conversation_id,
                        )
                        logger.warning({
                            "event": "image_upstream_message_error",
                            "trace_id": trace_id,
                            "conversation_id": output.conversation_id,
                            "policy_violation": policy_violation,
                            "text_chars": len(text),
                            "text_preview": text[:300],
                            "index": index,
                        })
                    if unsupported_app:
                        # 应用/文本响应不是账号故障，也不按普通失败处理：直接交给
                        # 外层 ImageUnsupportedAppResponseError 分支统一换号重试，
                        # 避免这里先给账号记一次失败、又消耗两次重试额度。
                        raise failure
                    if schedule_generation_retry(failure):
                        account_service.mark_image_result(token, False)
                        # 日志里展示上游原生报错，而不是本地兜底文案「本次生图失败，请重试。」
                        # （面向用户的 error 响应仍由 failure 决定，不受这里影响）。
                        attempt_outcome = "message_retry"
                        attempt_error = text.strip() or str(failure)
                        retry_after_output = True
                        break
                    raise failure
                emitted_for_token = True
                final_output_emitted = final_output_emitted or output.kind in ("message", "result")
                returned_message = output.kind == "message"
                returned_result = returned_result or output.kind == "result"
                outputs.append(output)
            if retry_after_output:
                continue
            if returned_result:
                # 成功日志也记一份主链路摘要：之后排查「为什么图有了但流里没 id」
                # 或「这轮到底有没有调起图片工具」时，不用再去翻容器日志。
                summary = _image_stream_summary(backend)
                if summary:
                    tool_invoked = summary.get("tool_invoked")
                    turn_use_case = str(summary.get("turn_use_case") or "").strip()
                    plan_type = str(summary.get("plan_type") or "").strip()
                    mark_timeline(
                        "stream_summary", "上游流结果",
                        f"tool_invoked={tool_invoked if tool_invoked is not None else '?'}；"
                        f"turn_use_case={turn_use_case or '?'}；"
                        f"plan_type={plan_type or '?'}；"
                        f"图片 id {summary.get('file_ids', 0)} 个 / sediment {summary.get('sediment_ids', 0)} 个",
                        started=summary.get("at"),
                        tool_invoked=tool_invoked,
                        turn_use_case=turn_use_case,
                        plan_type=plan_type,
                        product_experience=summary.get("product_experience"),
                        app_ui_marker=summary.get("app_ui_marker"),
                        conversation_id=summary.get("conversation_id"),
                        file_ids=summary.get("file_ids"),
                        sediment_ids=summary.get("sediment_ids"),
                    )
            if returned_message:
                account_service.mark_image_result(token, False)
                attempt_outcome = "message"
                return outputs
            if not returned_result:
                account_service.mark_image_result(token, False)
                if emitted_for_token:
                    conv_id = outputs[-1].conversation_id if outputs else ""
                    attempt_outcome, attempt_error = "no_image", "upstream completed without generating images"
                    raise ImageGenerationError(
                        "upstream completed without generating images",
                        status_code=400,
                        error_type="invalid_request_error",
                        code="no_image_generated",
                        account_email=account_email,
                        conversation_id=conv_id,
                    )
                attempt_outcome = "empty_stream"
                return outputs
            account_service.mark_image_result(token, True)
            attempt_outcome = "success"
            return outputs
        except ImagePollTimeoutError as exc:
            account_service.mark_image_result(token, False)
            # 「SSE 结束就没有图片工具事件」也走这条换号路径，但原因不是轮询超时，
            # 日志/轨迹里必须分开写，否则又会把「上游没开始生成」误判成「上游慢」。
            no_tool_event = isinstance(exc, ImageNoToolEventError)
            attempt_outcome = "no_tool_event" if no_tool_event else "poll_timeout"
            attempt_error = str(exc)
            # 轮询是「预算跑完了」还是「上游一直 5xx 提前超时」，两者文案一样、现场不同。
            poll_context = poll_timeout_diagnostics(exc)
            if no_tool_event:
                poll_context = {
                    "poll_reason": "no_image_tool_event",
                    "poll_attempts": 0,
                    "poll_elapsed_secs": 0,
                }
            if poll_context:
                diagnostics.update(poll_context)
                # 轮询现场挂到本次尝试上（由 finally 里的 record_attempt_state 统一收尾），
                # 换号重试的话这一段会跟着这次尝试留在轨迹里。
                attempt_extra.update(poll_context)
            if account_email:
                setattr(exc, "account_email", account_email)
            # 轮询超时 / 主链路没起图：如果还没有拿到最终消息/图片，只丢弃本账号的进度并换号重试。
            if not returned_message and not returned_result:
                poll_timeout_retry_count += 1
                # 内置的自动换号重试只能花剩余预算；预算已经耗尽时交给下面
                # 配置的「生成失败重试」——它同样只在剩余预算内生效，不会再续期。
                if poll_timeout_retry_count <= MAX_POLL_TIMEOUT_RETRIES and not image_deadline_expired(
                    attempt_deadline,
                ):
                    logger.warning({
                        "event": "image_poll_timeout_retry",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "account_email": account_email,
                        "retry_count": poll_timeout_retry_count,
                        "index": index,
                        "reason": poll_context.get("poll_reason"),
                        "no_tool_event": no_tool_event,
                        "error": str(exc)[:200],
                    })
                    retry_detail = (
                        "SSE 结束仍没有任何图片 id / 图片工具事件，跳过轮询直接换号"
                        f"（内置换号第 {poll_timeout_retry_count}/{MAX_POLL_TIMEOUT_RETRIES} 次，不续预算）"
                        if no_tool_event else
                        f"轮询 {poll_context.get('poll_elapsed_secs', '?')}s 没等到图"
                        f"（查询 {poll_context.get('poll_attempts', '?')} 次，原因 "
                        f"{poll_context.get('poll_reason', '?')}）；"
                        f"内置换号重试第 {poll_timeout_retry_count}/{MAX_POLL_TIMEOUT_RETRIES} 次，不续预算"
                    )
                    mark_timeline(
                        "retry", "主链路未出图换号" if no_tool_event else "轮询超时换号",
                        retry_detail,
                        retry_count=poll_timeout_retry_count,
                        retry_limit=MAX_POLL_TIMEOUT_RETRIES,
                        reason=str(poll_context.get("poll_reason") or ""),
                    )
                    continue
                logger.warning({
                    "event": "image_poll_timeout_exhausted_retries",
                    "trace_id": trace_id,
                    "request_token": anonymize_token(token),
                    "account_email": account_email,
                    "retry_count": poll_timeout_retry_count,
                    "index": index,
                    "deadline_expired": image_deadline_expired(attempt_deadline),
                })
            failure = ImageGenerationError(
                IMAGE_POLL_TIMEOUT_MESSAGE,
                code="upstream_timeout",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
            )
            if schedule_generation_retry(failure):
                continue
            raise failure from exc
        except ImageUnsupportedAppResponseError as exc:
            # 上游回的是应用/文本响应（拒绝出图、Create Pet 的 DIL 等），不是账号
            # 故障，别记这个号一次失败；但响应形态跟账号、模型路由有关，换号有机会
            # 正常出图。和 token 失效一样：当前流直接中断，换号重试（不消耗设置页
            # 的「生成失败重试」次数，只花本次请求的剩余预算）。
            attempt_outcome = "unsupported_app_response"
            attempt_error = str(exc)
            account_service.release_image_slot(token)
            if account_email and not getattr(exc, "account_email", ""):
                exc.account_email = account_email
            unsupported_reason = str(getattr(exc, "unsupported_reason", "") or "")
            upstream_text = str(getattr(exc, "upstream_text", "") or "")
            if unsupported_reason:
                diagnostics["unsupported_reason"] = unsupported_reason
                attempt_extra["unsupported_reason"] = unsupported_reason
            if upstream_text:
                # 尝试轨迹里保留完整原文（日志页可展开），body 里只放 1000 字摘要。
                attempt_extra["upstream_text"] = upstream_text
            # 只有一个号（或其它号都占满槽位）时继续换只会撞上「没有可用号」，
            # 反而把真正的失败原因盖成通用错误；此时保持原来的直接报错。
            try:
                has_other_account = account_service.has_available_image_account(
                    attempted_tokens | conduit_busy_tokens, **select_kwargs,
                )
            except Exception:
                has_other_account = False
            # 重试次数遵循设置页的「生成失败重试次数」：与普通生成失败共用同一份配额。
            can_retry = bool(has_other_account) and schedule_generation_retry(exc)
            logger.warning({
                "event": (
                    "image_unsupported_app_response_retry" if can_retry
                    else "image_unsupported_app_response_final"
                ),
                "trace_id": trace_id,
                "request_token": anonymize_token(token),
                "account_email": account_email,
                "conversation_id": getattr(exc, "conversation_id", ""),
                "unsupported_reason": unsupported_reason,
                "upstream_text_chars": len(upstream_text),
                "retry_count": generation_retry_count,
                "retry_limit": generation_retry_limit,
                "has_other_account": has_other_account,
                "error": str(exc),
                "index": index,
            })
            if can_retry:
                continue
            if not has_other_account:
                diagnostics["retry_stopped"] = "no_other_account"
            elif generation_retry_limit <= 0 or generation_retry_count >= generation_retry_limit:
                diagnostics["retry_stopped"] = "generation_retry_limit"
            raise
        except ImageContentPolicyError as exc:
            attempt_outcome, attempt_error = "content_policy", str(exc)
            account_service.mark_image_result(token, False)
            logger.warning({
                "event": "image_stream_content_policy_error",
                "trace_id": trace_id,
                "request_token": anonymize_token(token),
                "account_email": account_email,
                "error": str(exc),
                "index": index,
            })
            raise ImageGenerationError(
                str(exc) or "Image generation was rejected by upstream policy.",
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
            ) from exc
        except ImageGenerationError as exc:
            attempt_outcome, attempt_error = "generation_error", str(exc)
            account_service.mark_image_result(token, False)
            if account_email and not getattr(exc, "account_email", ""):
                exc.account_email = account_email
            error_text = str(exc)
            if is_skipped_mainline_error(exc):
                # 上游把这条 turn 判成 skipped_mainline：这个号上的 conduit 状态
                # 被别的并发请求吃掉了。日志里单独标出来，别和其它 400 混在一起。
                attempt_outcome = "mainline_rejected"
                account_service.mark_image_mainline_rejected(token)
            if not final_output_emitted and is_token_auth_error(exc, error_text):
                # 尝试轨迹里别再用 generation_error 这种通用标签：这是明确的 token 失效。
                attempt_outcome = "invalid_token"
                try:
                    refreshed_token = account_service.refresh_access_token(token, force=True, event="image_stream")
                except Exception as refresh_exc:
                    refreshed_token = ""
                    logger.warning({
                        "event": "image_stream_token_refresh_failed",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "account_email": account_email,
                        "index": index,
                        "error": str(refresh_exc)[:200],
                    })
                if refreshed_token and refreshed_token != token:
                    attempt_outcome = "token_refreshed"
                    logger.warning({
                        "event": "image_stream_token_refreshed_retry",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "refreshed_token": refreshed_token,
                        "account_email": account_email,
                        "index": index,
                    })
                    mark_timeline(
                        "retry", "刷新 token 重试",
                        "上游返回 token 失效，已强制刷新 access token，同号重试（不消耗换号次数）",
                        reason="token_refreshed",
                    )
                    continue
                account_service.remove_invalid_token(token, "image_stream")
                logger.warning({
                    "event": "image_stream_invalid_token_switch_account",
                    "trace_id": trace_id,
                    "request_token": anonymize_token(token),
                    "account_email": account_email,
                    "index": index,
                    "error": error_text[:200],
                })
                mark_timeline(
                    "retry", "token 失效换号",
                    "token 刷新后仍然失效，已把该号标记失效并立即换号（不消耗配置重试次数）",
                    reason="invalid_token",
                )
                continue
            # 如果是模型返回文本而非图片，尝试换账号重试。
            # 文本回复有两种识别方式：正文里带工具参数 JSON（旧逻辑），或上游
            # 明确回了正文但轮询拿不到图（text_reply 标记，code=upstream_text_reply）。
            is_text_reply_failure = (
                getattr(exc, "code", "") == "upstream_text_reply"
                or is_model_text_reply_instead_of_image(error_text)
            )
            if is_text_reply_failure and not final_output_emitted:
                upstream_text = ""
                exc_body = getattr(exc, "body", None)
                if isinstance(exc_body, dict):
                    upstream_text = str(exc_body.get("upstream_text") or "")
                # 这不是「生成失败」，而是上游只回了正文、没有出图。尝试轨迹里
                # 单独标一类，免得所有失败都被一个 generation_error 盖住。
                text_preview = " ".join(upstream_text.split())
                attempt_outcome = "text_reply"
                attempt_error = (
                    f"上游只回了文本，没有出图（{len(upstream_text)} 字）：{text_preview[:60]}"
                    if text_preview
                    else "上游只回了文本，没有出图"
                )
                # 完整原文单独存一份，日志页的「尝试轨迹」可展开查看，不受 60 字摘要限制。
                full_text = upstream_text or error_text
                if full_text.strip():
                    attempt_extra["upstream_text"] = full_text
                text_reply_retry_count += 1
                diagnostics["text_reply"] = {
                    "chars": len(upstream_text or error_text),
                    "preview": (upstream_text or error_text)[:300],
                    "retry_count": text_reply_retry_count,
                    "code": getattr(exc, "code", ""),
                    "conversation_id": getattr(exc, "conversation_id", ""),
                }
                if (
                    text_reply_retry_count <= MAX_TEXT_REPLY_RETRIES
                    and not image_deadline_expired(attempt_deadline)
                ):
                    # 文本回复通常出现在预算快耗尽时；重试只花剩余预算，不再续期，
                    # 否则一次请求会因为正文重试被放大到好几分钟。
                    remaining = remaining_image_deadline_secs(attempt_deadline)
                    logger.warning({
                        "event": "image_model_text_reply_retry",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "account_email": account_email,
                        "retry_count": text_reply_retry_count,
                        "error_code": getattr(exc, "code", ""),
                        "error_chars": len(upstream_text or error_text),
                        "error_preview": (upstream_text or error_text)[:300],
                        "index": index,
                    })
                    mark_timeline(
                        "retry", "文本回复换号",
                        f"上游回了正文而不是图（code={getattr(exc, 'code', '')}，"
                        f"{len(upstream_text or error_text)} 字）；"
                        f"换号重试第 {text_reply_retry_count}/{MAX_TEXT_REPLY_RETRIES} 次"
                        + (f"，用剩余 {remaining:.0f}s" if remaining is not None else ""),
                        retry_count=text_reply_retry_count,
                        retry_limit=MAX_TEXT_REPLY_RETRIES,
                    )
                    continue
                if text_reply_retry_count <= MAX_TEXT_REPLY_RETRIES:
                    diagnostics["retry_stopped"] = "request_deadline"
                    mark_timeline(
                        "retry_stopped", "重试停止",
                        "文本回复重试的预算已耗尽，不再换号（总耗时封顶）",
                        retry_count=text_reply_retry_count,
                        retry_limit=MAX_TEXT_REPLY_RETRIES,
                        reason="request_deadline",
                    )
                logger.warning({
                    "event": "image_model_text_reply_exhausted_retries",
                    "trace_id": trace_id,
                    "request_token": anonymize_token(token),
                    "account_email": account_email,
                    "retry_count": text_reply_retry_count,
                    "error_preview": (upstream_text or error_text)[:300],
                    "index": index,
                })
                exhausted = ImageGenerationError(
                    "Image generation failed: the upstream model returned a text description "
                    "instead of generating an image. Please try again later.",
                    status_code=502,
                    error_type="server_error",
                    code="upstream_text_reply",
                    account_email=account_email,
                    conversation_id=getattr(exc, "conversation_id", ""),
                )
                if upstream_text:
                    exhausted.body = _attach_stream_context({"upstream_text": upstream_text[:1000]}, backend)
                raise exhausted from exc
            logger.warning({
                "event": "image_stream_generation_error",
                "trace_id": trace_id,
                "request_token": anonymize_token(token),
                "account_email": account_email,
                "error": error_text,
                "index": index,
            })
            if schedule_generation_retry(exc):
                continue
            raise
        except Exception as exc:
            account_service.mark_image_result(token, False)
            # Local handshake recovery can still exhaust its attempts. Use
            # the bounded account rescue flow with a fresh backend session;
            # never mark this 400 retryable in the generic same-POST loop.
            mainline_rejected = isinstance(exc, ImageMainlineStateError) or is_skipped_mainline_error(exc)
            transient = mainline_rejected or is_retriable_upstream_error(exc)
            attempt_outcome = "mainline_rejected" if mainline_rejected else (
                "transient_upstream" if transient else "upstream_error"
            )
            attempt_error = str(exc)
            last_error = CHANNEL_BUSY_MESSAGE if transient else str(exc)
            last_transient_error = last_transient_error or transient
            if transient:
                transient_attempts += 1
            if mainline_rejected:
                # 短时间内的新请求优先换个账号，别在同一个号上反复撞。
                account_service.mark_image_mainline_rejected(token)
            inflight = account_service.image_inflight_count(token)
            # 这批诊断既进容器日志，也写进调用方传入的 diagnostics —— 最后抛出的异常
            # 会带着它一起出去，日志页（和一键导出）里就能看到 image_peers、握手排队这些数。
            diagnostics.update({
                "trace_id": trace_id,
                "request_token": anonymize_token(token),
                "account_email": account_email,
                # 这个账号上还有几条生图请求在跑（含自己）：>0 表示同账号并发，
                # 是 skipped_mainline 最需要确认的一个数。
                "image_inflight": inflight,
                "image_peers": max(0, inflight - 1),
                "index": index,
                "transient": transient,
                "mainline_rejected": mainline_rejected,
                "transient_attempts": transient_attempts,
                "rescue": in_rescue,
                **account_service.image_handshake_diagnostics(token),
            })
            logger.warning({"event": "image_stream_fail", "error": str(exc), **diagnostics})
            if not final_output_emitted and is_token_auth_error(exc, last_error):
                # 401 / token_revoked 不走通用的 upstream_error 标签，日志里直接写清楚。
                attempt_outcome = "invalid_token"
                try:
                    refreshed_token = account_service.refresh_access_token(token, force=True, event="image_stream")
                except Exception as refresh_exc:
                    refreshed_token = ""
                    logger.warning({
                        "event": "image_stream_token_refresh_failed",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "account_email": account_email,
                        "index": index,
                        "error": str(refresh_exc)[:200],
                    })
                if refreshed_token and refreshed_token != token:
                    attempt_outcome = "token_refreshed"
                    logger.warning({
                        "event": "image_stream_token_refreshed_retry",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "refreshed_token": refreshed_token,
                        "account_email": account_email,
                        "index": index,
                    })
                    mark_timeline(
                        "retry", "刷新 token 重试",
                        "上游返回 token 失效，已强制刷新 access token，同号重试（不消耗换号次数）",
                        reason="token_refreshed",
                    )
                    continue
                account_service.remove_invalid_token(token, "image_stream")
                logger.warning({
                    "event": "image_stream_invalid_token_switch_account",
                    "trace_id": trace_id,
                    "request_token": anonymize_token(token),
                    "account_email": account_email,
                    "index": index,
                    "error": last_error[:200],
                })
                mark_timeline(
                    "retry", "token 失效换号",
                    "token 刷新后仍然失效，已把该号标记失效并立即换号（不消耗配置重试次数）",
                    reason="invalid_token",
                )
                continue
            # skipped_mainline 是 conduit 握手状态被同号并发吃掉，不是模型输出。
            # 主链路在抛 400 之前往往已经发过 message_marker / conversation.delta，
            # 消费循环会给这些 progress 置 emitted_for_token=True；但它们既不是最终
            # 结果，也不该阻止换号——否则这个 400 会掉到下面的配置重试里，而
            # 「当前渠道拥堵」文案不在可重试集合中，最后就成了不换号直接失败。
            if transient and (not emitted_for_token or mainline_rejected):
                # 重试轨迹写清楚是哪类抖动：transient 的 last_error 已经洗成
                # 「当前渠道拥堵」，skipped_mainline 的上游原文要从 attempt_error 取。
                retry_reason = "mainline_rejected" if mainline_rejected else "transient_upstream"
                retry_cause = "主链路拒绝" if mainline_rejected else "上游抖动"
                retry_detail = (attempt_error if mainline_rejected else last_error)[:80]
                if in_rescue:
                    mark_timeline(
                        "retry", f"抢救窗口内{retry_cause}换号",
                        f"{retry_cause}（{retry_detail}）；继续换号再试",
                        reason=retry_reason if mainline_rejected else "transient_rescue",
                    )
                    continue
                if transient_attempts >= MAX_INITIAL_TRANSIENT_IMAGE_ACCOUNT_ATTEMPTS:
                    rescue_deadline = time.time() + TRANSIENT_IMAGE_RESCUE_WINDOW_SECS
                    mark_timeline(
                        "rescue_enter", "进入抢救窗口",
                        f"连续 {transient_attempts} 次{retry_cause}；接下来 "
                        f"{TRANSIENT_IMAGE_RESCUE_WINDOW_SECS:.0f}s 内持续换号重试",
                        reason=retry_reason,
                    )
                    continue
                mark_timeline(
                    "retry", f"{retry_cause}换号",
                    f"{retry_cause}（{retry_detail}）；换号重试第 {transient_attempts}/"
                    f"{MAX_INITIAL_TRANSIENT_IMAGE_ACCOUNT_ATTEMPTS} 次",
                    retry_count=transient_attempts,
                    reason=retry_reason,
                )
                continue
            # TLS/SSL 连接错误：自动重试
            if not emitted_for_token and is_tls_connection_error(last_error):
                tls_retry_count += 1
                if tls_retry_count <= MAX_TLS_RETRIES:
                    logger.warning({
                        "event": "image_stream_tls_retry",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "account_email": account_email,
                        "retry_count": tls_retry_count,
                        "index": index,
                        "error": last_error[:200],
                    })
                    wait_secs = min(2.0 * tls_retry_count, 10.0)
                    mark_timeline(
                        "retry", "TLS 错误重试",
                        f"TLS/SSL 连接错误；等待 {wait_secs:.1f}s 后同号重试第 "
                        f"{tls_retry_count}/{MAX_TLS_RETRIES} 次",
                        dur_hint_ms=int(wait_secs * 1000),
                        retry_count=tls_retry_count,
                        reason="tls_error",
                    )
                    sleep_with_image_deadline(wait_secs, attempt_deadline)
                    continue
            # 连接超时错误（curl 28）：同账号短等待重试，不切换账号
            if not emitted_for_token and is_connection_timeout_error(last_error):
                conn_timeout_retry_count += 1
                if conn_timeout_retry_count <= MAX_CONN_TIMEOUT_RETRIES:
                    wait_secs = min(3.0 * conn_timeout_retry_count, 9.0)
                    logger.warning({
                        "event": "image_stream_conn_timeout_retry",
                        "trace_id": trace_id,
                        "request_token": anonymize_token(token),
                        "account_email": account_email,
                        "retry_count": conn_timeout_retry_count,
                        "index": index,
                        "wait_secs": wait_secs,
                        "error": last_error[:200],
                    })
                    mark_timeline(
                        "retry", "连接超时重试",
                        f"连接上游超时；等待 {wait_secs:.1f}s 后同号重试第 "
                        f"{conn_timeout_retry_count}/{MAX_CONN_TIMEOUT_RETRIES} 次",
                        dur_hint_ms=int(wait_secs * 1000),
                        retry_count=conn_timeout_retry_count,
                        reason="connection_timeout",
                    )
                    sleep_with_image_deadline(wait_secs, attempt_deadline)
                    continue
            if transient:
                failure = ImageGenerationError(CHANNEL_BUSY_MESSAGE, account_email=account_email, conversation_id="")
            else:
                failure = ImageGenerationError(
                    image_stream_error_message(last_error), account_email=account_email, conversation_id=""
                )
            if schedule_generation_retry(failure):
                continue
            raise failure from exc
        finally:
            # 无论这次尝试是成功、重试还是抛错，都在这里把现场落到 diagnostics。
            # 之前只在大异常分支里刷新，所以导出的 image_diagnostics 经常停留在
            # 上一次尝试、上一个账号上。
            record_attempt_state()
            conduit_hold = _conduit_state(backend) if backend is not None else {}
            if backend is not None:
                # 阶段事件的时间戳早于「当前」，先追加再统一按 at_ms 排序。
                _append_phase_timeline(mark_timeline, backend)
            if conduit_hold.get("conduit_hold_ms"):
                reason_text = {
                    "mainline_sent": "主链路已发出后正常释放",
                    "mainline_error": "主链路没发出，异常释放",
                }.get(str(conduit_hold.get("conduit_release_reason") or ""), "已释放")
                mark_timeline(
                    "conduit_release", "释放握手窗口",
                    f"窗口共持有 {conduit_hold['conduit_hold_ms']}ms（{reason_text}）；"
                    f"其余同号请求看到窗口忙会立刻换号，不会排队等",
                    duration_ms=conduit_hold["conduit_hold_ms"],
                    outcome=conduit_hold.get("conduit_release_reason") or "",
                )
            if attempt_extra:
                poll_elapsed = attempt_extra.get("poll_elapsed_secs")
                mark_timeline(
                    "poll", "轮询取图",
                    f"上游没直接给出图片，回退轮询：查询 "
                    f"{attempt_extra.get('poll_attempts', '?')} 次，耗时 "
                    f"{poll_elapsed if poll_elapsed is not None else '?'}s，原因 "
                    f"{attempt_extra.get('poll_reason', '?')}",
                    duration_ms=(
                        int(float(poll_elapsed) * 1000)
                        if isinstance(poll_elapsed, (int, float)) else None
                    ),
                    reason=str(attempt_extra.get("poll_reason") or ""),
                    attempts=attempt_extra.get("poll_attempts"),
                )
            if backend is not None:
                resolve_diag = _image_resolve_diagnostics(backend)
                if resolve_diag.get("resolve_poll"):
                    resolve_state = {
                        "hit": "命中", "empty": "没查到结果", "timeout": "超时", "error": "出错",
                    }.get(str(resolve_diag["resolve_poll"]), str(resolve_diag["resolve_poll"]))
                    mark_timeline(
                        "resolve_poll", "回退轮询取图",
                        f"SSE 没直接给出图片，查上游任务结果：{resolve_state}，耗时 "
                        f"{resolve_diag.get('resolve_poll_elapsed_ms', 0)}ms",
                        duration_ms=resolve_diag.get("resolve_poll_elapsed_ms"),
                        outcome=str(resolve_diag["resolve_poll"]),
                    )
            if attempt is not None:
                end_bits = [f"结局 {attempt_outcome or 'unknown'}"]
                remaining_secs = (
                    attempt_remaining_secs if attempt_remaining_secs is not None
                    else remaining_image_deadline_secs(attempt_deadline)
                )
                if remaining_secs is not None:
                    end_bits.append("预算已耗尽" if remaining_secs <= 0 else f"预算剩余 {remaining_secs:.1f}s")
                if attempt_error:
                    end_bits.append(f"原因 {str(attempt_error)[:160]}")
                mark_timeline(
                    "attempt_end", "尝试结束", "；".join(end_bits),
                    duration_ms=attempt.get("dur_ms"),
                    outcome=attempt_outcome or "unknown",
                    account_email=attempt.get("account_email") or "",
                    error=str(attempt_error)[:200],
                    **({"conduit_hold_ms": conduit_hold["conduit_hold_ms"]}
                       if conduit_hold.get("conduit_hold_ms") else {}),
                )
            timeline.sort(key=lambda item: item.get("at_ms", 0))
            # 选号时预抢的 conduit 窗口若还没在流里释放（比如建 backend、上传参考图
            # 之前就失败了），这里兜底放掉。正常路径在主链路发出去时已经释放，
            # 本方法只放「本线程持有的那把锁」，重复调用是空操作。
            if token:
                account_service.release_image_handshake(token)


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    if not is_supported_image_model(request.model):
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)))
    # 整个请求一个 base trace，多图时每张再拼上序号（<base>-1、<base>-2 …）。
    request.trace_id = request.trace_id or new_trace_id()

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(request, 1, 1)
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        for index in range(1, request.n + 1):
            outputs = _generate_single_image(request, index, request.n)
            for output in outputs:
                yield output
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    # 每张图片一个线程，同时启动
    futures = {}
    results: dict[int, list[ImageOutput]] = {}
    errors: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=request.n) as executor:
        for index in range(1, request.n + 1):
            future = executor.submit(_generate_single_image, request, index, request.n)
            futures[future] = index

        # 按完成顺序收集结果
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                errors[index] = exc
                logger.warning({
                    "event": "image_parallel_generation_error",
                    "index": index,
                    "error": str(exc)[:300],
                })

    # 失败的图也各有自己的执行现场：把时间线取出来，等会随成功的 chunk 一起进调用日志，
    # 否则多图部分失败时日志里只剩成功那几张，失败那张卡在哪一步完全看不到。
    failed_timelines: list[dict[str, Any]] = []
    for index, exc in sorted(errors.items()):
        group = _exception_image_timeline_group(exc, index)
        if group is not None:
            failed_timelines.append(group)

    # yield 结果：跳过索引顺序限制，不再让低索引失败阻塞高索引成功结果
    emitted = False
    last_error = ""
    first_error: Exception | None = None
    # 先 yield 所有成功的结果
    for index in range(1, request.n + 1):
        if index in results:
            for output in results[index]:
                emitted = True
                if failed_timelines and not output.failed_image_timelines:
                    output.failed_image_timelines = list(failed_timelines)
                yield output
        elif index in errors:
            last_error = str(errors[index])
            if first_error is None:
                first_error = errors[index]
            if not emitted:
                logger.warning({
                    "event": "image_parallel_failure_before_success",
                    "failed_index": index,
                    "error": last_error[:200],
                })

    # 如果有失败但也有成功，记录警告
    if emitted:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })

    if not emitted:
        if not last_error:
            last_error = "no account in the pool could generate images — check account quota and rate-limit status"
        # 这里把文案洗成了用户看得懂的说法，原始异常挂到 cause 上，日志才排得动。
        error = ImageGenerationError(image_stream_error_message(last_error), conversation_id="")
        # 全部失败时每张图的时间线都要进调用日志（异常链只能带第一张的诊断）。
        if failed_timelines:
            error.image_timeline_groups = failed_timelines
        raise error from first_error


def stream_image_chunks(outputs: Iterable[ImageOutput]) -> Iterator[dict[str, Any]]:
    for output in outputs:
        yield output.to_chunk()


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    account_trail: list[str] = []
    attempts_summaries: list[str] = []
    attempts: list[dict[str, Any]] = []
    image_timelines: list[dict[str, Any]] = []
    postprocess: dict[str, Any] = {}
    upstream_model_slug = ""
    for output in outputs:
        created = created or output.created
        if output.image_timeline:
            image_timelines.append({
                "index": output.index,
                "total": output.total,
                "steps": list(output.image_timeline),
            })
        # 多图并行部分失败：失败那几张的时间线跟着成功结果一起进日志。
        image_timelines.extend(output.failed_image_timelines)
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.upstream_model_slug and not upstream_model_slug:
            upstream_model_slug = output.upstream_model_slug
        # 每个 chunk 都带一份完整的换号轨迹（progress 也算），多图并行时还是
        # A→B、B→A 交替出现。只留第一次出现的账号，否则日志里会拼出
        # A→B→A→B… 几十个「账号」，看着像换了几十个号。
        for email in output.account_trail:
            if email and email not in account_trail:
                account_trail.append(email)
        if output.attempts_summary and output.attempts_summary not in attempts_summaries:
            attempts_summaries.append(output.attempts_summary)
        if output.attempts:
            # 结构化尝试现场只挂在最终 chunk 上；多图时各张图各带一份。
            attempts.extend(dict(item) for item in output.attempts)
        if output.postprocess:
            _merge_postprocess(postprocess, output.postprocess)
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    if account_trail:
        result["_account_trail"] = account_trail
    if attempts_summaries:
        result["_attempts_summary"] = " | ".join(attempts_summaries)
    if attempts:
        result["_attempts"] = attempts
    if image_timelines:
        result["_image_timeline"] = image_timelines
    if postprocess:
        result["_postprocess"] = postprocess
    if upstream_model_slug:
        result["_upstream_model_slug"] = upstream_model_slug
    return result
