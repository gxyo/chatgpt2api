from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import secrets
import string
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

from curl_cffi import requests as curl_requests

from services.register import mail_provider
from services.register.openai_register import (
    _mailbox_domain,
    config,
    log,
    note_task_facts,
    platform_auth0_client,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
    request_platform_oauth_token,
    step,
)
from utils.pkce import code_challenge_for as _code_challenge_for
from utils.pkce import generate_pkce as _generate_pkce

platform_base = "https://platform.openai.com"
auth_base = "https://auth.openai.com"
REGISTER_TIMEOUT = 120_000
SIGNUP_STEP_TIMEOUT = 30_000
PASSWORD_INPUT_SELECTOR = 'input[name="password"], input[type="password"], input[id="password"]'
OTP_INPUT_SELECTOR = (
    'input[name="code"], input[id="code"], input[autocomplete="one-time-code"], '
    'input[inputmode="numeric"]'
)
PROFILE_INPUT_SELECTOR = (
    'input[name="name"], input[id="name"], input[name="fullName"], input[name="age"], '
    'input[id="age"], input[name="birthdate"], input[name="birthday"], input[type="date"]'
)
PASSWORD_OPTION_SELECTOR = (
    'button:has-text("Continue with password"), a:has-text("Continue with password"), '
    'button:has-text("使用密码继续"), a:has-text("使用密码继续")'
)
OTP_SIGNUP_OPTION_SELECTOR = (
    'button:has-text("Sign up with a one-time code"), '
    'a:has-text("Sign up with a one-time code"), '
    'button:has-text("使用一次性验证码注册"), a:has-text("使用一次性验证码注册")'
)
# OpenAI 拒绝用密码注册时的提示。原来这里是 text="Failed to create account. Please try again."
# —— 带引号的 text= 是精确匹配，而线上页面渲染出来的是 "Failed to create account. Please try again"
# （没有结尾句号），一个标点之差就让检测永远为假：兜底切换一次都没触发过，白白干等 15 秒
# 之后报"提交密码后页面未继续"，把上游拒绝伪装成流程卡死。文案和标点都可能再变，
# 改成对正文做大小写不敏感的子串匹配，只认稳定的前缀。
PASSWORD_REJECTION_TEXT = "failed to create account"
SUBMIT_BUTTON_SELECTOR = (
    'button[type="submit"], button:has-text("Continue"), button:has-text("Verify"), '
    'button:has-text("继续"), button:has-text("验证")'
)
BIRTHDATE_INPUT_SELECTOR = (
    'input[name="birthdate"]:not([type="hidden"]), input[name="birthday"]:not([type="hidden"]), '
    'input[type="date"], input[placeholder*="YYYY"], input[placeholder*="MM/DD"], '
    'input[placeholder*="birth" i]'
)
BIRTHDATE_SEGMENT_SELECTOR = '[role="spinbutton"]'
# 注意：不能再用 "**/oauth/authorize*" / "**/auth/callback*" 这类 glob 分模式注册路由。
# Playwright 的 route 只在重定向链的第一个 URL 上回调，302 跳转的目标永远不会被拦截：
# 授权端点和 callback 都是跳转过去的，所以 glob 命中与否全看运气。改用一条 "**/*" 万用路由
# 按 URL 内容判断，再拦掉 callback 页面自己的出站请求，让一次性 code 保持可用。
OAUTH_CALLBACK_PATH = "/auth/callback"
OAUTH_TOKEN_PATH = "/api/accounts/oauth/token"
OAUTH_PROFILE_PATH = "/about-you"
# 上游新增的风控闸门：资料页提交后不再直接跳 callback，而是要求证件核验（"Verify your ID"）。
# 这是个认不出来的页面，原来的实现会干等 30 秒然后报「无法识别注册流程页面」，把风控信号
# 伪装成流程 bug —— 得单独认出来，并且把「换 IP / 换邮箱域名」这句话写进失败原因里。
IDENTITY_VERIFICATION_PATH = "/verify-your-identity"
OAUTH_ROUTE_PATTERN = "**/*"
OAUTH_CALLBACK_STUB = "<!doctype html><title>OAuth complete</title>"
OAUTH_HOSTS = frozenset({"auth.openai.com", "platform.openai.com"})
OAUTH_STATIC_RESOURCE_TYPES = frozenset({"stylesheet", "image", "font", "media"})
# Worker / Service Worker 的请求没有 frame，callback 出现后它们同样能把 code 换掉。
OAUTH_FRAMELESS_BLOCK_TYPES = frozenset({"xhr", "fetch", "other", "ping", "eventsource"})
OAUTH_TOKEN_EXCHANGE_ATTEMPTS = 3
OAUTH_TRANSIENT_STATUSES = ("status=429", "status=500", "status=502", "status=503", "status=504")


def _secret_fingerprint(value: str) -> str:
    """Return a stable diagnostic fingerprint without logging OAuth secrets."""
    if not value:
        return "-"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _trace_enabled() -> bool:
    return str(os.getenv("CHATGPT2API_REGISTER_TRACE") or "").strip().lower() in {"1", "true", "yes"}


def _replace_pkce_params(url: str, code_challenge: str) -> str:
    """Replace PKCE params while preserving all other OAuth query parameters."""
    parsed = urlparse(url)
    query: list[tuple[str, str]] = []
    challenge_replaced = False
    method_replaced = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "code_challenge":
            value = code_challenge
            challenge_replaced = True
        elif key == "code_challenge_method":
            value = "S256"
            method_replaced = True
        query.append((key, value))
    if not challenge_replaced:
        query.append(("code_challenge", code_challenge))
    if not method_replaced:
        query.append(("code_challenge_method", "S256"))
    return parsed._replace(query=urlencode(query)).geturl()


# 浏览器自己那份 PKCE verifier 藏在页面存储里（它必须跨整站跳转活下来，所以只可能落在
# sessionStorage / localStorage / cookie 里）。authorize 请求带的 code_challenge 就是它的
# S256 —— 于是可以按哈希反查，而不是猜上游用了什么键名、什么结构。
_VERIFIER_LIKE = re.compile(r"[A-Za-z0-9_\-]{43,128}")
_VERIFIER_SCAN_SCRIPT = """
() => {
  const values = [];
  const push = (value) => {
    if (typeof value === "string" && value) values.push(value);
  };
  const walk = (node, depth) => {
    if (depth > 4 || values.length > 300) return;
    if (typeof node === "string") { push(node); return; }
    if (node && typeof node === "object") {
      for (const key of Object.keys(node)) { walk(node[key], depth + 1); }
    }
  };
  const scan = (store) => {
    try {
      for (let index = 0; index < store.length; index += 1) {
        const raw = store.getItem(store.key(index));
        if (!raw) continue;
        push(raw);
        try { walk(JSON.parse(raw), 1); } catch (error) {}
      }
    } catch (error) {}
  };
  scan(window.sessionStorage);
  scan(window.localStorage);
  push(document.cookie || "");
  let device = "";
  try { device = window.localStorage.getItem("oai-device-id") || ""; } catch (error) {}
  return { values: values.slice(0, 300), device_id: device };
}
"""


def _verifier_candidates(raw_values: Any) -> list[str]:
    """把存储里的原始字符串摊平成候选明文：整体、JSON 叶子、以及里面像 base64url 的片段。"""
    candidates: list[str] = []
    seen: set[str] = set()
    for value in raw_values if isinstance(raw_values, list) else []:
        if not isinstance(value, str) or not value:
            continue
        for item in (value, *_VERIFIER_LIKE.findall(value)):
            if item and item not in seen:
                seen.add(item)
                candidates.append(item)
    return candidates


def _match_verifier(candidates: Iterable[str], challenges: Iterable[str]) -> str:
    """返回第一个 S256 算出来等于某条已知 challenge 的候选明文（没有就返回空串）。

    只认 S256：上游 authorize 一直带的都是 code_challenge_method=S256，而存储里很可能
    也放着这条 challenge 本身（transaction 对象里两个字段挨着），把明文当 verifier 用会
    换错人——反算必须算得出来才算数。
    """
    wanted = {challenge for challenge in challenges if challenge}
    if not wanted:
        return ""
    for candidate in candidates:
        try:
            if _code_challenge_for(candidate) in wanted:
                return candidate
        except Exception:
            continue
    return ""


async def _browser_storage_snapshot(page) -> tuple[list[str], str]:
    """读一次页面存储，返回（候选明文, device id）。读不到就当作什么都没有。"""
    try:
        snapshot = await asyncio.wait_for(page.evaluate(_VERIFIER_SCAN_SCRIPT), timeout=5)
    except Exception:
        return [], ""
    if not isinstance(snapshot, dict):
        return [], ""
    device_id = str(snapshot.get("device_id") or "").strip()
    return _verifier_candidates(snapshot.get("values")), device_id


def _note_authorize_params(oauth: dict[str, Any], params: dict[str, list[str]]) -> None:
    """记下 authorize 请求里的公开参数：兑换必须和它保持一致。

    client_id 尤其关键——上游 SPA 用的 client 未必还是我们写死那个常量，拿旧 client 去换
    人家的 code，上游只会回一句 invalid_grant。challenge 存下来是为了按 S256 反查 verifier。
    """
    challenge = str((params.get("code_challenge") or [""])[0]).strip()
    if challenge and challenge not in oauth["challenges"]:
        oauth["challenges"].append(challenge)
    for key in ("client_id", "redirect_uri", "device_id"):
        value = str((params.get(key) or [""])[0]).strip()
        if value and not oauth.get(key):
            oauth[key] = value


def _code_is_bound_to_our_challenge(oauth: dict[str, Any]) -> bool:
    """签发 code 的那次 authorize 是不是我们改写成功的那次？

    是——code 绑在我们的 verifier 上，我们自带的那份就能换；
    否——code 绑在浏览器自己那份 verifier 上，只有从它的存储里反查出来才换得动。
    （我们改写过、但 code 由别的请求签发的中间情况，按「不是我们的」处理：反查失败才退回自带。）
    """
    issuer_fp = str(oauth.get("issuer_fp") or "")
    return bool(issuer_fp) and issuer_fp in (oauth.get("rewritten_fps") or set())


async def _resolve_browser_verifier(page, oauth: dict[str, Any], record, *, stage: str) -> None:
    """按 challenge 的 S256 反查浏览器自己留存的 verifier，找到就记进 ``oauth``。

    上游 SPA 生成 verifier 后必须存下来才能在跳转回来后兑换——而且必须存在**兑换方**的源
    （platform.openai.com）下，否则它自己那一步就兑不成。存哪个键名会变，所以只认
    「算出来等于签发 code 那条 challenge」这个事实；签发的那条由 ``issuer_fp`` 指认，
    多条 challenge 里先按它收窄，收窄不到再退回全部（宁可多试一条，也别空手）。
    """
    if oauth.get("verifier"):
        return
    issuer_fp = str(oauth.get("issuer_fp") or "")
    challenges = [
        challenge for challenge in oauth.get("challenges") or []
        if _secret_fingerprint(challenge) == issuer_fp
    ] or list(oauth.get("challenges") or [])
    if not challenges:
        record(f"没有任何观察到 challenge 的 authorize 请求（{stage}），兑换只能用自带的那份")
        return
    candidates, device_id = await _browser_storage_snapshot(page)
    if device_id and not oauth.get("device_id"):
        oauth["device_id"] = device_id
    verifier = _match_verifier(candidates, challenges)
    if verifier:
        oauth["verifier"] = verifier
        oauth["verifier_source"] = "浏览器"
        record(f"从浏览器存储里反查到 verifier（{stage}），code 用浏览器自己那份兑换")
        return
    oauth["verifier_source"] = "自带"
    if not oauth.get("probe_miss_logged"):
        oauth["probe_miss_logged"] = True
        record(
            f"浏览器存储里没有匹配 challenge 的 verifier（{stage}，"
            f"候选 {len(candidates)} 条），兑换只能用自带的那份"
        )


def _callback_frames(frame) -> tuple[Any, ...]:
    """Return the frames of the page the request belongs to, or () for non-page requests."""
    try:
        page = frame.page
    except Exception:
        return ()
    if page is None:
        return ()
    try:
        return tuple(page.frames)
    except Exception:
        return ()


def _frame_urls(frame) -> tuple[str, ...]:
    """Return the URLs of every frame on the requesting page (empty for non-page requests)."""
    urls: list[str] = []
    for candidate in _callback_frames(frame):
        try:
            urls.append(str(candidate.url or ""))
        except Exception:
            continue
    return tuple(urls)


def _page_has_oauth_callback(frame) -> bool:
    """True once the requesting page has a frame sitting on the OAuth callback."""
    return any(OAUTH_CALLBACK_PATH in url for url in _frame_urls(frame))


def _page_at_profile(frame) -> bool:
    """True while the requesting page is on the profile step, whose submit hops to callback."""
    return any(OAUTH_PROFILE_PATH in url for url in _frame_urls(frame))


def _oauth_callback_code(url: str) -> str:
    """Return the one-time code when ``url`` is the platform OAuth callback, else ""."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.netloc not in OAUTH_HOSTS or not parsed.path.startswith(OAUTH_CALLBACK_PATH):
        return ""
    return str((parse_qs(parsed.query).get("code") or [""])[0]).strip()


def _should_block_browser_oauth_request(request) -> str:
    """Return a reason to block an in-page request, or "" to let it through.

    After ``/auth/callback`` loads, the platform's own page (or a nested frame) calls its
    backend to redeem the one-time code. That request can be stopped even though the
    redirect that produced the callback page cannot be intercepted, which is what keeps
    the code alive for our own exchange.
    """
    try:
        frame = request.frame
    except Exception:
        return ""  # APIRequestContext 等非页面请求，放行
    try:
        url = str(request.url or "")
        resource_type = str(request.resource_type or "")
        method = str(request.method or "")
    except Exception:
        return ""
    path = urlparse(url).path
    if path == OAUTH_TOKEN_PATH or path.startswith(f"{OAUTH_TOKEN_PATH}/"):
        return f"页面直接调用 token 接口 ({method})"
    if resource_type in OAUTH_STATIC_RESOURCE_TYPES:
        return ""
    if not _page_has_oauth_callback(frame):
        return ""
    # 放行 GET 文档导航：那是注册流程自己的跳转/重试，不是 callback 页面在兑换 code。
    if resource_type == "document" and method.upper() == "GET":
        return ""
    return f"callback 页面出站请求 ({resource_type})"


def _should_block_frameless_request(request, callback_seen: bool) -> str:
    """Block Worker / Service Worker requests aimed at OpenAI once the callback is in play.

    Worker requests have no frame, so the frame-based rules above cannot see them; a
    service worker is one of the few ways the platform can still redeem the code behind
    our back. Nothing legitimate in this flow needs a worker call to OpenAI by then.
    """
    if not callback_seen:
        return ""
    try:
        url = str(request.url or "")
        resource_type = str(request.resource_type or "")
    except Exception:
        return ""
    if urlparse(url).netloc not in OAUTH_HOSTS:
        return ""
    if resource_type not in OAUTH_FRAMELESS_BLOCK_TYPES:
        return ""
    return f"无 frame 请求 ({resource_type})"


async def _install_oauth_routes(
    page,
    index: int,
    code_challenge: str,
    captured_codes: list[str],
    events: list[str] | None = None,
    oauth: dict[str, Any] | None = None,
) -> None:
    """Install one catch-all route that keeps the one-time code ours to redeem.

    - 带 ``code_challenge`` 的请求一律改写成我们自己的 S256 challenge（authorize 端点会换名字
      ——``/oauth/authorize`` / ``/api/oauth/oauth2/auth`` / ``/api/accounts/authorize``，
      所以按内容判断而不是 glob），并把这次改写记进 ``rewritten_fps``。
      但要清楚改写**未必生效**：Playwright 只对请求链的第一个 URL 回调路由，跳转目标不回调，
      实测跨域 302 的目标连 route 都进不来（见 probe：serve A 302 到 serve B，B 只看到原始
      challenge）。所以「我们改写过 authorize」不等于「签发 code 的那次被改写过」——到底谁签发
      的，看 ``page.on("request")`` 观察到的最后一条 challenge（``issuer_fp``），
      兑换前据此决定用谁的 verifier（见 ``_code_is_bound_to_our_challenge``）。
    - code 是纯观察拿到的：``page.on("response")`` 读那一跳 302 的 ``Location``，
      或者路由里直接看到落在 callback 上的导航。
    - 万一落了 callback，callback 页面的出站请求一律阻断兜底。
    """

    state = {
        "callback_seen": False,
        "callback_requested": False,
        # 最后一条带 challenge 的请求 = 签发 code 的那条（它紧挨在 callback 之前）。
        # 抓到 code 的那一刻快照进 oauth["issuer_fp"]——之后再有 authorize 也不该改口。
        "issuer_fp": "",
    }
    own_challenge_fp = _secret_fingerprint(code_challenge)
    seen_challenges: set[str] = set()
    started = time.monotonic()

    def _record(message: str) -> None:
        if events is not None:
            events.append(f"{message} [t={int((time.monotonic() - started) * 1000)}ms]")

    def _capture(code: str, how: str) -> None:
        state["callback_seen"] = True
        if code in captured_codes:
            return
        captured_codes.append(code)
        if oauth is not None and not oauth.get("issuer_fp"):
            oauth["issuer_fp"] = state["issuer_fp"]
        _record(f"获取 OAuth code（{how}）code_fp={_secret_fingerprint(code)}")
        step(index, "已拦截到 OAuth code")

    def _challenge_fp(url: str) -> str:
        """URL 里带的 code_challenge 指纹——用来确认签发 code 的那次请求拿的是谁的 challenge。"""
        try:
            value = str((parse_qs(urlparse(url).query).get("code_challenge") or [""])[0])
        except Exception:
            return ""
        return _secret_fingerprint(value) if value else ""

    def _on_request(request) -> None:
        """把每一个带 code_challenge 的请求都记下来，包括我们改写不到的那几跳。

        路由不到的跳转目标也会走到这里，所以日志能回答"签发 code 的那次请求到底带了谁的
        challenge"——那决定了这个 code 我们换不换得动。这里也是**唯一**能观察到那几跳的
        authorize 参数的地方：连 client_id 都只能在这儿抄，兑换才不会拿错 client。
        """
        try:
            target = str(getattr(request, "url", "") or "")
        except Exception:
            return
        if _oauth_callback_code(target):
            if not state["callback_requested"]:
                state["callback_requested"] = True
                _record("浏览器已发出 callback 请求")
            return
        fingerprint = _challenge_fp(target)
        # 我们自己的 challenge 出现在线上只可能是改写的结果，不是浏览器自己生成的——
        # 记进「浏览器那份」里只会把兑换带偏。
        if not fingerprint or fingerprint == own_challenge_fp:
            return
        state["issuer_fp"] = fingerprint
        if oauth is not None:
            try:
                _note_authorize_params(oauth, parse_qs(urlparse(target).query))
            except Exception:
                pass
        key = f"{urlparse(target).path}#{fingerprint}"
        if key in seen_challenges:
            return
        seen_challenges.add(key)
        _record(f"发出带 challenge 的请求 {urlparse(target).path} challenge_fp={fingerprint}")

    def _on_response(response) -> None:
        """Read the code off the 302 that points at the callback.

        跳转目标路由不到，但 ``page.on("response")`` 看得到那一跳的 ``location``。
        这是纯观察：不改写、不重发任何请求，所以不可能影响注册流程本身。
        顺便记下这次 302 的来源请求带了谁的 challenge——签发 code 的那次请求如果
        不是我们改写过的那个，code 就绑在别人的 verifier 上，我们怎么换都是 invalid_grant。
        """
        if state["callback_seen"]:
            return
        try:
            status = int(response.status)
            if status < 300 or status >= 400:
                return
            location = str((response.headers or {}).get("location") or "")
            if not location:
                return
            target = urljoin(str(response.url or ""), location)
            source = str(getattr(getattr(response, "request", None), "url", "") or response.url or "")
        except Exception:
            return
        code = _oauth_callback_code(target)
        if code:
            source_fp = _challenge_fp(source)
            # 这一跳的来源请求比「最后一条带 challenge 的请求」更准，优先采信它。
            if source_fp and source_fp != own_challenge_fp:
                state["issuer_fp"] = source_fp
            _capture(code, "响应 Location")
            _record(
                f"code 由 {urlparse(source).path} 签发, 该请求 challenge_fp={source_fp or '-'}, "
                f"我们的 challenge_fp={_secret_fingerprint(code_challenge)}, "
                f"一致={'是' if source_fp == _secret_fingerprint(code_challenge) else '否'}"
            )

    async def _handler(route) -> None:
        request = route.request
        url = str(getattr(request, "url", "") or "")
        method = str(getattr(request, "method", "") or "")
        try:
            direct_code = _oauth_callback_code(url)
            if direct_code:
                # code 就在这次导航的 URL 里：收下它，请求根本不用发出去。
                _capture(direct_code, "直接导航")
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=OAUTH_CALLBACK_STUB,
                )
                return
            if "code_challenge=" in url:
                params = parse_qs(urlparse(url).query)
                original = str((params.get("code_challenge") or [""])[0])
                if oauth is not None:
                    _note_authorize_params(oauth, params)
                    oauth["rewritten_fps"].add(_secret_fingerprint(original))
                # 这里**不能**去读页面存储：请求正停在这个 handler 里，页面处于导航中，
                # page.evaluate 会一直等到超时也不返回（实测）。verifier 的反查放到兑换前
                # 做——那时页面已经停在 callback 上，存储读得到。
                _record(
                    f"改写 authorize PKCE {urlparse(url).path} "
                    f"original_fp={_secret_fingerprint(original)}"
                )
                if _trace_enabled():
                    step(
                        index,
                        "OAuth trace: authorize PKCE "
                        f"original_fp={_secret_fingerprint(original)}, "
                        f"replacement_fp={_secret_fingerprint(code_challenge)}",
                    )
                await route.continue_(url=_replace_pkce_params(url, code_challenge))
                return
            reason = _should_block_browser_oauth_request(request)
            if not reason:
                reason = _should_block_frameless_request(
                    request, bool(state["callback_seen"])
                )
            if reason:
                parsed = urlparse(url)
                _record(f"阻断 {method} {parsed.netloc}{parsed.path}（{reason}）")
                if _trace_enabled():
                    step(index, f"OAuth trace: 阻断 callback 请求 {method} {url[:160]}", "yellow")
                await route.abort("blockedbyclient")
                return
        except Exception as error:
            _record(f"路由异常放行 {method} {urlparse(url).path}: {error}")
            if _trace_enabled():
                step(index, f"OAuth trace: 路由异常放行 {error}", "yellow")
        await route.continue_()

    await page.route(OAUTH_ROUTE_PATTERN, _handler)
    page.on("response", _on_response)
    page.on("request", _on_request)


def _install_oauth_trace(page, index: int) -> None:
    """Log auth API request ordering when explicitly enabled for diagnostics."""
    if not _trace_enabled():
        return

    def _request(request) -> None:
        parsed = urlparse(request.url)
        if parsed.netloc not in {"auth.openai.com", "platform.openai.com"}:
            return
        if not (
            parsed.path.startswith("/api/accounts/")
            or parsed.path == "/oauth/authorize"
            or parsed.path == "/api/oauth/oauth2/auth"
            or parsed.path == "/auth/callback"
        ):
            return
        detail = ""
        if parsed.path == "/api/oauth/oauth2/auth":
            params = parse_qs(parsed.query)
            detail = (
                f", query_keys={','.join(sorted(params)) or '-'}"
                f", challenge_fp={_secret_fingerprint(str((params.get('code_challenge') or [''])[0]))}"
            )
        elif parsed.path == "/api/accounts/oauth/token":
            try:
                payload = json.loads(request.post_data or "{}")
            except Exception:
                payload = {}
            detail = (
                f", code_fp={_secret_fingerprint(str(payload.get('code') or ''))}"
                f", verifier_fp={_secret_fingerprint(str(payload.get('code_verifier') or ''))}"
            )
        step(index, f"OAuth trace -> {request.method} {parsed.netloc}{parsed.path}{detail}")

    def _response(response) -> None:
        parsed = urlparse(response.url)
        if parsed.netloc not in {"auth.openai.com", "platform.openai.com"}:
            return
        if not (
            parsed.path.startswith("/api/accounts/")
            or parsed.path == "/oauth/authorize"
            or parsed.path == "/api/oauth/oauth2/auth"
            or parsed.path == "/auth/callback"
        ):
            return
        step(index, f"OAuth trace <- {response.status} {parsed.netloc}{parsed.path}")

    page.on("request", _request)
    page.on("response", _response)


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_age() -> str:
    return str(random.randint(20, 30))


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2004):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


async def _locator_is_visible(page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


async def _page_rejects_password_signup(page) -> bool:
    """OpenAI 反滥用拒掉了这次创建账号（"Failed to create account. Please try again"）。

    读页面正文做子串判断，不用选择器：上游文案和标点会变，精确匹配已经失效过一次。
    """
    try:
        body = await page.inner_text("body")
    except Exception:
        return False
    return PASSWORD_REJECTION_TEXT in body.lower()


async def _wait_for_signup_step(page, captured_code: list[str], timeout_ms: int = SIGNUP_STEP_TIMEOUT) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        if captured_code:
            return "complete"

        parsed = urlparse(page.url)
        callback_code = str((parse_qs(parsed.query).get("code") or [""])[0]).strip()
        if "/auth/callback" in parsed.path or callback_code:
            return "complete"
        if "/about-you" in parsed.path:
            return "profile"
        if IDENTITY_VERIFICATION_PATH in parsed.path:
            return "identity"
        if "/create-account/password" in parsed.path and await _locator_is_visible(page, PASSWORD_INPUT_SELECTOR):
            return "password"

        for state, selector in (
            ("password", PASSWORD_INPUT_SELECTOR),
            ("profile", PROFILE_INPUT_SELECTOR),
            ("otp", OTP_INPUT_SELECTOR),
        ):
            if await _locator_is_visible(page, selector):
                return state

        if "/email-verification" in parsed.path:
            return "otp"

        if asyncio.get_running_loop().time() >= deadline:
            body = await _page_debug_info(page)
            raise RuntimeError(f"无法识别注册流程页面, url={page.url}, body={body}")
        await page.wait_for_timeout(250)


async def _switch_to_password_if_offered(page, index: int) -> bool:
    option = page.locator(PASSWORD_OPTION_SELECTOR).first
    try:
        if not await option.is_visible():
            return False
        step(index, "检测到新注册流程，切换到密码注册")
        await option.click()
        await page.locator(PASSWORD_INPUT_SELECTOR).first.wait_for(state="visible", timeout=15_000)
        return True
    except Exception:
        return False


async def _return_to_otp_signup(page, index: int) -> None:
    option = page.locator(OTP_SIGNUP_OPTION_SELECTOR).first
    if await option.is_visible():
        await option.click()
    else:
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=REGISTER_TIMEOUT)
        except Exception:
            pass

    try:
        await page.locator(OTP_INPUT_SELECTOR).first.wait_for(state="visible", timeout=15_000)
    except Exception:
        body = await _page_debug_info(page)
        raise RuntimeError(
            f"密码注册被 OpenAI 拒绝，且无法切回一次性验证码注册, url={page.url}, body={body}"
        )
    step(index, "密码注册被 OpenAI 拒绝，改用一次性验证码注册", "yellow")


async def _continue_identity_verification(page, index: int) -> None:
    """上游要求证件核验（verify-your-identity）时点一下 Continue。

    页面本身只有一句说明 + Continue；点了多半是进第三方证件核验（自动化不了），所以这是
    唯一能做的一步，点不动就立刻认输。关键在于失败原因要说清是风控闸门，而不是「认不出页面」——
    后者会让人以为是我们自己的选择器坏了，白白浪费一轮线上排查。
    """
    button = page.locator(
        'button:has-text("Continue"), button:has-text("继续"), button[type="submit"]'
    ).first
    try:
        await button.wait_for(state="visible", timeout=5_000)
        await button.click()
    except Exception:
        raise RuntimeError(
            "上游要求证件核验（verify-your-identity），当前 IP 或邮箱域名已被风控"
        )
    step(index, "已点击证件核验页面的继续", "yellow")


async def _submit_password(page, index: int, password: str) -> bool:
    step(index, "输入密码")
    password_input = page.locator(PASSWORD_INPUT_SELECTOR).first
    try:
        await password_input.wait_for(state="visible", timeout=15_000)
    except Exception:
        body = await _page_debug_info(page)
        raise RuntimeError(f"未找到密码输入框, url={page.url}, body={body}")
    await password_input.fill(password)
    await page.locator(SUBMIT_BUTTON_SELECTOR).first.click()

    deadline = asyncio.get_running_loop().time() + 15
    while True:
        if not await password_input.is_visible():
            return True
        if await _page_rejects_password_signup(page):
            await _return_to_otp_signup(page, index)
            return False
        if asyncio.get_running_loop().time() >= deadline:
            body = await _page_debug_info(page)
            raise RuntimeError(f"提交密码后页面未继续, url={page.url}, body={body}")
        await page.wait_for_timeout(250)


async def _submit_otp(page, index: int, mailbox: dict) -> None:
    step(index, "等待收取验证码")
    code = mail_provider.wait_for_code(config["mail"], mailbox)
    if not code:
        raise RuntimeError("等待注册验证码超时")
    step(index, f"收到注册验证码: {code}")

    step(index, "输入验证码")
    code_inputs = page.locator(OTP_INPUT_SELECTOR)
    try:
        await code_inputs.first.wait_for(state="visible", timeout=15_000)
    except Exception:
        body = await _page_debug_info(page)
        raise RuntimeError(f"未找到验证码输入框, url={page.url}, body={body}")

    input_count = await code_inputs.count()
    if input_count == len(code) and input_count > 1:
        for position, digit in enumerate(code):
            await code_inputs.nth(position).fill(digit)
    else:
        await code_inputs.first.fill(code)

    await page.locator(SUBMIT_BUTTON_SELECTOR).first.click()
    try:
        await code_inputs.first.wait_for(state="hidden", timeout=15_000)
    except Exception:
        await page.wait_for_timeout(1000)


async def _probe_platform_session(context) -> str:
    """Report whether the platform itself ended up logged in — i.e. it redeemed the code."""
    try:
        response = await context.request.get(
            f"{platform_base}/api/auth/session",
            headers={"accept": "application/json", "referer": f"{platform_base}/"},
            fail_on_status_code=False,
            timeout=15_000,
        )
    except Exception as error:
        return f"平台会话探测失败: {error}"
    try:
        body = (await response.text()).strip()
    except Exception:
        body = ""
    logged_in = any(token in body[:400] for token in ('"user"', '"accessToken"', '"email"'))
    return f"平台会话探测: status={response.status}, 平台已登录={logged_in}"


def _is_transient_exchange_error(error: Exception) -> bool:
    """Retry network hiccups and 429/5xx, but never a deterministic 4xx like invalid_grant."""
    message = str(error)
    if "status=" not in message:
        return True
    return any(fragment in message for fragment in OAUTH_TRANSIENT_STATUSES)


async def _exchange_oauth_token_in_browser(
    page, context, index: int, code: str, code_verifier: str, proxy: str,
    *, client_id: str = "", redirect_uri: str = "", device_id: str = "",
) -> dict:
    # client_id / redirect_uri 优先用浏览器 authorize 请求里观察到的那个：上游 SPA 换过
    # client 的话，拿我们写死的常量去换人家的 code，只会得到一句 invalid_grant。
    payload = {
        "client_id": client_id or platform_oauth_client_id,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri or platform_oauth_redirect_uri,
    }
    browser_headers = {
        "accept": "*/*",
        "auth0-client": platform_auth0_client,
        "content-type": "application/json",
        "origin": platform_base,
        "referer": f"{platform_base}/",
    }
    if device_id:
        # 上游自己的接口都带这个头；能观察到就跟着带，别让对方把这次兑换当成陌生设备。
        browser_headers["oai-device-id"] = device_id

    session_options: dict[str, Any] = {"impersonate": "chrome", "verify": False}
    if proxy:
        session_options["proxy"] = proxy
    session = curl_requests.Session(**session_options)
    try:
        for cookie in await context.cookies():
            name = str(cookie.get("name") or "")
            if not name:
                continue
            domain = str(cookie.get("domain") or "")
            if name.startswith("__Host-"):
                domain = ""
            session.cookies.set(
                name,
                str(cookie.get("value") or ""),
                domain=domain,
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure")),
            )
        last_error: Exception | None = None
        for attempt in range(OAUTH_TOKEN_EXCHANGE_ATTEMPTS):
            try:
                return request_platform_oauth_token(
                    session, code, code_verifier,
                    client_id=payload["client_id"],
                    redirect_uri=payload["redirect_uri"],
                    device_id=device_id,
                )
            except Exception as error:
                last_error = error
                if attempt + 1 >= OAUTH_TOKEN_EXCHANGE_ATTEMPTS or not _is_transient_exchange_error(error):
                    break
                step(
                    index,
                    f"Chrome OAuth token 交换暂时失败，重试 ({attempt + 2}/{OAUTH_TOKEN_EXCHANGE_ATTEMPTS}): {error}",
                    "yellow",
                )
                await page.wait_for_timeout(1000 * (attempt + 1))
        step(index, f"Chrome OAuth token 交换失败，尝试 Playwright 会话: {last_error}", "yellow")
    finally:
        session.close()

    response = await context.request.post(
        f"{auth_base}/api/accounts/oauth/token",
        headers=browser_headers,
        data=payload,
        fail_on_status_code=False,
        timeout=60_000,
    )
    status = int(response.status)
    body = await response.text()
    request_id = str(response.headers.get("x-request-id") or "")
    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}
    if status == 200 and isinstance(data, dict) and data.get("access_token"):
        return data

    request_detail = f", request_id={request_id}" if request_id else ""
    raise RuntimeError(
        f"OAuth token 交换失败: status={status}{request_detail}, body={body[:300]}"
    )


async def _run_signup_state_machine(
    page, index: int, password: str, name: str, age: str, mailbox: dict, captured_code: list[str]
) -> bool:
    password_set = False
    password_attempted = False
    otp_submitted = False
    profile_submitted = False
    identity_attempted = False

    for _ in range(8):
        state = await _wait_for_signup_step(page, captured_code)
        if state == "complete":
            return password_set
        if state == "password":
            if password_attempted:
                body = await _page_debug_info(page)
                raise RuntimeError(f"提交密码后页面未继续, url={page.url}, body={body}")
            password_attempted = True
            password_set = await _submit_password(page, index, password)
            continue
        if state == "otp":
            if not password_attempted and not otp_submitted and await _switch_to_password_if_offered(page, index):
                continue
            if otp_submitted:
                body = await _page_debug_info(page)
                raise RuntimeError(f"提交验证码后页面未继续, url={page.url}, body={body}")
            await _submit_otp(page, index, mailbox)
            otp_submitted = True
            continue
        if state == "profile":
            if profile_submitted:
                body = await _page_debug_info(page)
                raise RuntimeError(f"提交账号资料后页面未继续, url={page.url}, body={body}")
            step(index, "填写账号信息")
            await _fill_profile(page, name, age, index)
            profile_submitted = True
        if state == "identity":
            if identity_attempted:
                raise RuntimeError(
                    "上游要求证件核验（verify-your-identity），当前 IP 或邮箱域名已被风控"
                )
            identity_attempted = True
            step(index, "上游要求证件核验，尝试继续", "yellow")
            await _continue_identity_verification(page, index)

    body = await _page_debug_info(page)
    raise RuntimeError(f"注册流程步骤过多, url={page.url}, body={body}")


async def _async_register(index: int, proxy: str) -> dict:
    from playwright.async_api import async_playwright

    step(index, "启动浏览器")
    async with async_playwright() as pw:
        launch_args: dict[str, Any] = {
            "headless": False,
            "args": [
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        }
        if proxy:
            launch_args["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch_args)
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            page = await context.new_page()
            _install_oauth_trace(page, index)

            step(index, "创建邮箱")
            mailbox = mail_provider.create_mailbox(config["mail"])
            email = str(mailbox.get("address") or "").strip()
            if not email:
                mail_provider.release_mailbox(mailbox)
                raise RuntimeError("邮箱服务未返回 address")
            label = str(mailbox.get("label") or "")
            note_task_facts(
                index,
                email=email,
                mail_domain=_mailbox_domain(email),
                mail_provider=str(mailbox.get("provider") or ""),
                mail_label=label,
            )
            step(index, f"邮箱创建完成[{label}]: {email}")

            password = _random_password()
            first_name, last_name = _random_name()
            age = _random_age()

            try:
                result = await _browser_register_flow(
                    page, context, index, email, password,
                    f"{first_name} {last_name}", age, mailbox, proxy,
                )
            except Exception as error:
                mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
                raise

            mail_provider.mark_mailbox_result(mailbox, success=True)
            return result
        finally:
            await browser.close()


async def _browser_register_flow(
    page, context, index: int, email: str, password: str,
    name: str, age: str, mailbox: dict, proxy: str,
) -> dict:
    code_verifier, code_challenge = _generate_pkce()
    captured_code: list[str] = []
    oauth_events: list[str] = []
    # 一次注册里 authorize 请求带的公开参数 + 最后选中的 verifier。
    # client_id / redirect_uri 必须和签发 code 的那次 authorize 一致，verifier 必须是
    # 服务端记下的那条 challenge 的原文（见 _resolve_browser_verifier）。
    oauth_context: dict[str, Any] = {
        "challenges": [],
        "client_id": "",
        "redirect_uri": "",
        "device_id": "",
        "verifier": "",
        "verifier_source": "自带",
        # 签发 code 的那条 challenge 的指纹，以及我们真正改写成功的那些指纹。
        "issuer_fp": "",
        "rewritten_fps": set(),
    }

    def _dump_oauth_events() -> None:
        for event in oauth_events[-16:]:
            step(index, f"OAuth 诊断: {event}", "yellow")

    await _install_oauth_routes(
        page, index, code_challenge, captured_code, oauth_events, oauth_context
    )

    signup_url = f"{platform_base}/signup"
    step(index, "导航到注册页面")
    await page.goto(signup_url, wait_until="domcontentloaded", timeout=REGISTER_TIMEOUT)
    await page.wait_for_timeout(3000)

    step(index, f"当前页面: {page.url}")

    step(index, "输入邮箱")
    email_input = page.locator('input[name="email"], input[type="email"], input[id="email"], input[id="username"]').first
    for attempt in range(3):
        try:
            await email_input.wait_for(state="visible", timeout=15_000)
            break
        except Exception:
            if attempt < 2:
                step(index, f"邮箱输入框未出现，刷新页面重试 ({attempt + 1}/2)")
                await page.reload(wait_until="domcontentloaded", timeout=REGISTER_TIMEOUT)
                await page.wait_for_timeout(3000)
                email_input = page.locator('input[name="email"], input[type="email"], input[id="email"], input[id="username"]').first
            else:
                body = await _page_debug_info(page)
                raise RuntimeError(f"未找到邮箱输入框, url={page.url}, body={body}")
    await email_input.fill(email)

    continue_btn = page.locator('button[type="submit"], button:has-text("Continue"), button:has-text("继续")').first
    await continue_btn.click()
    await page.wait_for_timeout(5000)

    step(index, f"点击继续后页面: {page.url}")

    try:
        password_set = await _run_signup_state_machine(
            page, index, password, name, age, mailbox, captured_code
        )
    except Exception:
        # 状态机死掉时（比如停在认不出的页面上）也要留下拦截记录，
        # 否则日志里只有一句"无法识别注册流程页面"，看不到前面发生了什么。
        step(index, f"OAuth 诊断: 状态机中止于 {page.url[:120]}", "yellow")
        _dump_oauth_events()
        raise
    if not password_set:
        step(index, "当前流程未提供密码设置入口，账号将按无密码方式保存", "yellow")
    for _ in range(15):
        if captured_code:
            break
        current_url = page.url
        if "/auth/callback" in current_url or "code=" in current_url:
            parsed = urlparse(current_url)
            params = parse_qs(parsed.query)
            c = (params.get("code") or [""])[0]
            if c:
                # 正常路径是响应监听在 302 那一跳就拿到 code。走到这里说明监听没接住，
                # 我们是在浏览器已经落到 callback 页之后才开始兑换，大概率已经晚了。
                captured_code.append(c)
                step(index, "OAuth 诊断: 兜底从浏览器 URL 取码（响应监听未接住，可能已晚一步）", "yellow")
                _dump_oauth_events()
                break
        await page.wait_for_timeout(2000)

    if not captured_code:
        _dump_oauth_events()
        raise RuntimeError(f"未能获取到 OAuth code, 最终页面: {page.url}")

    if not oauth_context["verifier"] and not _code_is_bound_to_our_challenge(oauth_context):
        # 签发 code 的那次 authorize 不是我们改写成功的那次（路由不到的跳转目标进不来 handler），
        # code 绑在浏览器自己的 verifier 上。此刻页面正停在 callback（platform.openai.com 源），
        # 而存储只在导航结束后读得到——首次能读的时机就是这里。
        await _resolve_browser_verifier(
            page, oauth_context, oauth_events.append, stage="兑换前"
        )
    verifier = str(oauth_context["verifier"] or code_verifier)
    client_id = str(oauth_context["client_id"] or platform_oauth_client_id)
    redirect_uri = str(oauth_context["redirect_uri"] or platform_oauth_redirect_uri)
    step(
        index,
        f"OAuth 诊断: verifier 来源={oauth_context['verifier_source']}, "
        f"client_id={client_id}, device_id={'有' if oauth_context['device_id'] else '无'}, "
        f"观察到的 challenge={len(oauth_context['challenges'])} 条, "
        f"签发方 challenge_fp={oauth_context['issuer_fp'] or '-'}"
        f"（改写生效={'是' if _code_is_bound_to_our_challenge(oauth_context) else '否'}）",
        "yellow",
    )

    step(index, "用 OAuth code 换取 token")
    if _trace_enabled():
        step(
            index,
            "OAuth trace: external token exchange "
            f"code_fp={_secret_fingerprint(captured_code[0])}, "
            f"verifier_fp={_secret_fingerprint(verifier)}",
        )
    try:
        tokens = await _exchange_oauth_token_in_browser(
            page, context, index, captured_code[0], verifier, proxy,
            client_id=client_id, redirect_uri=redirect_uri,
            device_id=str(oauth_context["device_id"]),
        )
    except Exception as error:
        # invalid_grant 说明这个 code 我们换不了：要么已被兑换，要么绑在别人的 verifier 上。
        # 打印拦截记录、平台会话状态和这次兑换的身份，让日志能区分这几种情况。
        step(index, f"OAuth 诊断: {await _probe_platform_session(context)}", "yellow")
        _dump_oauth_events()
        raise RuntimeError(
            f"{error}（verifier={oauth_context['verifier_source']}, client_id={client_id}, "
            f"签发方改写生效={'是' if _code_is_bound_to_our_challenge(oauth_context) else '否'}）"
        ) from error

    if not tokens or not tokens.get("access_token"):
        raise RuntimeError("OAuth token 交换返回数据缺少 access_token")

    step(index, "注册完成，token 获取成功")
    return {
        "email": email,
        "password": password if password_set else "",
        "access_token": str(tokens.get("access_token") or "").strip(),
        "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        "id_token": str(tokens.get("id_token") or "").strip(),
        "source_type": "web",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def _page_debug_info(page) -> str:
    try:
        text = await page.inner_text("body")
        return text[:500].replace("\n", " ")
    except Exception:
        return "(unable to read page)"


def _birthdate_segment_name(label: str) -> str:
    normalized = str(label or "").strip().lower()
    if "year" in normalized or "年份" in normalized or normalized.startswith("年"):
        return "year"
    if "month" in normalized or "月份" in normalized or normalized.startswith("月"):
        return "month"
    if "day" in normalized or "日期" in normalized or normalized.startswith("日"):
        return "day"
    return ""


async def _fill_birthdate(page, birthdate: str) -> bool:
    year, month, day = birthdate.split("-", 2)
    date_input = page.locator(BIRTHDATE_INPUT_SELECTOR).first
    try:
        date_input_visible = await date_input.is_visible()
    except Exception:
        date_input_visible = False
    if date_input_visible:
        input_type = str(await date_input.get_attribute("type") or "").lower()
        placeholder = str(await date_input.get_attribute("placeholder") or "").lower()
        value = birthdate
        if input_type != "date" and "/" in placeholder:
            positions = {part: placeholder.find(part) for part in ("yyyy", "mm", "dd")}
            if positions["yyyy"] >= 0 and positions["yyyy"] < positions["mm"]:
                value = f"{year}/{month}/{day}"
            elif positions["dd"] >= 0 and positions["dd"] < positions["mm"]:
                value = f"{day}/{month}/{year}"
            else:
                value = f"{month}/{day}/{year}"
        await date_input.fill(value)
        return True

    segments = page.locator(BIRTHDATE_SEGMENT_SELECTOR)
    visible_segments = []
    for position in range(await segments.count()):
        segment = segments.nth(position)
        if await segment.is_visible():
            visible_segments.append(segment)
    if len(visible_segments) < 3:
        return False

    values = {"year": year, "month": month, "day": day}
    segment_names = [
        _birthdate_segment_name(str(await segment.get_attribute("aria-label") or ""))
        for segment in visible_segments
    ]
    if not {"year", "month", "day"}.issubset(set(segment_names)):
        # The browser context uses en-US, whose unlabeled date segment order is month/day/year.
        segment_names = ["month", "day", "year", *segment_names[3:]]

    filled: set[str] = set()
    for segment, segment_name in zip(visible_segments, segment_names):
        if segment_name not in values or segment_name in filled:
            continue
        await segment.fill(values[segment_name])
        filled.add(segment_name)
    return filled == {"year", "month", "day"}


async def _fill_profile(page, name: str, age: str, index: int) -> None:
    await page.wait_for_timeout(2000)
    step(index, f"填写资料页面: {page.url}")

    name_input = page.locator('input[name="name"], input[id="name"], input[placeholder*="name" i], input[placeholder*="Name" i]').first
    try:
        await name_input.wait_for(state="visible", timeout=10_000)
        await name_input.fill(name)
        step(index, f"已填写姓名: {name}")
    except Exception:
        all_inputs = page.locator("input[type='text']")
        count = await all_inputs.count()
        if count > 0:
            await all_inputs.first.fill(name)

    age_filled = False
    age_input = page.locator('input[name="age"], input[id="age"], input[placeholder*="age" i], input[type="number"]').first
    if await age_input.is_visible():
        try:
            await age_input.fill(age)
            step(index, f"已填写年龄: {age}")
            age_filled = True
        except Exception:
            pass

    if not age_filled:
        birthdate = _random_birthdate()
        try:
            age_filled = await _fill_birthdate(page, birthdate)
        except Exception:
            age_filled = False
        if age_filled:
            step(index, f"已填写生日: {birthdate}")

    if not age_filled:
        body = await _page_debug_info(page)
        raise RuntimeError(f"未找到可填写的年龄或生日控件, url={page.url}, body={body}")

    await page.wait_for_timeout(500)

    finish_btn = page.locator('button:has-text("Finish"), button:has-text("finish"), button[type="submit"]').first
    try:
        await finish_btn.wait_for(state="visible", timeout=5_000)
        await finish_btn.click()
        step(index, "已点击 Finish creating account")
    except Exception:
        buttons = page.locator('button:has-text("Continue"), button:has-text("Agree"), button:has-text("Submit")')
        count = await buttons.count()
        if count > 0:
            await buttons.first.click()

    try:
        await page.wait_for_url(lambda url: "about-you" not in url, timeout=15_000)
        step(index, f"表单提交后跳转到: {page.url}")
    except Exception:
        step(index, f"提交后页面未跳转，仍在: {page.url}")
        body = await _page_debug_info(page)
        step(index, f"当前页面内容: {body[:300]}")


def register(index: int, proxy: str = "") -> dict:
    """同步入口：在新的事件循环中运行 Playwright 注册流程。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_async_register(index, proxy))
    finally:
        loop.close()
