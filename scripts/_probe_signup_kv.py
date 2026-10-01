"""只读探针：hook Storage.setItem，抓取注册流程中客户端写入的本地存储键值，
用于确认 code_verifier 是否落在浏览器存储里（若在，可直接复用客户端的 verifier）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)

HOOK_JS = """
(() => {
  const hook = (proto, name) => {
    const original = proto.setItem;
    proto.setItem = function (key, value) {
      try {
        console.log('KVWRITE ' + name + ' ' + location.origin + ' ' + key + ' = ' + String(value).slice(0, 300));
      } catch (e) {}
      return original.apply(this, arguments);
    };
  };
  hook(Storage.prototype, 'storage');
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (/oauth|authorize|token/.test(url)) {
        console.log('FETCH ' + (init && init.method ? init.method : 'GET') + ' ' + String(url).slice(0, 300) + ' body=' + String((init && init.body) || '').slice(0, 200));
      }
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };
})();
"""


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            executable_path=EXECUTABLE_PATH,
            args=["--headless=new", "--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        await context.add_init_script(HOOK_JS)
        page = await context.new_page()

        def on_console(message) -> None:
            text = message.text
            if text.startswith("KVWRITE") or text.startswith("FETCH"):
                print(f"[{message.type}] {text}", flush=True)

        page.on("console", on_console)
        await page.goto("https://platform.openai.com/signup", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(10_000)
        print(f"final url: {page.url}", flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
