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

from curl_cffi import requests as curl_requests

from services.register import mail_provider
from services.register.openai_register import (
    config,
    log,
    platform_auth0_client,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
    request_platform_oauth_token,
    step,
)
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
PASSWORD_REJECTION_SELECTOR = 'text="Failed to create account. Please try again."'
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


async def _locator_is_visible(page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


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
        if await _locator_is_visible(page, PASSWORD_REJECTION_SELECTOR):
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


async def _exchange_oauth_token_in_browser(
    page, context, index: int, code: str, code_verifier: str, proxy: str
) -> dict:
    payload = {
        "client_id": platform_oauth_client_id,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": platform_oauth_redirect_uri,
    }
    browser_headers = {
        "accept": "*/*",
        "auth0-client": platform_auth0_client,
        "content-type": "application/json",
        "origin": platform_base,
        "referer": f"{platform_base}/",
    }

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
        try:
            return request_platform_oauth_token(session, code, code_verifier)
        except Exception as error:
            step(index, f"Chrome OAuth token 交换失败，尝试 Playwright 会话: {error}", "yellow")
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

    password_set = await _run_signup_state_machine(
        page, index, password, name, age, mailbox, captured_code
    )
    if not password_set:
        step(index, "当前流程未提供密码设置入口，账号将按无密码方式保存", "yellow")

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
    tokens = await _exchange_oauth_token_in_browser(
        page, context, index, captured_code[0], code_verifier, proxy
    )

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
