"""北京时间（UTC+8）工具。

项目里“给人看的时间”统一按北京时间存储与展示：

- **不带时区**的字符串（如 ``2026-10-01 20:30:00``）一律按北京时间理解；
- **带时区**的字符串（``Z`` / ``+08:00``）保留原始时刻，展示时再换算成北京时间。

容器里默认 ``TZ=Asia/Shanghai``，裸 ``datetime.now()`` 恰好等于北京时间，但代码
不应该依赖宿主机时区（本地开发、CI、其他镜像都不保证），因此所有墙钟时间都显式
走这里的换算。

注意：``utils/pow.py`` 的 GMT-0500、``utils/sentinel.py`` 的 GMT 文本是协议要求
的固定时区，**不属于**本模块管辖范围，不要改成北京时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))

#: 项目里墙钟时间统一使用的文本格式。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def beijing_now() -> datetime:
    """当前北京时间（UTC+8）的 naive datetime，不受宿主机时区影响。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def beijing_now_text() -> str:
    """当前北京时间的 ``YYYY-MM-DD HH:MM:SS`` 文本。"""
    return beijing_now().strftime(TIME_FORMAT)


def beijing_from_timestamp(value: float) -> datetime:
    """epoch 秒 → 北京时间 naive datetime（用于文件 mtime 等）。"""
    return datetime.fromtimestamp(value, BEIJING_TZ).replace(tzinfo=None)


def beijing_text_from_timestamp(value: float) -> str:
    """epoch 秒 → 北京时间的 ``YYYY-MM-DD HH:MM:SS`` 文本。"""
    return beijing_from_timestamp(value).strftime(TIME_FORMAT)


def utc_now_iso() -> str:
    """当前 UTC 时刻的带时区 ISO 文本，用于需要机器比较的时间戳。

    与墙钟时间不同，这类字段（退避计算、过期判断）必须保留明确时区，
    展示层再换算成北京时间。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_beijing(dt: datetime) -> datetime:
    """把任意 datetime 换算成北京时间 naive datetime。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BEIJING_TZ).replace(tzinfo=None)
