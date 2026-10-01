"""验证：302 重定向目标能否被 context.route("**/*") 万用路由拦住（这是修复的关键）。"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import threading
from pathlib import Path

from playwright.async_api import async_playwright

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: str = "", location: str = "") -> None:
        self.send_response(status)
        if location:
            self.send_header("location", location)
        else:
            self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/redirect"):
            self._send(302, location="/auth/callback?code=from-redirect&state=s1")
            return
        if self.path.startswith("/chain"):
            self._send(302, location="/hop?n=1")
            return
        if self.path.startswith("/hop"):
            self._send(302, location="/auth/callback?code=from-chain&state=s2")
            return
        self._send(200, "<html><body>landed</body></html>")

    def log_message(self, *args) -> None:
        pass


async def scenario(label: str, path: str, use_context: bool) -> None:
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=EXECUTABLE_PATH, args=["--headless=new", "--no-sandbox"])
            context = await browser.new_context()
            page = await context.new_page()
            seen: list[str] = []

            async def catch_all(route) -> None:
                url = route.request.url
                seen.append(url)
                if "/auth/callback" in url:
                    print(f"  [CALLBACK INTERCEPTED] {url}", flush=True)
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<html>stub</html>")
                    return
                await route.continue_()

            if use_context:
                await context.route("**/*", catch_all)
            else:
                await page.route("**/*", catch_all)
            await page.goto(f"http://127.0.0.1:{port}{path}", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1000)
            print(f"{label}: requests_seen={len(seen)} callback_seen={any('/auth/callback' in u for u in seen)} final={page.url}", flush=True)
            await browser.close()
        server.shutdown()


async def main() -> None:
    await scenario("page.route  catch-all + 302 redirect", "/redirect", use_context=False)
    await scenario("context.route catch-all + 302 redirect", "/redirect", use_context=True)
    await scenario("context.route catch-all + 302 chain", "/chain", use_context=True)


if __name__ == "__main__":
    asyncio.run(main())
