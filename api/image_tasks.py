from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.image_task_service import image_task_service

# 客户端可能误传的内部字段：透传会改掉服务端的请求预算或触发流式返回。
_INTERNAL_PARAM_KEYS = {"timeout_secs", "progress_callback", "base_url", "stream"}


def _extra_params(params: dict[str, object] | None) -> dict[str, object]:
    return {key: value for key, value in (params or {}).items() if key not in _INTERNAL_PARAM_KEYS}


class ImageGenerationTaskRequest(BaseModel):
    # extra=allow：保留客户端多传的扩展参数，任务日志里才能看到它们。
    model_config = ConfigDict(extra="allow")
    client_task_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    size: str | None = None
    quality: str = "auto"


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-tasks")
    async def list_image_tasks(
        ids: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(image_task_service.list_tasks, identity, _parse_task_ids(ids))

    @router.post("/api/image-tasks/generations")
    async def create_generation_task(
        body: ImageGenerationTaskRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                image_task_service.submit_generation,
                identity,
                client_task_id=body.client_task_id,
                prompt=body.prompt,
                model=body.model,
                size=body.size,
                quality=body.quality,
                base_url=resolve_image_base_url(request),
                extra_params=_extra_params(body.model_extra),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-tasks/edits")
    async def create_edit_task(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources, extra_params = await parse_image_edit_request(request)
        client_task_id = str(payload.get("client_task_id") or "").strip()
        if not client_task_id:
            raise HTTPException(status_code=400, detail={"error": "client_task_id is required"})
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        images = await read_image_sources(image_sources)
        masks = await read_image_sources(mask_sources) if mask_sources else None
        try:
            return await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=client_task_id,
                prompt=prompt,
                model=model,
                size=payload["size"],
                quality=payload["quality"],
                base_url=resolve_image_base_url(request),
                images=images,
                masks=masks,
                extra_params=_extra_params(extra_params),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    return router
