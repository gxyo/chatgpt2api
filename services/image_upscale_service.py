from __future__ import annotations

import io
import re
import threading
import time

from PIL import Image, ImageFilter

from services.config import config
from utils.log import logger

# 接受 2048x2048 / 2048×2048 两种分隔符
_SIZE_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(\d+)\s*$", re.IGNORECASE)

# 4K 图解码后单张 RGB 约 50MB，resize 峰值还要再翻倍；并发超分会把 4G 内存的机器打爆。
# 这里串行化，宁可排队也不要 OOM。
_UPSCALE_LOCK = threading.Lock()

# LANCZOS 放大后画面偏软，用 USM 找补。radius 大致对应放大倍数下的模糊半径。
_UNSHARP_RADIUS = 2.0
_UNSHARP_PERCENT = 110
_UNSHARP_THRESHOLD = 3


def parse_size_long_edge(size: object) -> int | None:
    """把 OpenAI 风格的 size（"2048x2048"）解析成长边像素数。

    这就是超分目标：传多少就是多少。免费号直出 1K，所以 "1024x1024" 等于不放大。
    不传、传 "auto" 或格式非法时返回 None，同样不放大。
    """
    match = _SIZE_RE.match(str(size or ""))
    if not match:
        return None
    return max(int(match.group(1)), int(match.group(2)))


def upscale_png(image_data: bytes, target: int | None) -> bytes:
    """把图片长边放大到 target，返回 PNG 字节。target 为 None 表示不放大。

    未开启、已经够大、或任何一步失败时都原样返回 image_data：超分是锦上添花，
    不能因为它让生图请求失败。
    """
    if target is None or target <= 0:
        return image_data
    if not config.image_upscale_enabled:
        return image_data

    try:
        with Image.open(io.BytesIO(image_data)) as source:
            width, height = source.size
            longest = max(width, height)
            if longest <= 0 or longest >= target:
                return image_data
            scale = target / longest
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            # 排队等锁的时间单独记：全部超分是串行的，等锁久说明同时在超分的请求多，
            # 这段等待也是用户实打实等掉的时间（按任务端点调用时尤其明显）。
            queued_at = time.monotonic()
            with _UPSCALE_LOCK:
                lock_wait_ms = int((time.monotonic() - queued_at) * 1000)
                started = time.monotonic()
                # P 模式要先转 RGB；RGBA 保留透明度，别把带 alpha 的图压成实底。
                image = source.copy() if source.mode in {"RGB", "RGBA"} else source.convert("RGB")
                image = image.resize(size, Image.Resampling.LANCZOS)
                image = image.filter(
                    ImageFilter.UnsharpMask(
                        radius=_UNSHARP_RADIUS,
                        percent=_UNSHARP_PERCENT,
                        threshold=_UNSHARP_THRESHOLD,
                    )
                )
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                result = buffer.getvalue()
                logger.info({
                    "event": "image_upscale_done",
                    "from_size": f"{width}x{height}",
                    "to_size": f"{size[0]}x{size[1]}",
                    "target": target,
                    "queue_wait_ms": lock_wait_ms,
                    "upscale_ms": int((time.monotonic() - started) * 1000),
                    "source_bytes": len(image_data),
                    "result_bytes": len(result),
                })
                return result
    except Exception:
        return image_data
