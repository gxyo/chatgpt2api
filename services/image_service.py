from __future__ import annotations

import io
import json
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps

from services.config import config
from services.image_storage_service import IMAGE_EXTENSIONS, image_storage_service
from services.image_tags_service import load_tags, remove_tags
from utils.log import logger

THUMBNAIL_SIZE = (320, 320)
MEGABYTE = 1024 * 1024
IMAGE_CLEANUP_CHECK_SECONDS = 30
STORAGE_CLEANUP_CHECK_SECONDS = 1800
BEIJING_TZ = timezone(timedelta(hours=8))
IMAGE_CLEANUP_TIMEZONE_KEY = "+08:00"
_image_cleanup_state_lock = threading.Lock()


def _beijing_now() -> datetime:
    """Current Beijing time (UTC+8) as a naive datetime, regardless of the host timezone."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def _cleanup_empty_dirs(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _local_image_files() -> list[Path]:
    """Return managed image files on the local filesystem, across supported platforms."""
    root = config.images_dir
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]


def _managed_image_items() -> list[dict[str, object]]:
    """Load indexed images and discover local files missing from the index."""
    return image_storage_service.list_items("")


def _image_item_size(item: dict[str, object]) -> int:
    rel = str(item.get("path") or item.get("rel") or "").strip()
    if rel:
        try:
            local_path = config.images_dir.joinpath(*Path(_safe_relative_path(rel)).parts)
            if local_path.is_file() and not local_path.is_symlink():
                return max(0, local_path.stat().st_size)
        except (OSError, ValueError, HTTPException):
            pass
    try:
        return max(0, int(item.get("size") or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _image_item_age(item: dict[str, object], rel: str) -> float:
    created_at = str(item.get("created_at") or "").strip()
    if created_at:
        try:
            return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    try:
        local_path = config.images_dir.joinpath(*Path(_safe_relative_path(rel)).parts)
        return local_path.stat().st_mtime
    except (OSError, ValueError, HTTPException):
        return 0.0


def _remove_image_artifacts(rel: str) -> None:
    for thumbnail in (_thumbnail_path(rel), config.image_thumbnails_dir / _safe_relative_path(rel)):
        try:
            if thumbnail.is_file():
                thumbnail.unlink()
        except OSError:
            pass
    remove_tags(rel)


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _safe_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.images_dir.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return path


def get_image_response(relative_path: str) -> FileResponse | Response:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if image_storage_service.has_local(relative_path):
        return FileResponse(_safe_image_path(relative_path), headers=headers)
    return Response(content=image_storage_service.get_bytes(relative_path), media_type="image/png", headers=headers)


def _thumbnail_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    return config.image_thumbnails_dir / f"{rel}.png"


def thumbnail_url(base_url: str, relative_path: str) -> str:
    return f"{base_url.rstrip('/')}/image-thumbnails/{_safe_relative_path(relative_path)}"


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def ensure_thumbnail(relative_path: str) -> Path:
    target = _thumbnail_path(relative_path)
    source_mtime = 0.0
    source: Path | None = None
    if image_storage_service.has_local(relative_path):
        source = _safe_image_path(relative_path)
        source_mtime = source.stat().st_mtime
    if target.exists() and (not source_mtime or target.stat().st_mtime >= source_mtime):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        image_source = source if source is not None else io.BytesIO(image_storage_service.get_bytes(relative_path))
        with Image.open(image_source) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            image.save(target, format="PNG", optimize=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="failed to create thumbnail") from exc
    return target


def get_thumbnail_response(relative_path: str) -> FileResponse:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    return FileResponse(ensure_thumbnail(relative_path), headers=headers)


def get_image_download_response(relative_path: str) -> FileResponse:
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if image_storage_service.has_local(relative_path):
        path = _safe_image_path(relative_path)
        headers = {**cors_headers, "Content-Disposition": f'attachment; filename="{path.name}"'}
        return FileResponse(path, filename=path.name, headers=headers)
    rel = _safe_relative_path(relative_path)
    headers = {
        **cors_headers,
        "Content-Disposition": f'attachment; filename="{Path(rel).name}"',
    }
    return Response(
        content=image_storage_service.get_bytes(rel),
        media_type="image/png",
        headers=headers,
    )


def cleanup_image_thumbnails() -> int:
    thumbnails_root = config.image_thumbnails_dir
    removed = 0
    for path in thumbnails_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(thumbnails_root).as_posix()
        if not rel.endswith(".png") or not image_storage_service.exists(rel[:-4]):
            path.unlink()
            removed += 1
    _cleanup_empty_dirs(thumbnails_root)
    return removed

def list_images(base_url: str, start_date: str = "", end_date: str = "") -> dict[str, object]:
    config.cleanup_old_images()
    cleanup_image_thumbnails()
    all_tags = load_tags()
    items = [
        {
            **item,
            "url": str(item.get("url") or f"{base_url.rstrip('/')}/images/{item['path']}"),
            "thumbnail_url": thumbnail_url(base_url, str(item["path"])),
            "tags": all_tags.get(str(item["path"]), []),
        }
        for item in image_storage_service.list_items(base_url, start_date, end_date)
    ]
    groups: dict[str, list[dict[str, object]]] = {}
    for item in items:
        groups.setdefault(str(item["date"]), []).append(item)
    return {"items": items, "groups": [{"date": key, "items": value} for key, value in groups.items()]}


def delete_images(paths: list[str] | None = None, start_date: str = "", end_date: str = "", all_matching: bool = False) -> dict[str, int]:
    root = config.images_dir.resolve()
    targets = [
        str(item["path"])
        for item in image_storage_service.list_items("", start_date=start_date, end_date=end_date)
    ] if all_matching else (paths or [])
    removed = 0
    for item in targets:
        path = (root / item).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if image_storage_service.delete(item):
            removed += 1
        for thumbnail in (_thumbnail_path(item), config.image_thumbnails_dir / _safe_relative_path(item)):
            if thumbnail.is_file():
                thumbnail.unlink()
        remove_tags(item)
    _cleanup_empty_dirs(root)
    _cleanup_empty_dirs(config.image_thumbnails_dir)
    return {"removed": removed}


def _image_cleanup_state_path() -> Path:
    return config.images_dir.parent / "image_cleanup_state.json"


def _next_image_cleanup_at(now: datetime, interval_days: int, scheduled_time: str) -> datetime:
    hour, minute = (int(part) for part in scheduled_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=interval_days)
    return candidate


def _load_image_cleanup_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_image_cleanup_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def run_scheduled_image_cleanup_if_due(now: datetime | None = None) -> dict[str, int] | None:
    """Delete all images present at a due scheduled time using the date-delete path."""
    if not config.image_cleanup_schedule_configured:
        return None

    current = now or _beijing_now()
    interval_days = config.image_cleanup_interval_days
    scheduled_time = config.image_cleanup_time
    schedule_key = f"{interval_days}:{scheduled_time}@{IMAGE_CLEANUP_TIMEZONE_KEY}"
    state_path = _image_cleanup_state_path()

    with _image_cleanup_state_lock:
        state = _load_image_cleanup_state(state_path)
        if state.get("schedule") != schedule_key:
            next_run_at = _next_image_cleanup_at(current, interval_days, scheduled_time)
            _save_image_cleanup_state(state_path, {
                "schedule": schedule_key,
                "next_run_at": next_run_at.isoformat(timespec="seconds"),
            })
            return None

        try:
            next_run_at = datetime.fromisoformat(str(state.get("next_run_at") or ""))
        except ValueError:
            next_run_at = _next_image_cleanup_at(current, interval_days, scheduled_time)
            _save_image_cleanup_state(state_path, {
                "schedule": schedule_key,
                "next_run_at": next_run_at.isoformat(timespec="seconds"),
            })
            return None

        if current < next_run_at:
            return None

        result = delete_images(end_date=current.date().isoformat(), all_matching=True)
        while next_run_at <= current:
            next_run_at += timedelta(days=interval_days)
        _save_image_cleanup_state(state_path, {
            "schedule": schedule_key,
            "last_run_at": current.isoformat(timespec="seconds"),
            "next_run_at": next_run_at.isoformat(timespec="seconds"),
        })
        logger.info({
            "event": "scheduled_image_cleanup_done",
            "removed": result["removed"],
            "next_run_at": next_run_at.isoformat(timespec="seconds"),
        })
        return result


def download_images_zip(paths: list[str]) -> io.BytesIO:
    root = config.images_dir.resolve()
    buf = io.BytesIO()
    added = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in paths:
            rel = _safe_relative_path(item)
            path = (root / rel).resolve()
            payload: bytes | None = None
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if path.is_file():
                payload = path.read_bytes()
            else:
                try:
                    payload = image_storage_service.get_bytes(rel)
                except Exception:
                    continue
            name = path.name
            if name in used_names:
                stem = path.stem
                suffix = path.suffix
                counter = 2
                while f"{stem}_{counter}{suffix}" in used_names:
                    counter += 1
                name = f"{stem}_{counter}{suffix}"
            used_names.add(name)
            zf.writestr(name, payload)
            added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="no images found")
    buf.seek(0)
    return buf


def storage_stats() -> dict:
    usage = shutil.disk_usage(config.images_dir)
    total_mb = usage.total // MEGABYTE
    used_mb = usage.used // MEGABYTE
    free_mb = usage.free // MEGABYTE

    try:
        items = _managed_image_items()
        image_count = len(items)
        image_size = sum(_image_item_size(item) for item in items)
    except Exception:
        # A broken/stale remote index should not make the storage panel unusable.
        files = _local_image_files()
        image_count = len(files)
        image_size = sum(path.stat().st_size for path in files)

    return {
        "disk_total_mb": total_mb,
        "disk_used_mb": used_mb,
        "disk_free_mb": free_mb,
        "image_count": image_count,
        "image_size_mb": image_size // (1024 * 1024),
        "image_size_bytes": image_size,
    }


def compress_images(quality: int = 60) -> dict:
    """重新压缩所有图片，返回节省的空间"""
    saved = 0
    count = 0
    for p in sorted(config.images_dir.rglob("*.png")):
        if not p.is_file():
            continue
        try:
            orig = p.stat().st_size
            with Image.open(p) as img:
                img = ImageOps.exif_transpose(img)
                img.save(str(p) + ".tmp", format="PNG", optimize=True)
            new_size = Path(str(p) + ".tmp").stat().st_size
            if new_size < orig:
                Path(str(p) + ".tmp").replace(p)
                saved += orig - new_size
                count += 1
            else:
                Path(str(p) + ".tmp").unlink()
        except Exception:
            pass
    return {"compressed": count, "saved_bytes": saved, "saved_mb": saved // (1024 * 1024)}


def delete_to_target(target_image_mb: int, dry_run: bool = False) -> dict[str, int | bool]:
    """Delete the oldest images until managed image usage is at most ``target_image_mb``."""
    target_image_mb = max(0, int(target_image_mb))
    target_bytes = target_image_mb * MEGABYTE
    candidates: list[tuple[float, str, int]] = []
    for item in _managed_image_items():
        rel = str(item.get("path") or item.get("rel") or "").strip()
        if not rel or Path(rel).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        candidates.append((_image_item_age(item, rel), rel, _image_item_size(item)))

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    current_bytes = sum(size for _, _, size in candidates)
    removed = 0
    freed = 0
    for _, rel, size in candidates:
        if current_bytes - freed <= target_bytes:
            break
        if not dry_run:
            try:
                if not image_storage_service.delete(rel):
                    continue
                _remove_image_artifacts(rel)
            except Exception:
                # Keep failed items and continue with newer files.
                continue
        freed += size
        removed += 1

    if not dry_run:
        _cleanup_empty_dirs(config.images_dir)
        _cleanup_empty_dirs(config.image_thumbnails_dir)

    remaining_bytes = max(0, current_bytes - freed)
    return {
        "removed": removed,
        "freed_bytes": freed,
        "freed_mb": freed // MEGABYTE,
        "target_image_mb": target_image_mb,
        "target_free_mb": target_image_mb,
        "current_size_bytes": remaining_bytes,
        "current_size_mb": remaining_bytes // MEGABYTE,
        "done": remaining_bytes <= target_bytes,
        "dry_run": dry_run,
    }


def delete_to_free_space_target(target_free_mb: int, dry_run: bool = False) -> dict[str, int | bool]:
    """Delete local images until the filesystem has at least ``target_free_mb`` free."""
    target_free_mb = max(0, int(target_free_mb))
    target_bytes = target_free_mb * MEGABYTE
    current_free_bytes = shutil.disk_usage(config.images_dir).free
    files = sorted(
        _local_image_files(),
        key=lambda path: (path.stat().st_mtime, path.relative_to(config.images_dir).as_posix()),
    )
    removed = 0
    freed = 0
    for path in files:
        if current_free_bytes + freed >= target_bytes:
            break
        rel = path.relative_to(config.images_dir).as_posix()
        size = path.stat().st_size
        if not dry_run:
            try:
                if not image_storage_service.delete(rel):
                    continue
                _remove_image_artifacts(rel)
            except Exception:
                continue
        freed += size
        removed += 1

    if not dry_run:
        _cleanup_empty_dirs(config.images_dir)
        _cleanup_empty_dirs(config.image_thumbnails_dir)

    remaining_free_bytes = current_free_bytes + freed
    return {
        "removed": removed,
        "freed_bytes": freed,
        "freed_mb": freed // MEGABYTE,
        "target_free_mb": target_free_mb,
        "current_free_mb": remaining_free_bytes // MEGABYTE,
        "done": remaining_free_bytes >= target_bytes,
        "dry_run": dry_run,
    }


def download_images_zip(paths: list[str]) -> io.BytesIO:
    root = config.images_dir.resolve()
    buf = io.BytesIO()
    added = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in paths:
            rel = _safe_relative_path(item)
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            name = path.name
            if name in used_names:
                stem = path.stem
                suffix = path.suffix
                counter = 2
                while f"{stem}_{counter}{suffix}" in used_names:
                    counter += 1
                name = f"{stem}_{counter}{suffix}"
            used_names.add(name)
            zf.write(path, name)
            added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="no images found")
    buf.seek(0)
    return buf


def _auto_cleanup_worker(stop_event: threading.Event) -> None:
    """Run scheduled full cleanup and retain the low-disk safety cleanup."""
    next_storage_check = time.monotonic() + STORAGE_CLEANUP_CHECK_SECONDS
    while not stop_event.is_set():
        try:
            run_scheduled_image_cleanup_if_due()
        except Exception as exc:
            logger.error({"event": "scheduled_image_cleanup_failed", "error": str(exc)})

        if time.monotonic() >= next_storage_check:
            next_storage_check = time.monotonic() + STORAGE_CLEANUP_CHECK_SECONDS
            try:
                config.cleanup_old_images()
                cleanup_image_thumbnails()
                min_free_mb = getattr(config, "image_min_free_mb", 500) or 500
                usage = shutil.disk_usage(config.images_dir)
                free_mb = usage.free // MEGABYTE
                if free_mb < min_free_mb:
                    logger.info({"event": "image_auto_cleanup", "free_mb": free_mb, "min_free_mb": min_free_mb})
                    result = delete_to_free_space_target(min_free_mb)
                    logger.info({"event": "image_auto_cleanup_done", **result})
            except Exception as exc:
                logger.error({"event": "image_storage_cleanup_failed", "error": str(exc)})

        stop_event.wait(IMAGE_CLEANUP_CHECK_SECONDS)


def start_image_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    t = threading.Thread(target=_auto_cleanup_worker, args=(stop_event,), daemon=True, name="image-cleanup")
    t.start()
    return t
