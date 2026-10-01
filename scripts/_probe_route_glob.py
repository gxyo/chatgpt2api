"""验证 Playwright glob 是否真的能匹配带 query 的 callback 导航（本地 HTTP 服务，无需外网）。"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)
PATTERN = "**/auth/callback*"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>real callback page</body></html>")

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
            hits: list[str] = []

            async def on_route(route) -> None:
                url = route.request.url
                params = parse_qs(urlparse(url).query)
                code = str((params.get("code") or [""])[0])
                hits.append(f"{route.request.resource_type} {url}")
                print(f"[ROUTE FIRED] resource_type={route.request.resource_type} code={code!r}", flush=True)
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<html>stub</html>")

            await page.route(PATTERN, on_route)
            target = f"http://127.0.0.1:{port}/auth/callback?code=oaistb_ac_ABC.def&scope=openid+email+profile+offline_access&state=xyz"
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            body = await page.inner_text("body")
            print(f"page.url={page.url}", flush=True)
            print(f"body={body!r}", flush=True)
            print(f"route hits={len(hits)}", flush=True)

            # 再验证 redirect 链上是否也能拦截
            hits.clear()
            target2 = f"http://127.0.0.1:{port}/auth/callback?code=second&state=s2"
            await page.goto(target2, wait_until="domcontentloaded", timeout=30_000)
            print(f"second navigation hits={len(hits)}", flush=True)
            await browser.close()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
