from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from api.support import require_admin, require_identity, resolve_image_base_url
from services.account_service import account_service
from services.config import config
from services.image_service import (
    delete_images,
    download_images_zip,
    get_image_download_response,
    get_image_response,
    get_thumbnail_response,
    list_images,
    storage_stats,
)
from services.image_tags_service import delete_tag, get_all_tags, set_tags
from services.log_cleanup_service import cleanup_logs, log_storage_info
from services.log_service import LOG_EXPORT_DEFAULT_LIMIT, log_service
from services.proxy_service import test_proxy
from services.stats_service import build_image_stats
from utils.beijing_time import beijing_now_text


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProxyTestRequest(BaseModel):
    url: str = ""


class ImageDeleteRequest(BaseModel):
    paths: list[str] = []
    start_date: str = ""
    end_date: str = ""
    all_matching: bool = False

class ImageDownloadRequest(BaseModel):
    paths: list[str]

class ImageTagsRequest(BaseModel):
    path: str
    tags: list[str]

class LogDeleteRequest(BaseModel):
    ids: list[str] = []
class LogCleanupRequest(BaseModel):
    days: int | None = None


def create_router(app_version: str) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/login")
    async def login(authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        return {
            "ok": True,
            "version": app_version,
            "role": identity.get("role"),
            "subject_id": identity.get("id"),
            "name": identity.get("name"),
        }

    @router.get("/version")
    async def get_version():
        return {"version": app_version}

    @router.get("/api/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"config": config.get()}

    @router.post("/api/settings")
    async def save_settings(body: SettingsUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"config": config.update(body.model_dump(mode="python"))}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/images")
    async def get_images(request: Request, start_date: str = "", end_date: str = "", authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return list_images(resolve_image_base_url(request), start_date=start_date.strip(), end_date=end_date.strip())

    @router.get("/images/{image_path:path}", include_in_schema=False)
    async def get_image(image_path: str):
        return get_image_response(image_path)

    @router.get("/image-thumbnails/{image_path:path}", include_in_schema=False)
    async def get_image_thumbnail(image_path: str):
        return get_thumbnail_response(image_path)

    @router.post("/api/images/delete")
    async def delete_images_endpoint(body: ImageDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return delete_images(body.paths, start_date=body.start_date.strip(), end_date=body.end_date.strip(), all_matching=body.all_matching)

    @router.post("/api/images/download")
    async def download_images_endpoint(body: ImageDownloadRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        buf = download_images_zip(body.paths)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="images.zip"'},
        )

    @router.get("/api/images/download/{image_path:path}")
    async def download_single_image_endpoint(image_path: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return get_image_download_response(image_path)

    @router.get("/api/logs")
    async def get_logs(type: str = "", status: str = "", start_date: str = "", end_date: str = "",
                       authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": log_service.list(
            type=type.strip(),
            status=status.strip(),
            start_date=start_date.strip(),
            end_date=end_date.strip(),
        )}

    @router.get("/api/logs/export")
    async def export_logs(type: str = "", status: str = "", start_date: str = "", end_date: str = "",
                          limit: int = LOG_EXPORT_DEFAULT_LIMIT,
                          authorization: str | None = Header(default=None)):
        """导出最近 N 条日志为 txt，报文完整，方便直接复制出来排查。"""
        require_admin(authorization)
        text, count = await run_in_threadpool(
            log_service.build_export, type.strip(), status.strip(), start_date.strip(), end_date.strip(), limit,
        )
        stamp = beijing_now_text().replace(" ", "-").replace(":", "")
        return Response(
            content=text,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="logs-{stamp}.txt"',
                # 前端用来提示导出了几条；拿不到也不影响下载。
                "X-Exported-Count": str(count),
            },
        )

    @router.post("/api/logs/delete")
    async def delete_logs(body: LogDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return log_service.delete(body.ids)

    @router.get("/api/logs/retention")
    async def get_log_retention(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return log_storage_info()

    @router.post("/api/logs/cleanup")
    async def cleanup_logs_endpoint(body: LogCleanupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if body.days is not None and body.days < 1:
            raise HTTPException(status_code=400, detail={"error": "保留天数至少为 1"})
        return await run_in_threadpool(cleanup_logs, body.days)

    @router.get("/api/stats/images")
    async def get_image_stats(
        start_date: str = "",
        end_date: str = "",
        scope: str = "",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(build_image_stats, start_date.strip(), end_date.strip(), scope.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/stats/inflight")
    async def get_inflight_stats(authorization: str | None = Header(default=None)):
        """号池当前在途的生图请求数，供统计页「当前请求数」卡片单独刷新。

        只查内存态计数，不读盘，所以可以随便点刷新。
        """
        require_admin(authorization)
        return {"count": account_service.image_inflight_total()}

    @router.post("/api/proxy/test")
    async def test_proxy_endpoint(body: ProxyTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"result": await run_in_threadpool(test_proxy, (body.url or "").strip())}

    @router.get("/api/storage/info")
    async def get_storage_info(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        storage = config.get_storage_backend()
        return {
            "backend": storage.get_backend_info(),
            "health": storage.health_check(),
        }

    @router.get("/api/images/tags")
    async def list_image_tags(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"tags": get_all_tags()}

    @router.post("/api/images/tags")
    async def update_image_tags(body: ImageTagsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        rel = body.path.strip().lstrip("/")
        if not rel:
            raise HTTPException(status_code=400, detail={"error": "path is required"})
        tags = set_tags(rel, body.tags)
        return {"ok": True, "tags": tags}

    @router.delete("/api/images/tags/{tag}")
    async def delete_image_tag(tag: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        count = delete_tag(tag)
        return {"ok": True, "removed_from": count}

    @router.get("/api/images/storage")
    async def get_image_storage(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return storage_stats()

    @router.get("/health", response_model=None)
    async def health_dashboard(format: str = Query(default="html")):
        stats = account_service.get_stats()
        storage = config.get_storage_backend()
        storage_health = storage.health_check()
        healthy = stats["active"] > 0 or stats["unlimited_quota_count"] > 0

        stats_json = {
            "status": "ok" if healthy else "degraded",
            "healthy": healthy,
            "version": app_version,
            "storage": {"backend": storage.get_backend_info(), "health": storage_health},
            "accounts": stats,
        }
        if format == "json":
            return stats_json
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>号池健康监控 - chatgpt2api</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
.header{{background:#1a1d27;border-bottom:1px solid #2a2d3a;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}}
.header h1{{font-size:20px}}
.status-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}}
.status-ok{{background:#22c55e;box-shadow:0 0 8px #22c55e88}}
.status-degraded{{background:#f59e0b;box-shadow:0 0 8px #f59e0b88}}
.container{{max-width:960px;margin:0 auto;padding:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}}
.card{{background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:16px}}
.card .value{{font-size:28px;font-weight:700;margin:4px 0}}
.card .label{{font-size:13px;color:#94a3b8}}
.green{{color:#22c55e}}.yellow{{color:#f59e0b}}.red{{color:#ef4444}}.blue{{color:#6c63ff}}
table{{width:100%;border-collapse:collapse;background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;overflow:hidden}}
th{{background:#242836;font-weight:600;text-align:left;padding:10px 12px;font-size:12px;color:#94a3b8;text-transform:uppercase}}
td{{padding:8px 12px;border-top:1px solid #2a2d3a;font-size:14px}}tr:hover td{{background:rgba(108,99,255,.05)}}
.api-url{{font-family:monospace;font-size:12px;color:#6c63ff}}
.refresh{{font-size:12px;color:#64748b;text-align:center;margin-top:24px}}
</style>
<meta http-equiv="refresh" content="30">
</head>
<body>
<div class="header">
<h1><span class="status-dot {'status-ok' if healthy else 'status-degraded'}"></span>号池健康监控</h1>
<div style="font-size:13px;color:#94a3b8">v{app_version} · 30s 自动刷新</div>
</div>
<div class="container">
<div class="cards">
<div class="card"><div class="label">号池状态</div><div class="value {'green' if healthy else 'yellow'}">{'正常' if healthy else '异常'}</div></div>
<div class="card"><div class="label">当前账号</div><div class="value blue">{stats['total']}</div></div>
<div class="card"><div class="label">累计入库</div><div class="value">{stats['cumulative_total']}</div></div>
<div class="card"><div class="label">可用账号</div><div class="value green">{stats['active']}</div></div>
<div class="card"><div class="label">无限额</div><div class="value">{stats['unlimited_quota_count']}</div></div>
<div class="card"><div class="label">剩余额度</div><div class="value">{stats['total_quota']}</div></div>
<div class="card"><div class="label">限流</div><div class="value yellow">{stats['limited']}</div></div>
<div class="card"><div class="label">异常</div><div class="value red">{stats['abnormal']}</div></div>
<div class="card"><div class="label">禁用</div><div class="value">{stats['disabled']}</div></div>
<div class="card"><div class="label">成功/失败</div><div class="value">{stats['total_success']}<span style="font-size:18px;color:#94a3b8">/</span><span class="red">{stats['total_fail']}</span></div></div>
</div>
<h2 style="margin-bottom:12px;font-size:16px">账号类型分布</h2>
<table>
<tr><th>类型</th><th>数量</th></tr>
{''.join(f'<tr><td>{t}</td><td>{c}</td></tr>' for t,c in sorted(stats['by_type'].items()))}
</table>
<div class="refresh">JSON: <span class="api-url">/health?format=json</span></div>
</div></body></html>""")

    return router
