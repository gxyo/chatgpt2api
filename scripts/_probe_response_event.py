"""验证现在的做法：只观察响应，不重发任何请求。

链路模拟线上形状：资料页 -> POST/跳转 -> authorize(带 code_challenge) -> 302 -> callback。
检查三件事：
  1. code 是从那一跳响应的 location 里读到的
  2. 抓到 code 的那一刻，服务器还没收到 callback 请求（即比原来轮询 page.url 早得多）
  3. 每个请求服务器只收到一次（没有替浏览器重发）
"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import async_playwright

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)

HITS: list[str] = []


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


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
        route = self.path.split("?", 1)[0]
        if route == "/about-you":
            self._send(200, "<html><body>profile<script>setTimeout(() => "
                            "location.href='/authorize?code_challenge=browser-challenge"
                            "&code_challenge_method=S256', 600)</script></body></html>")
            return
        if route == "/about-you-xhr":
            self._send(200, "<html><body>profile<script>setTimeout(() => "
                            "fetch('/authorize?code_challenge=browser-challenge"
                            "&code_challenge_method=S256').then(r => r.text()), 600)</script></body></html>")
            return
        if route == "/authorize":
            self._send(302, location="/auth/callback?code=from-response&state=s9")
            return
        if route == "/auth/callback":
            self._send(200, "<html><body>REAL CALLBACK PAGE</body></html>")
            return
        self._send(200, "<html><body>home</body></html>")

    def log_message(self, *args) -> None:
        pass


async def scenario(label: str, page_path: str) -> None:
    with Server(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=EXECUTABLE_PATH, args=["--headless=new", "--no-sandbox"]
            )
            page = await browser.new_page()
            captured: list[dict] = []

            def on_response(response) -> None:
                try:
                    location = str((response.headers or {}).get("location") or "")
                except Exception:
                    return
                if not location or not (300 <= int(response.status) < 400):
                    return
                target = urljoin(str(response.url), location)
                parsed = urlparse(target)
                code = str((parse_qs(parsed.query).get("code") or [""])[0])
                if code and parsed.path == "/auth/callback":
                    captured.append({
                        "code": code,
                        "callback_already_hit": any("/auth/callback" in h for h in HITS),
                        "hits_at_capture": len(HITS),
                    })

            page.on("response", on_response)
            HITS.clear()
            await page.goto(f"{base}{page_path}", wait_until="domcontentloaded", timeout=15_000)
            await page.wait_for_timeout(2500)

            snapshot = captured[0] if captured else {}
            print(f"[{label}] captured={[c['code'] for c in captured]}")
            print(f"[{label}] 抓到 code 时 callback 是否已被请求: {snapshot.get('callback_already_hit')}")
            print(f"[{label}] 服务器收到: {HITS}")
            duplicates = [path for path, count in Counter(HITS).items() if count > 1]
            print(f"[{label}] 重复请求: {duplicates or '无'}")
            await browser.close()
        server.shutdown()


async def main() -> None:
    await scenario("nav", "/about-you")
    await scenario("xhr", "/about-you-xhr")


if __name__ == "__main__":
    asyncio.run(main())
