"""sentinel 求解服务：常驻一个无头 Chrome，在真实 chatgpt.com 页面上算 Turnstile / SO token。

2026-10 起 chatgpt.com 的对话/生图接口要求 Turnstile token 必须由真实 chatgpt.com
页面算出：Python/Node 模拟 VM 的结果会被判无效。失效时上游不报鉴权错误，而是把
请求伪装成 404 ``{"detail":"Model not found"}``，所以「404 说不清是没号还是没过校验」
这件事只能靠这个服务解决。

两条求解路径，优先用第一条：

1. 页面自带的 turnstile 模块（``_SOLVE_VIA_SDK``）：直接 ``import()`` chatgpt.com 自己
   加载的那个 ``turnstile-*.js`` chunk，调它的 ``getEnforcementToken``。上游改前端时
   这条路径自动跟上，不用我们同步任何东西。
2. 移植的 VM（``vm.js``，``_SOLVE_VIA_VM``）：SDK 那条失败时的退路。它得跟着前端
   指令集的变动人工维护，所以只当兜底。

刻意保持成一个没有账号的匿名页面：
- 输入只有挑战参数（dx、p，以及整份 prepare 响应），不接受 access token、Cookie、用户 prompt；
- 主服务按环境变量里的 URL/KEY 调用它，容器不对外暴露端口。

环境变量：
- SENTINEL_SOLVER_KEY：可选。设置后所有接口要带 `Authorization: Bearer <key>`
- SENTINEL_SOLVER_PROXY：可选。浏览器出网代理，必须和主服务访问 chatgpt.com 的出口一致
- SENTINEL_SOLVER_USER_AGENT：可选。默认按实际 Chromium 版本拼一个 Linux Chrome UA
- SENTINEL_SOLVER_TIMEZONE：可选。页面时区（如 America/Los_Angeles），要和出口 IP 对齐
- SENTINEL_SOLVER_REFRESH_SECONDS：页面定期重开的秒数，默认 1800
- SENTINEL_SOLVER_HEADLESS：默认 true，排障时置 false 便于肉眼看页面
- PORT：监听端口，默认 8080
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright_stealth import Stealth
from pydantic import BaseModel

CHATGPT_URL = "https://chatgpt.com/"
# Cloudflare 拦截页的标题，出现任意一个都说明页面还没真正就绪。
CHALLENGE_TITLES = ("请稍候", "Just a moment", "Attention Required")
VM_SCRIPT = (Path(__file__).parent / "vm.js").read_text(encoding="utf-8")
# 时区/偏移由页面自己算，别写死偏移：洛杉矶一年里是 420 或 480（夏令时）。
PROFILE_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
window.__sentinelProfile = () => ({
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  timezone_offset_min: new Date().getTimezoneOffset(),
});
"""
BASE_INIT_SCRIPT = PROFILE_SCRIPT

# 优先走这条：直接 import 这个页面自己加载的 turnstile chunk，让上游前端那份代码
# 在真实环境里算 token。移植的 VM 得跟着前端指令集一起改，调它自带模块不用。
#
# chunk 文件名带内容 hash，每次前端发布都会变，所以解题时现找，不写死。
# `_PINNED_ASSET` 只是三层兜底里的最后一层。
_PINNED_ASSET = "/unauth-mweb/assets/turnstile-DykPLDEM.js"
_DISCOVER_TURNSTILE_ASSET = """
() => {
  const pattern = /\\/turnstile-[^/"']+\\.js/;
  const seen = [];
  const push = (url) => { if (url && pattern.test(url) && !seen.includes(url)) seen.push(url); };
  try { performance.getEntriesByType('resource').forEach((e) => push(e.name)); } catch (err) {}
  try { Array.from(document.scripts).forEach((s) => push(s.src)); } catch (err) {}
  return seen;
}
"""
_SCAN_ASSET_IN_SCRIPTS = """
async (pattern) => {
  const re = new RegExp(pattern);
  for (const script of Array.from(document.scripts)) {
    if (!script.src) continue;
    try {
      const text = await (await fetch(script.src)).text();
      const hit = text.match(re);
      if (hit) return hit[0];
    } catch (err) {}
  }
  return null;
}
"""
_SOLVE_VIA_SDK = """
async ([requirements, key, asset]) => {
  try {
    const mod = await import(asset);
    const turnstile = mod.turnstile || (mod.default && mod.default.turnstile);
    if (!turnstile || typeof turnstile.getEnforcementToken !== 'function') {
      return 'ERR:getEnforcementToken missing';
    }
    // SDK 会拿 requirements 对象去 WeakMap 里查它自己那份 seed；这里把那把 key
    // 换成我们提交的 p token，等于让它用我们的挑战材料算。
    const original = WeakMap.prototype.get;
    WeakMap.prototype.get = function (target) {
      return target === requirements ? key : original.call(this, target);
    };
    try {
      return String(await turnstile.getEnforcementToken(requirements));
    } finally {
      WeakMap.prototype.get = original;
    }
  } catch (error) {
    return 'ERR:' + String(error).slice(0, 300);
  }
}
"""
_SOLVE_VIA_VM = "([dx, p]) => window.__sentinelVM.turnstile(dx, p)"

SOLVER_KEY = os.getenv("SENTINEL_SOLVER_KEY", "").strip()
PROXY = os.getenv("SENTINEL_SOLVER_PROXY", "").strip()
USER_AGENT = os.getenv("SENTINEL_SOLVER_USER_AGENT", "").strip()
TIMEZONE = os.getenv("SENTINEL_SOLVER_TIMEZONE", "").strip()
REFRESH_SECONDS = max(60, int(os.getenv("SENTINEL_SOLVER_REFRESH_SECONDS", "1800")))
HEADLESS = os.getenv("SENTINEL_SOLVER_HEADLESS", "true").strip().lower() not in {"0", "false", "no"}
# vm.js 里的 VM 自己守着前端那套预算（turnstile 500ms、SO 60s），慢不算错。
# 页面自带的模块第一次用要现取现解析，得给它留足时间——page.evaluate 没有单次超时，
# 只能靠 context 的默认超时兜底。
SDK_SOLVE_TIMEOUT_MS = 120000
READY_TIMEOUT_SECONDS = 60


class SolverPage:
    """常驻一个过了 Cloudflare 校验的 chatgpt.com 页面，按需重开。"""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._opened_at = 0.0
        self._lock = asyncio.Lock()
        self._turnstile_asset = ""
        self.user_agent = USER_AGENT
        self.last_error = ""

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        try:
            launch_kwargs = {
                "headless": HEADLESS,
                # channel="chromium" 用的是完整 Chromium 的新版无头模式；默认的
                # headless shell 过不了 Cloudflare。
                "channel": "chromium",
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            }
            if PROXY:
                launch_kwargs["proxy"] = {"server": PROXY}
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            if not self.user_agent:
                major = self._browser.version.split(".")[0]
                self.user_agent = (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
                )
            await self.page()
        except Exception as exc:
            # 浏览器缺失/装坏、页面打不开都不算致命：进程留着，/health 把原因报出来，
            # 主服务那边会明确报错（不会退成本地模拟挑战），下一次请求还会再试一次
            # 重开。反过来说，如果在这里抛出去，容器会直接起不来并进入重启循环。
            self.last_error = str(exc)
            print(f"[sentinel-solver] startup failed, serving in degraded mode: {exc}", flush=True)

    async def stop(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def page(self) -> Page:
        async with self._lock:
            stale = self._page is None or time.monotonic() - self._opened_at > REFRESH_SECONDS
            if not stale and not await self._is_ready(self._page):
                stale = True
            if stale:
                await self._reopen()
            return self._page

    async def invalidate(self) -> None:
        async with self._lock:
            self._opened_at = 0.0

    async def _reopen(self) -> None:
        if self._browser is None:
            raise RuntimeError("chromium is not available; check the solver logs")
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        self._page = None
        # 新页面可能已经换了一版前端，chunk 的文件名跟着变，缓存作废重找。
        self._turnstile_asset = ""
        context_kwargs = {
            # 页面语言决定了 composer 插入的图片工具提及文本（中文页是 @创建图像），
            # 主服务发上去的 prompt 前缀和偏移量就是按这套文案写的。
            "locale": "zh-CN",
            "viewport": {"width": 1280, "height": 800},
            "user_agent": self.user_agent,
        }
        if TIMEZONE:
            context_kwargs["timezone_id"] = TIMEZONE
        context = await self._browser.new_context(**context_kwargs)
        # page.evaluate 没有单次超时，页面自带模块第一次用可能要现取现解析。
        context.set_default_timeout(SDK_SOLVE_TIMEOUT_MS)
        await context.add_init_script(BASE_INIT_SCRIPT)
        await context.add_init_script(VM_SCRIPT)
        await Stealth().apply_stealth_async(context)
        page = await context.new_page()
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while not await self._is_ready(page):
            if time.monotonic() > deadline:
                title = await page.title()
                await context.close()
                raise RuntimeError(f"chatgpt.com page not ready (title={title!r}), likely stuck on a Cloudflare challenge")
            await asyncio.sleep(1)
        self._context = context
        self._page = page
        self._opened_at = time.monotonic()
        self.last_error = ""
        print(f"[sentinel-solver] chatgpt.com page ready: {await page.title()!r}", flush=True)

    @staticmethod
    async def _is_ready(page: Page | None) -> bool:
        if page is None or page.is_closed():
            return False
        try:
            title = await page.title()
            if any(marker in title for marker in CHALLENGE_TITLES):
                return False
            return await page.evaluate("location.hostname === 'chatgpt.com' && typeof window.__sentinelVM === 'object'")
        except Exception:
            return False

    async def turnstile_asset(self) -> str:
        """找到 chatgpt.com 自己那份 turnstile chunk 的 URL。

        文件名带内容 hash，每次前端发布都变，所以先看这个页面上实际加载了哪些资源，
        再退一步去翻页面自己的 script 里有没有那个路径的字面量；两者都失手才用写死的
        路径。找到就缓存住，页面重开时才重新找。
        """
        if self._turnstile_asset:
            return self._turnstile_asset
        try:
            loaded = await self.evaluate(_DISCOVER_TURNSTILE_ASSET, None)
            if loaded:
                self._turnstile_asset = str(loaded[0])
                print(f"[sentinel-solver] turnstile asset (loaded): {self._turnstile_asset}", flush=True)
                return self._turnstile_asset
            scanned = await self.evaluate(_SCAN_ASSET_IN_SCRIPTS, r"/unauth-mweb/assets/turnstile-[A-Za-z0-9_-]+\.js")
            if scanned:
                self._turnstile_asset = str(scanned)
                print(f"[sentinel-solver] turnstile asset (scanned): {self._turnstile_asset}", flush=True)
                return self._turnstile_asset
        except Exception as exc:
            self.last_error = str(exc)
        print(f"[sentinel-solver] turnstile asset discovery fell back to {_PINNED_ASSET}", flush=True)
        self._turnstile_asset = _PINNED_ASSET
        return self._turnstile_asset

    async def solve_turnstile(self, requirements: dict[str, Any] | None, dx: str, p: str) -> tuple[bool, str]:
        """算 Turnstile token：先调页面自带的模块，失败再退到移植的 VM。

        返回 ``(是否成功, token 或错误原因)``。有 ``requirements`` 说明调用方把整份
        prepare 响应转来了，页面自带模块可以直接吃这个对象；只有 dx 的话就只剩 VM 这条
        退路，而 VM 在当前上游版本上多半过不了校验。
        """
        if requirements:
            asset = await self.turnstile_asset()
            try:
                result = str(await self.evaluate(_SOLVE_VIA_SDK, [requirements, p, asset]) or "")
            except Exception as exc:
                result = f"ERR:{exc}"
            if result and not result.startswith("ERR:"):
                return True, result
            self.last_error = result[:300]
            print(f"[sentinel-solver] SDK solve failed via {asset}: {result[:300]}", flush=True)
            # 缓存和页面一起丢：下次重新找 chunk、重开页面。
            self._turnstile_asset = ""
            await self.invalidate()
        if not dx:
            return False, self.last_error or "no dx and no requirements supplied"
        result = str(await self.evaluate(_SOLVE_VIA_VM, [dx, p]) or "")
        if result.startswith("ERR:"):
            return False, result[4:]
        return bool(result), result

    async def evaluate(self, expression: str, arg: object) -> object:
        """在页面里执行；页面失效时重开一次再试。"""
        for attempt in range(2):
            page = await self.page()
            try:
                return await page.evaluate(expression, arg)
            except Exception as exc:
                self.last_error = str(exc)
                if attempt:
                    raise
                await self.invalidate()
        raise RuntimeError("unreachable")

    async def profile(self) -> dict[str, object]:
        """页面自己算出来的时区/偏移（浏览器会处理夏令时）。"""
        try:
            value = await self.evaluate("() => window.__sentinelProfile()", None)
        except Exception as exc:
            self.last_error = str(exc)
            return {}
        return value if isinstance(value, dict) else {}

    def status(self) -> dict[str, object]:
        return {
            "ready": self._page is not None and not self._page.is_closed(),
            "page_age_seconds": int(time.monotonic() - self._opened_at) if self._opened_at else None,
            "browser_version": self._browser.version if self._browser else None,
            "user_agent": self.user_agent,
            "timezone": TIMEZONE or None,
            "last_error": self.last_error or None,
        }


solver = SolverPage()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await solver.start()
    yield
    await solver.stop()


app = FastAPI(title="chatgpt2api sentinel solver", lifespan=lifespan)


class TurnstileRequest(BaseModel):
    p: str
    # 只有 dx 时走移植的 VM（当前上游版本上多半过不了）。
    dx: str = ""
    # 整份 ``/chat-requirements/prepare`` 响应：里面只有挑战材料（prepare_token、dx、
    # PoW seed、SO 句柄），没有账号 token、Cookie 或用户 prompt。
    requirements: dict[str, Any] | None = None


class SORequest(BaseModel):
    collector_dx: str
    snapshot_dx: str
    p: str


def require_key(authorization: str | None) -> None:
    if not SOLVER_KEY:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, SOLVER_KEY):
        raise HTTPException(status_code=401, detail="invalid solver key")


@app.get("/health")
async def health() -> dict[str, object]:
    """主服务从这里取「要求和挑战环境一致」的那几个值：UA、时区、偏移。"""
    status = solver.status()
    status.update(await solver.profile())
    return status


@app.post("/v1/turnstile")
async def solve_turnstile(body: TurnstileRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    require_key(authorization)
    try:
        ok, token = await solver.solve_turnstile(body.requirements, body.dx, body.p)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"turnstile solve failed: {exc}") from exc
    if not ok:
        # 算不出来就明确报 503：主服务会照着失败处理，不会拿着半成品 token 去撞 404。
        raise HTTPException(status_code=503, detail=f"turnstile solve failed: {token[:300]}")
    return {"token": token}


@app.post("/v1/so")
async def solve_so(body: SORequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    require_key(authorization)
    try:
        token = await solver.evaluate(
            "([c, s, p]) => window.__sentinelVM.so(c, s, p)", [body.collector_dx, body.snapshot_dx, body.p]
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"so solve failed: {exc}") from exc
    return {"token": str(token or "")}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), access_log=False)
