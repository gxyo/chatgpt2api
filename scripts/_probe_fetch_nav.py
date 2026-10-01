"""隔离实验：对 document 导航请求，哪种方式能拿到 302 的 Location 而不让浏览器跟过去。

方案 A: route.fetch()（默认跟随重定向）
方案 B: route.fetch(max_redirects=0)
方案 C: context.request.get(url, max_redirects=0) + route.fulfill(response=...)
方案 D: 不拦，仅监听 page.on("response") 能否看到 302 那一跳
"""
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

HITS: list[str] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: str = "", location: str = "") -> None:
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        if location:
            self.send_header("location", location)
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        HITS.append(self.path)
        if self.path.startswith("/hop"):
            self._send(302, location="/auth/callback?code=from-hop&state=s1")
            return
        if self.path.startswith("/start"):
            self._send(200, "<html><body>start<script>setTimeout(() => location.href='/hop', 300)</script></body></html>")
            return
        if self.path.startswith("/auth/callback"):
            self._send(200, "<html><body>REAL CALLBACK</body></html>")
            return
        self._send(200, "<html><body>home</body></html>")

    def log_message(self, *args) -> None:
        pass


async def scenario(label: str, mode: str) -> None:
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        HITS.clear()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=EXECUTABLE_PATH, args=["--headless=new", "--no-sandbox"]
            )
            context = await browser.new_context()
            page = await context.new_page()
            seen_responses: list[str] = []
            page.on("response", lambda r: seen_responses.append(f"{r.status} {r.url}"))

            async def handler(route) -> None:
                if route.request.resource_type != "document" or "/hop" not in route.request.url:
                    await route.continue_()
                    return
                try:
                    if mode == "A":
                        response = await asyncio.wait_for(route.fetch(), timeout=8)
                    elif mode == "B":
                        response = await asyncio.wait_for(route.fetch(max_redirects=0), timeout=8)
                    elif mode == "C":
                        api = await asyncio.wait_for(
                            context.request.get(route.request.url, max_redirects=0, fail_on_status_code=False),
                            timeout=8,
                        )
                        print(f"  [{label}] api status={api.status} location={api.headers.get('location')!r}")
                        await route.fulfill(status=api.status, headers=api.headers, body=await api.body())
                        return
                    else:
                        await route.continue_()
                        return
                    print(f"  [{label}] fetch ok status={response.status} location={response.headers.get('location')!r}")
                    await route.fulfill(response=response)
                except Exception as error:
                    print(f"  [{label}] {type(error).__name__}: {str(error).splitlines()[0][:120]}")
                    await route.continue_()

            await page.route("**/*", handler)
            await page.goto(f"{base}/start", wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(3000)
            print(f"[{label}] url={page.url} callback_hit={any('/auth/callback' in h for h in HITS)}")
            print(f"[{label}] responses seen={seen_responses}")
            await browser.close()
        server.shutdown()


async def main() -> None:
    for label, mode in (("A fetch()", "A"), ("B fetch(max0)", "B"), ("C api.get", "C"), ("D none", "D")):
        await scenario(label, mode)


if __name__ == "__main__":
    asyncio.run(main())
