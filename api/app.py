from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from api import accounts, ai, image_tasks, register, system
from api.errors import install_exception_handlers
from api.support import (
    resolve_web_asset,
    start_all_account_watcher,
    start_limited_account_watcher,
    web_asset_response,
)
from services.config import config
from services.image_service import start_image_cleanup_scheduler
from services.log_cleanup_service import start_log_cleanup_scheduler


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        limited_account_thread = start_limited_account_watcher(stop_event)
        all_account_thread = start_all_account_watcher(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        log_cleanup_thread = start_log_cleanup_scheduler(stop_event)
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            limited_account_thread.join(timeout=1)
            all_account_thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            log_cleanup_thread.join(timeout=1)

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        # 日志导出的下载文件名和条数靠这两个响应头回传；跨域跑前端（开发时）也要能读到。
        expose_headers=["Content-Disposition", "X-Exported-Count"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(register.create_router())
    app.include_router(system.create_router(app_version))

    # HEAD 必须一并支持：Next.js 在 output: "export" 模式下预取路由前会先发 HEAD 探测，
    # 405 会让它把这些路由缓存标记为不可用，前端每次点导航都要现拉一次 RSC。
    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(request: Request, full_path: str):
        include_body = request.method != "HEAD"
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return web_asset_response(asset, full_path, include_body=include_body)
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return web_asset_response(fallback, "", include_body=include_body)

    return app
