"""只读探针：打开 platform.openai.com/signup，记录 OAuth authorize 请求的真实 URL。

不填写邮箱、不创建账号，仅用于确认 authorize 请求的端点与查询串形态。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

TARGET_HOSTS = {"auth.openai.com", "platform.openai.com"}
EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            executable_path=EXECUTABLE_PATH,
            args=[
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await context.new_page()

        seen: list[str] = []

        def on_request(request) -> None:
            parsed = urlparse(request.url)
            if parsed.netloc not in TARGET_HOSTS:
                return
            line = f"{request.method} {parsed.netloc}{parsed.path}"
            if line in seen:
                return
            seen.append(line)
            if "authorize" in parsed.path or "oauth" in parsed.path:
                print(f"[REQ] {request.method} {request.url}", flush=True)
                keys = sorted(parse_qs(parsed.query).keys())
                print(f"      query_keys={keys}", flush=True)
                raw_query = parsed.query
                print(f"      query_has_literal_slash={'/' in raw_query}", flush=True)
            else:
                print(f"[REQ] {line}", flush=True)

        page.on("request", on_request)

        print("goto platform.openai.com/signup", flush=True)
        try:
            await page.goto(
                "https://platform.openai.com/signup",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as error:
            print(f"goto error: {error}", flush=True)
        await page.wait_for_timeout(12_000)
        print(f"final url: {page.url}", flush=True)
        try:
            body = await page.inner_text("body")
            print(f"body: {body[:300]!r}", flush=True)
        except Exception as error:
            print(f"body unreadable: {error}", flush=True)
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"probe failed: {exc}", file=sys.stderr, flush=True)
        raise
