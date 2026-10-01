"""端到端验证修复机制：callback 页面（由 302 跳转而来）自己发出的请求会被拦掉。

用本地 HTTP 服务模拟平台：
  /redirect -> 302 -> /auth/callback?code=one-time-code
  /auth/callback 页面会立刻 fetch('/consume')、加载 /app.js 和一张图片
期望：
  - /consume 与 /app.js 被 abort（平台无法用掉 code），图片正常加载
  - 普通注册页 /signup 的 fetch 不受影响
"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import threading
from pathlib import Path

from playwright.async_api import async_playwright

from services.register.playwright_register import _should_block_browser_oauth_request

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)

HITS: list[str] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: str = "", content_type: str = "text/html; charset=utf-8", location: str = "") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        if location:
            self.send_header("location", location)
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        HITS.append(self.path)
        if self.path.startswith("/redirect"):
            self._send(302, location="/auth/callback?code=one-time-code&state=s1")
            return
        if self.path.startswith("/auth/callback"):
            # 平台回调页：一次性 code 会在这里被用掉
            self._send(200, "<html><body>callback<script src='/app.js'></script>"
                            "<script>fetch('/consume')</script><img src='/pixel.png'></body></html>")
            return
        if self.path.startswith("/signup"):
            self._send(200, "<html><body>signup<script>fetch('/api/ok')</script></body></html>")
            return
        if self.path.startswith("/app.js"):
            self._send(200, "console.log('app');", "application/javascript")
            return
        self._send(200, "", "image/png")

    def do_POST(self) -> None:  # noqa: N802
        HITS.append(self.path)
        self._send(200, "")

    def log_message(self, *args) -> None:
        pass


async def main() -> None:
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=EXECUTABLE_PATH, args=["--headless=new", "--no-sandbox"]
            )
            page = await browser.new_page()
            blocked: list[str] = []

            async def handler(route) -> None:
                reason = _should_block_browser_oauth_request(route.request)
                if reason:
                    blocked.append(f"{route.request.resource_type} {route.request.url}")
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            await page.route("**/*", handler)

            await page.goto(f"http://127.0.0.1:{port}/redirect", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)
            print(f"callback url reached: {page.url}", flush=True)
            print(f"blocked by route: {blocked}", flush=True)
            consumed_after_callback = any(p.startswith("/consume") for p in HITS)
            print(f"platform consumed the code: {consumed_after_callback}", flush=True)

            HITS.clear()
            blocked.clear()
            await page.goto(f"http://127.0.0.1:{port}/signup", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1500)
            print(f"signup fetch blocked: {blocked}", flush=True)
            print(f"signup server hits: {HITS}", flush=True)
            await browser.close()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
