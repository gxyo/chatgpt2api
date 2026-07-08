from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread

from fastapi import HTTPException, Request
from fastapi.responses import Response

from services.account_service import account_service
from services.auth_service import auth_service
from services.config import config
from utils.helper import public_error_message

BASE_DIR = Path(__file__).resolve().parents[1]
WEB_DIST_DIR = BASE_DIR / "web_dist"
DISABLED_REFRESH_POLL_SECONDS = 60
AUTO_ACCOUNT_REFRESH_LOCK = Lock()
IMMUTABLE_WEB_ASSET_PREFIX = "_next/static/"


@dataclass(frozen=True)
class CachedWebAsset:
    mtime_ns: int
    size: int
    content: bytes
    media_type: str


_WEB_ASSET_CACHE: dict[Path, CachedWebAsset] = {}


WEB_MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _legacy_admin_identity(token: str) -> dict[str, object] | None:
    auth_key = str(config.auth_key or "").strip()
    if auth_key and token == auth_key:
        return {"id": "admin", "name": "管理员", "role": "admin"}
    return None


def require_identity(authorization: str | None) -> dict[str, object]:
    token = extract_bearer_token(authorization)
    identity = _legacy_admin_identity(token) or auth_service.authenticate(token)
    if identity is None:
        raise HTTPException(status_code=401, detail={"error": "密钥无效或已失效，请重新登录"})
    return identity


def require_auth_key(authorization: str | None) -> None:
    require_identity(authorization)


def require_admin(authorization: str | None) -> dict[str, object]:
    identity = require_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"error": "需要管理员权限才能执行这个操作"})
    return identity


def resolve_image_base_url(request: Request) -> str:
    return config.base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def raise_image_quota_error(exc: Exception) -> None:
    message = str(exc)
    if "no available image quota" in message.lower():
        raise HTTPException(status_code=429, detail={"error": "no available image quota"}) from exc
    raise HTTPException(status_code=502, detail={"error": public_error_message(exc)}) from exc


def sanitize_cpa_pool(pool: dict | None) -> dict | None:
    if not isinstance(pool, dict):
        return None
    return {key: value for key, value in pool.items() if key != "secret_key"}


def sanitize_cpa_pools(pools: list[dict]) -> list[dict]:
    return [sanitized for pool in pools if (sanitized := sanitize_cpa_pool(pool)) is not None]


def sanitize_sub2api_server(server: dict | None) -> dict | None:
    if not isinstance(server, dict):
        return None
    sanitized = {key: value for key, value in server.items() if key not in {"password", "api_key"}}
    sanitized["has_api_key"] = bool(str(server.get("api_key") or "").strip())
    return sanitized


def sanitize_sub2api_servers(servers: list[dict]) -> list[dict]:
    return [sanitized for server in servers if (sanitized := sanitize_sub2api_server(server)) is not None]


def start_limited_account_watcher(stop_event: Event) -> Thread:
    def worker() -> None:
        while not stop_event.is_set():
            interval_minutes = config.refresh_account_interval_minute
            if not interval_minutes or interval_minutes <= 0:
                stop_event.wait(DISABLED_REFRESH_POLL_SECONDS)
                continue

            try:
                limited_tokens = account_service.list_limited_tokens()
                normal_tokens = account_service.list_normal_tokens()
                expiring_tokens = account_service.list_expiring_access_tokens()
                keepalive_tokens = account_service.list_refresh_token_keepalive_tokens()
                tokens = list(dict.fromkeys([*limited_tokens, *normal_tokens, *expiring_tokens]))
                expiring_token_set = set(expiring_tokens)
                keepalive_tokens = [token for token in keepalive_tokens if token not in expiring_token_set]
                if tokens:
                    print(
                        "[account-watcher] checking "
                        f"{len(limited_tokens)} limited accounts, "
                        f"{len(normal_tokens)} normal accounts, "
                        f"{len(expiring_tokens)} expiring access tokens"
                    )
                    with AUTO_ACCOUNT_REFRESH_LOCK:
                        account_service.refresh_accounts(tokens)
                if keepalive_tokens:
                    print(f"[account-watcher] keepalive {len(keepalive_tokens)} refresh tokens")
                    with AUTO_ACCOUNT_REFRESH_LOCK:
                        result = account_service.keepalive_refresh_tokens(keepalive_tokens)
                    if result.get("errors"):
                        print(f"[account-watcher] keepalive errors: {result['errors']}")
            except Exception as exc:
                print(f"[account-watcher] fail {exc}")

            interval_minutes = config.refresh_account_interval_minute
            wait_seconds = interval_minutes * 60 if interval_minutes and interval_minutes > 0 else DISABLED_REFRESH_POLL_SECONDS
            stop_event.wait(wait_seconds)

    thread = Thread(target=worker, name="account-watcher", daemon=True)
    thread.start()
    return thread


def start_all_account_watcher(stop_event: Event) -> Thread:
    def worker() -> None:
        while not stop_event.is_set():
            interval_minutes = config.refresh_all_accounts_interval_minute
            if not interval_minutes or interval_minutes <= 0:
                stop_event.wait(DISABLED_REFRESH_POLL_SECONDS)
                continue

            try:
                refreshable_tokens = account_service.list_refreshable_tokens()
                if refreshable_tokens:
                    print(f"[account-all-watcher] refreshing {len(refreshable_tokens)} non-disabled accounts")
                    with AUTO_ACCOUNT_REFRESH_LOCK:
                        account_service.refresh_accounts(refreshable_tokens)
            except Exception as exc:
                print(f"[account-all-watcher] fail {exc}")

            interval_minutes = config.refresh_all_accounts_interval_minute
            wait_seconds = interval_minutes * 60 if interval_minutes and interval_minutes > 0 else DISABLED_REFRESH_POLL_SECONDS
            stop_event.wait(wait_seconds)

    thread = Thread(target=worker, name="all-account-watcher", daemon=True)
    thread.start()
    return thread


def resolve_web_asset(requested_path: str) -> Path | None:
    if not WEB_DIST_DIR.exists():
        return None
    clean_path = requested_path.strip("/")
    base_dir = WEB_DIST_DIR.resolve()
    candidates = [base_dir / "index.html"] if not clean_path else [
        base_dir / Path(clean_path),
        base_dir / clean_path / "index.html",
        base_dir / f"{clean_path}.html",
    ]
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(base_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _web_asset_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in WEB_MEDIA_TYPES:
        return WEB_MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _cached_web_asset(path: Path) -> CachedWebAsset:
    stat = path.stat()
    cached = _WEB_ASSET_CACHE.get(path)
    if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
        return cached

    asset = CachedWebAsset(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        content=path.read_bytes(),
        media_type=_web_asset_media_type(path),
    )
    _WEB_ASSET_CACHE[path] = asset
    return asset


def web_asset_response(path: Path, requested_path: str) -> Response:
    asset = _cached_web_asset(path)
    clean_path = requested_path.strip("/")
    is_html = path.suffix.lower() == ".html"
    cache_control = (
        "public, max-age=31536000, immutable"
        if clean_path.startswith(IMMUTABLE_WEB_ASSET_PREFIX)
        else "no-store, max-age=0"
        if is_html
        else "public, max-age=3600"
    )
    return Response(
        content=asset.content,
        media_type=asset.media_type,
        headers={
            "Cache-Control": cache_control,
            "X-Content-Type-Options": "nosniff",
        },
    )
