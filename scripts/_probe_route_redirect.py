"""验证 callback 经过 302 重定向 / POST 提交 / JS location 跳转时，page.route 是否仍能拦截。"""
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
PATTERN = "**/auth/callback*"


class Handler(http.server.BaseHTTPRequestHandler):
    def _html(self, body: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("location", "/auth/callback?code=from-redirect&state=s1")
            self.end_headers()
            return
        if self.path.startswith("/jsjump"):
            self._html("<html><body>jumping<script>location.href='/auth/callback?code=from-js&state=s2'</script></body></html>")
            return
        if self.path.startswith("/form"):
            self._html(
                "<html><body><form id=f method=post action=/submit><input name=x value=1></form>"
                "<script>document.getElementById('f').submit()</script></body></html>"
            )
            return
        self._html("<html><body>landed</body></html>")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/submit"):
            self.send_response(302)
            self.send_header("location", "/auth/callback?code=from-form&state=s3")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


async def main() -> None:
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=EXECUTABLE_PATH, args=["--headless=new", "--no-sandbox"])
            page = await browser.new_page()
            fired: list[str] = []

            async def on_route(route) -> None:
                fired.append(f"{route.request.resource_type} {route.request.url}")
                print(f"[ROUTE FIRED] {route.request.resource_type} {route.request.url}", flush=True)
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<html>stub</html>")

            await page.route(PATTERN, on_route)
            for label, path in (
                ("302 redirect", "/redirect"),
                ("js location", "/jsjump"),
                ("form POST -> 302", "/form"),
            ):
                fired.clear()
                await page.goto(f"http://127.0.0.1:{port}{path}", wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1500)
                print(f"== {label}: fired={len(fired)} url={page.url}", flush=True)
            await browser.close()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
