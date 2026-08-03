from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import secrets
import string
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from services.register import mail_provider
from services.register.openai_register import (
    config,
    create_session,
    log,
    request_platform_oauth_token,
    step,
)
from utils.pkce import generate_pkce as _generate_pkce

platform_base = "https://platform.openai.com"
auth_base = "https://auth.openai.com"
REGISTER_TIMEOUT = 120_000
OAUTH_AUTHORIZE_PATTERNS = (
    "**/oauth/authorize*",
    "**/api/oauth/oauth2/auth*",
    "**/api/accounts/authorize*",
)
OAUTH_CALLBACK_PATTERN = "**/auth/callback*"


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


async def _install_oauth_routes(page, index: int, code_challenge: str, captured_codes: list[str]) -> None:
    async def _intercept_authorize(route):
        """Replace PKCE on both the current and legacy authorize endpoints."""
        url = route.request.url
        original_challenge = str((parse_qs(urlparse(url).query).get("code_challenge") or [""])[0])
        if _trace_enabled():
            step(
                index,
                "OAuth trace: authorize PKCE "
                f"original_fp={_secret_fingerprint(original_challenge)}, "
                f"replacement_fp={_secret_fingerprint(code_challenge)}",
            )
        await route.continue_(url=_replace_pkce_params(url, code_challenge))

    async def _intercept_callback(route):
        """Capture the one-time code and prevent the callback page from consuming it."""
        parsed = urlparse(route.request.url)
        params = parse_qs(parsed.query)
        code = str((params.get("code") or [""])[0]).strip()
        if parsed.netloc != "platform.openai.com" or not code:
            await route.continue_()
            return
        if code not in captured_codes:
            captured_codes.append(code)
            step(index, "已拦截到 OAuth code")
            if _trace_enabled():
                step(index, f"OAuth trace: callback code_fp={_secret_fingerprint(code)}")
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body="<!doctype html><title>OAuth complete</title>",
        )

    for pattern in OAUTH_AUTHORIZE_PATTERNS:
        await page.route(pattern, _intercept_authorize)
    await page.route(OAUTH_CALLBACK_PATTERN, _intercept_callback)


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
    await _install_oauth_routes(page, index, code_challenge, captured_code)

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

    step(index, "输入密码")
    password_input = page.locator('input[name="password"], input[type="password"], input[id="password"]').first
    try:
        await password_input.wait_for(state="visible", timeout=30_000)
    except Exception:
        body = await _page_debug_info(page)
        raise RuntimeError(f"未找到密码输入框, url={page.url}, body={body}")
    await password_input.fill(password)

    submit_btn = page.locator('button[type="submit"], button:has-text("Continue"), button:has-text("继续")').first
    await submit_btn.click()
    await page.wait_for_timeout(5000)

    step(index, "等待验证码发送")
    await _wait_for_otp_page(page)

    step(index, "等待收取验证码")
    code = mail_provider.wait_for_code(config["mail"], mailbox)
    if not code:
        raise RuntimeError("等待注册验证码超时")
    step(index, f"收到注册验证码: {code}")

    step(index, "输入验证码")
    code_input = page.locator('input[name="code"], input[id="code"], input[type="text"]').first
    try:
        await code_input.wait_for(state="visible", timeout=15_000)
    except Exception:
        body = await _page_debug_info(page)
        raise RuntimeError(f"未找到验证码输入框, url={page.url}, body={body}")
    await code_input.fill(code)

    verify_btn = page.locator('button[type="submit"], button:has-text("Continue"), button:has-text("Verify")').first
    await verify_btn.click()
    await page.wait_for_timeout(5000)

    step(index, "填写账号信息")
    await _fill_profile(page, name, age, index)

    step(index, "等待获取 OAuth code")
    for _ in range(15):
        if captured_code:
            break
        current_url = page.url
        if "/auth/callback" in current_url or "code=" in current_url:
            parsed = urlparse(current_url)
            params = parse_qs(parsed.query)
            c = (params.get("code") or [""])[0]
            if c:
                captured_code.append(c)
                break
        await page.wait_for_timeout(2000)

    if not captured_code:
        raise RuntimeError(f"未能获取到 OAuth code, 最终页面: {page.url}")

    step(index, "用 OAuth code 换取 token")
    if _trace_enabled():
        step(
            index,
            "OAuth trace: external token exchange "
            f"code_fp={_secret_fingerprint(captured_code[0])}, "
            f"verifier_fp={_secret_fingerprint(code_verifier)}",
        )
    session = create_session(proxy)
    try:
        tokens = request_platform_oauth_token(session, captured_code[0], code_verifier)
    finally:
        session.close()

    if not tokens or not tokens.get("access_token"):
        raise RuntimeError("OAuth token 交换返回数据缺少 access_token")

    step(index, "注册完成，token 获取成功")
    return {
        "email": email,
        "password": password,
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


async def _wait_for_otp_page(page) -> None:
    try:
        await page.locator('input[name="code"], input[id="code"]').first.wait_for(
            state="visible", timeout=30_000
        )
    except Exception:
        current_url = page.url
        body_text = await page.inner_text("body")
        if "verify" in body_text.lower() or "code" in body_text.lower():
            return
        raise RuntimeError(f"未进入验证码页面, url={current_url}, body={body_text[:300]}")


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
    if await age_input.count() > 0:
        try:
            await age_input.fill(age)
            step(index, f"已填写年龄: {age}")
            age_filled = True
        except Exception:
            pass

    if not age_filled:
        date_input = page.locator('input[name="birthdate"], input[name="birthday"], input[type="date"], input[placeholder*="YYYY"], input[placeholder*="MM/DD"], input[placeholder*="birth" i]').first
        if await date_input.count() > 0:
            birthdate = _random_birthdate()
            try:
                await date_input.fill(birthdate)
                step(index, f"已填写生日: {birthdate}")
                age_filled = True
            except Exception:
                pass

    if not age_filled:
        all_inputs = page.locator("input")
        count = await all_inputs.count()
        if count >= 2:
            await all_inputs.nth(1).fill(age)
            step(index, f"通过第二个 input 填写年龄: {age}")

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
