"""端到端验证新方案：资料页提交后那一跳由我们预取，浏览器不再落到 callback 页。

用本地 HTTP 服务模拟平台：
  /about-you      -> 资料页，JS 跳 /start（模拟点击 Finish creating account）
  /start          -> 302 -> /auth/callback?code=from-redirect   ← 跳转目标拦不住，只能预取 Location
  /about-you-post -> 资料页，form POST /submit
  /submit         -> 302 -> /auth/callback?code=from-form
  /normal-start   -> 302 -> /landing（对照组：普通跳转必须原样生效）
期望：
  - 两个 case 都拿到 code，且 /auth/callback 从未被平台服务器命中
  - POST 只提交一次（不能因为预取而重复提交）
  - 对照组跳转后的 URL 和内容都正确
"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import threading
from pathlib import Path

from playwright.async_api import async_playwright

import services.register.playwright_register as playwright_register
from services.register.playwright_register import _install_oauth_routes

EXECUTABLE_PATH = os.getenv(
    "PROBE_CHROME",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"),
)

HITS: list[str] = []


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: str = "", content_type: str = "text/html; charset=utf-8", location: str = "") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("set-cookie", "probe-cookie=1; Path=/")
        if location:
            self.send_header("location", location)
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        HITS.append(f"GET {self.path}")
        # 按去掉 query 的路径精确匹配：startswith 会被前缀吃掉（/authorize-nav vs /authorize）。
        route = self.path.split("?", 1)[0]
        if route == "/about-you-post":
            self._send(200, "<html><body><form id=f method=post action=/submit>"
                            "<input name=x value=1></form>"
                            "<script>setTimeout(() => document.getElementById('f').submit(), 600)</script>"
                            "</body></html>")
            return
        if route == "/about-you":
            self._send(200, "<html><body>profile<script>setTimeout(() => location.href='/start', 600)</script></body></html>")
            return
        if route == "/start":
            self._send(302, location="/auth/callback?code=from-redirect&state=s1")
            return
        if route == "/normal-start":
            self._send(302, location="/landing")
            return
        if route == "/authorize":
            # 真实链路：authorize 自己 302 到 callback，Location 里带 code。
            self._send(302, location="/auth/callback?code=from-authorize&state=s3")
            return
        if route == "/about-you-auth":
            # 资料页上发起 authorize：这才是通往 callback 的那一跳。
            self._send(200, "<html><body>profile<script>setTimeout(() => "
                            "location.href='/authorize?code_challenge=browser-challenge&code_challenge_method=S256', 600)"
                            "</script></body></html>")
            return
        if route == "/about-you-auth-xhr":
            self._send(200, "<html><body>profile<script>setTimeout(() => "
                            "fetch('/authorize?code_challenge=browser-challenge&code_challenge_method=S256')"
                            ".then(r => r.text()).then(t => document.title = 'xhr:' + t.length), 600)"
                            "</script></body></html>")
            return
        if route == "/authorize-signup":
            # 注册页自己那次 authorize：普通页面加载，必须由浏览器自己跟这个 302。
            self._send(302, location="/signup-real")
            return
        if route == "/signup-real":
            self._send(200, "<html><body>real signup page<input id=email></body></html>")
            return
        if route == "/signup-start":
            self._send(200, "<html><body>signup<script>setTimeout(() => location.href="
                            "'/authorize-signup?screen_hint=signup&code_challenge=browser-challenge"
                            "&code_challenge_method=S256', 600)</script></body></html>")
            return
        if route == "/auth/callback":
            self._send(200, "<html><body>REAL CALLBACK PAGE</body></html>")
            return
        if route == "/landing":
            self._send(200, "<html><body>landing</body></html>")
            return
        self._send(200, "<html><body>start</body></html>")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        HITS.append(f"POST {self.path} body={body!r} ctype={self.headers.get('content-type')!r}")
        if self.path.startswith("/submit"):
            self._send(302, location="/auth/callback?code=from-form&state=s2")
            return
        self._send(404)

    def log_message(self, *args) -> None:
        pass


async def main() -> None:
    with Server(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        # 生产逻辑按 host 判定 callback，探针里把本地服务当成 OpenAI 主机
        playwright_register.OAUTH_HOSTS = frozenset({f"127.0.0.1:{port}"})
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=EXECUTABLE_PATH, args=["--headless=new", "--no-sandbox"]
            )
            context = await browser.new_context()

            async def run_case(label: str, url: str) -> None:
                """每个用例独立页面 + 独立 route state，避免互相污染。"""
                page = await context.new_page()
                captured: list[str] = []
                events: list[str] = []
                log: list[str] = []
                page.on(
                    "request",
                    lambda r: log.append(
                        f"{r.method} {r.url[len(base):]} rt={r.resource_type}"
                        + (f" from={r.redirected_from.url[len(base):]}" if r.redirected_from else "")
                    ),
                )
                await _install_oauth_routes(page, 7, "our-challenge", captured, events)
                HITS.clear()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                except Exception as error:
                    print(f"[{label}] goto failed: {type(error).__name__}: {str(error).splitlines()[0][:100]}")
                await page.wait_for_timeout(2500)
                try:
                    body = (await page.inner_text("body")).strip()[:60]
                except Exception:
                    body = "<no body>"
                print(f"[{label}] captured={captured} url={page.url[len(base):]} body={body!r}")
                print(f"[{label}] server hits={HITS}")
                print(f"[{label}] callback page fetched by server: {any('/auth/callback' in h for h in HITS)}")
                print(f"[{label}] requests={log}")
                print(f"[{label}] events={events}")
                await page.close()

            # 对照组：普通 302 必须照常生效
            await run_case("control", f"{base}/normal-start")
            # 用例 1：JS 跳转 -> 302 -> callback
            await run_case("js", f"{base}/about-you")
            # 用例 2：form POST -> 302 -> callback
            await run_case("post", f"{base}/about-you-post")
            # 用例 3：资料页上的 authorize 302 到 callback（文档导航）
            await run_case("auth-nav", f"{base}/about-you-auth")
            # 用例 4：同上但走 XHR
            await run_case("auth-xhr", f"{base}/about-you-auth-xhr")
            # 用例 5：注册页那次 authorize 是普通页面加载，必须原样交给浏览器跟 302
            await run_case("signup", f"{base}/signup-start")

            cookies = {c["name"]: c["value"] for c in await context.cookies()}
            print(f"[cookies] {cookies}")
            await browser.close()
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
