"""探针：route.continue_(url=...) 改写之后，Request/Response 上报的是改写前还是改写后的 URL？

只为一件事：playwright_register.py 里那条
    code 由 /api/oauth/oauth2/auth 签发, 该请求 challenge_fp=..., 我们的 challenge_fp=..., 一致=否
是拿 response.request.url 里读出来的 challenge 跟我们的比对。如果 Playwright 在上报时
仍然给原始 URL，这条对比就永远显示"否"，不能当证据用；如果上报的是改写后的 URL，
那"否"就说明签发 code 的那次请求确实没走我们的改写。

跑法： .venv/Scripts/python.exe scripts/_probe_continue_url.py
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

ORIGINAL = "original-challenge"
REPLACEMENT = "our-challenge"
received: list[str] = []
events: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        received.append(self.path)
        body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handler(route):
            url = route.request.url
            if "code_challenge=" in url:
                await route.continue_(url=url.replace(ORIGINAL, REPLACEMENT))
                return
            await route.continue_()

        await page.route("**/*", handler)
        page.on("request", lambda r: events.append(f"request 事件 url={r.url}"))
        page.on(
            "response",
            lambda r: events.append(
                f"response 事件 status={r.status} response.url={r.url} response.request.url={r.request.url}"
            ),
        )

        await page.goto(f"http://127.0.0.1:{port}/entry?code_challenge={ORIGINAL}", wait_until="load")
        await page.wait_for_timeout(300)
        await browser.close()

    server.shutdown()
    print("服务器实际收到:", received)
    print("我们的 challenge:", REPLACEMENT, "| 页面原始 challenge:", ORIGINAL)
    for line in events:
        print(" ", line)


if __name__ == "__main__":
    asyncio.run(main())
