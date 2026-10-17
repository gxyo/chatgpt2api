from __future__ import annotations

import json
import time
from typing import Any

from services.account_service import account_service
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL, public_error_message


def list_upstream_models(access_token: str = "") -> dict[str, Any]:
    """直连上游取模型列表，原样返回，不做本地合并或归一化。

    传 access_token 时走登录通道（``/backend-api/models``），否则走匿名通道。
    """
    return OpenAIBackendAPI(access_token=access_token).list_models_raw()


def probe_upstream_model(access_token: str, model: str = "") -> dict[str, Any]:
    """用一条极短的文本对话验证某个模型 slug 能否被上游接受。

    模型选择器清单不是路由白名单，不在清单里的旧 slug 也可能可用；唯一可靠的
    验证方式就是真的发一轮请求。拿到 ``server_ste_metadata.model_slug`` 立即
    断开，不等待正文。注意：探测会在账号上真实创建一条对话。
    """
    requested = str(model or "").strip() or config.default_upstream_model_name
    started = time.monotonic()
    model_slug = ""
    conversation_id = ""
    error_text = ""
    backend = OpenAIBackendAPI(
        access_token=access_token,
        deadline=time.monotonic() + 20.0,
    )
    stream = backend.stream_conversation(prompt="ping", model=requested)
    try:
        for payload in stream:
            try:
                event = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            conversation_id = str(event.get("conversation_id") or conversation_id)
            value = event.get("v")
            if isinstance(value, dict):
                conversation_id = str(value.get("conversation_id") or conversation_id)
            if event.get("type") == "server_ste_metadata":
                metadata = event.get("metadata")
                if isinstance(metadata, dict):
                    model_slug = str(metadata.get("model_slug") or "").strip()
                    if model_slug:
                        break
            if event.get("type") in {"error", "conversation.error"}:
                error_text = str(event.get("message") or event.get("error") or "上游返回错误").strip()
                break
    except Exception as exc:
        return {
            "model": requested,
            "channel": "authenticated" if access_token else "anonymous",
            "ok": False,
            "error": public_error_message(exc),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        stream.close()
    result = {
        "model": requested,
        "channel": "authenticated" if access_token else "anonymous",
        "ok": not error_text,
        "model_slug": model_slug,
        "conversation_id": conversation_id,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if error_text:
        result["error"] = error_text
    return result


def list_models() -> dict[str, Any]:
    result = OpenAIBackendAPI().list_models()
    data = result.get("data")
    if not isinstance(data, list):
        return result
    seen = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    web_image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
    ]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if web_image_accounts:
        dynamic_models.add("gpt-image-2")
        dynamic_models.add("gpt-image-2.5")
        dynamic_models.add("gpt-image-2.5-sunburst")
        dynamic_models.add("gpt-image-2.5-flare")
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append({
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt2api",
                "permission": [],
                "root": model,
                "parent": None,
            })
    return result
