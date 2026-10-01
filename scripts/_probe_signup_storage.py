"""只读探针：打开注册页后，导出浏览器端的 localStorage / sessionStorage / cookie，
寻找 PKCE code_verifier 是否被客户端保存（若在，就能直接用客户端的 verifier 换 token）。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)

DUMP_JS = """
() => {
  const dump = (store) => {
    const out = {};
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      out[k] = String(store.getItem(k)).slice(0, 400);
    }
    return out;
  };
  return {
    url: location.href,
    localStorage: dump(localStorage),
    sessionStorage: dump(sessionStorage),
    cookie: document.cookie,
  };
}
"""


async def dump(page, label: str) -> None:
    try:
        data = await page.evaluate(DUMP_JS)
    except Exception as error:
        print(f"[{label}] evaluate failed: {error}", flush=True)
        return
    print(f"--- {label} ---", flush=True)
    print(f"url={data['url']}", flush=True)
    print(f"cookie={data['cookie']!r}", flush=True)
    for store_name in ("localStorage", "sessionStorage"):
        store = data[store_name]
        print(f"{store_name}: {json.dumps(store, ensure_ascii=False)[:2000]}", flush=True)


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
        await page.goto("https://platform.openai.com/signup", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(8000)
        await dump(page, "signup page")
        for cookie in await context.cookies():
            name = str(cookie.get("name") or "")
            if name.startswith("__Secure-next-auth") or "challenge" in name or "verifier" in name:
                print(f"cookie {name} domain={cookie.get('domain')} value={str(cookie.get('value'))[:120]}", flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
