"""调用 sentinel 求解服务（sentinel_solver/）计算 Turnstile token。

2026-10 起 chatgpt.com 只接受在**真实 chatgpt.com 页面**里算出来的 Turnstile
token：纯 Python/Node 模拟的 VM 过不了校验，缺 token 时上游不报错，而是把请求
伪装成 404 ``{"detail":"Model not found"}`` 返回。

求解服务没配或者算不出来时**直接报错**，不退回本地求解器（``utils.turnstile``）：
本地模拟出来的 token 一样会被拒，但拒法是被伪装成 404 ``Model not found``，会被上层
误判成「这个账号不行」而白白冷却账号。本地求解器只服务于「生图握手模式 = 旧版」那条
刻意保留的退路。
"""
from __future__ import annotations

import threading
import time

from curl_cffi import requests

from services.config import config
from utils.log import logger

SOLVER_TIMEOUT_SECONDS = 20
# 求解走的是页面里那些真实模块，冷页面第一次用要现取现解析，比探活慢得多。
SOLVER_SOLVE_TIMEOUT_SECONDS = 60
PROFILE_CACHE_SECONDS = 600


class SentinelSolverError(RuntimeError):
    """求解服务没配或者算不出 Turnstile token。

    这条链路故意「失败即报错」而不是退回本地模拟 VM：本地算出来的 token 会被上游
    拒掉，而上游不报鉴权失败，只把对话接口伪装成 404 ``Model not found`` —— 那会被
    当成「这个账号不可用」，白白把号池里的号逐个冷却掉。宁可在这里明确报错。
    """


_profile_lock = threading.Lock()
_profile_cache: dict[str, object] = {"url": "", "profile": {}, "expires_at": 0.0}


def _solver_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.sentinel_solver_key:
        headers["Authorization"] = f"Bearer {config.sentinel_solver_key}"
    return headers


def solver_profile() -> dict[str, object]:
    """求解页面的 UA / 时区 / 偏移；没配求解服务或取不到时返回空字典。

    这几项必须和算 Turnstile 的那个页面一致，否则上游按低可信处理，对话接口返回
    伪装成 404 的 "Model not found"。所以主服务不自己拼这些值，而是照抄页面报的
    ——时区名和偏移由同一个浏览器实例算出来，天然不会自相矛盾。
    """
    base_url = config.sentinel_solver_url
    if not base_url:
        return {}
    with _profile_lock:
        now = time.monotonic()
        if _profile_cache["url"] == base_url and now < float(_profile_cache["expires_at"]):
            return dict(_profile_cache["profile"])  # type: ignore[arg-type]
        profile: dict[str, object] = {}
        try:
            response = requests.get(f"{base_url}/health", headers=_solver_headers(), timeout=SOLVER_TIMEOUT_SECONDS)
            if response.status_code == 200:
                payload = response.json() or {}
                if isinstance(payload, dict) and payload.get("user_agent"):
                    profile = payload
        except Exception as exc:
            logger.warning({"event": "sentinel_solver_health_error", "error": str(exc)})
        # 失败时缓存也短：不让一次抖动把空档案钉死十分钟。
        _profile_cache.update(
            url=base_url,
            profile=profile,
            expires_at=now + (PROFILE_CACHE_SECONDS if profile else 30),
        )
        return dict(profile)


def solver_user_agent() -> str:
    """求解页面实际使用的 UA；取不到时返回空串，调用方退回自己的指纹。"""
    return str(solver_profile().get("user_agent") or "")


def solver_timezone_pair() -> tuple[str, int] | None:
    """求解页面报的 ``(时区名, new Date().getTimezoneOffset())``；页面没答全就返回 None。

    两项要么一起给、要么一起不给：只有让调用方整体退回本机配置，才不会拼出
    「名字抄页面的、偏移算本机的」这种自相矛盾的组合——那正是会被上游判成低可信的形态。
    """
    profile = solver_profile()
    offset = profile.get("timezone_offset_min")
    name = str(profile.get("timezone") or "")
    if not name:
        return None
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return None
    return name, int(offset)


def solve_turnstile(dx: str, p: str, requirements: dict | None = None) -> str:
    """算 Turnstile token：dx 取 prepare 响应的 turnstile.dx，p 是提交的 requirements token。

    ``requirements`` 是整份 prepare 响应，一起转给求解服务 —— 有了它，求解服务可以直接
    调用 chatgpt.com 页面自己那份 turnstile 模块，而不是靠我们移植的 VM 去模拟挑战。
    里面只有挑战材料（prepare_token、dx、PoW seed、SO 句柄），没有账号 token、Cookie
    或用户 prompt。

    失败即抛 ``SentinelSolverError``（不退回本地 VM），理由见该异常。
    """
    base_url = config.sentinel_solver_url
    if not base_url:
        raise SentinelSolverError(
            "sentinel solver is not configured "
            "(CHATGPT2API_SENTINEL_SOLVER_URL / config.json sentinel_solver_url); "
            "set 生图握手模式 to 旧版 if you mean to run without it"
        )
    body: dict[str, object] = {"dx": dx, "p": p}
    if requirements:
        body["requirements"] = requirements
    try:
        response = requests.post(
            f"{base_url}/v1/turnstile",
            headers=_solver_headers(),
            json=body,
            timeout=SOLVER_SOLVE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning({"event": "sentinel_solver_error", "error": str(exc)})
        raise SentinelSolverError(f"sentinel solver request failed: {exc}") from exc
    if response.status_code != 200:
        logger.warning({
            "event": "sentinel_solver_failed",
            "status": response.status_code,
            "body": str(response.text or "")[:200],
        })
        raise SentinelSolverError(
            f"sentinel solver returned HTTP {response.status_code}: {str(response.text or '')[:200]}"
        )
    token = str((response.json() or {}).get("token") or "")
    if not token:
        raise SentinelSolverError("sentinel solver returned an empty turnstile token")
    return token
