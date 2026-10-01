from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
import time

from services.storage.base import StorageBackend

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"

DEFAULT_IMAGE_POLL_TIMEOUT_SECS = 75
DEFAULT_IMAGE_CLEANUP_INTERVAL_DAYS = 1
DEFAULT_IMAGE_CLEANUP_TIME = "03:00"
IMAGE_CLEANUP_INTERVAL_CHOICES = {1, 3, 5, 7}
DEFAULT_LOG_RETENTION_DAYS = 30
# 每日自动清理的默认时刻（北京时间）。图片清理默认也是 03:00，两个任务各自独立。
DEFAULT_LOG_CLEANUP_TIME = "03:00"
# 上限十年：再长的保留期没有意义，也挡住了手滑填进来的天文数字。
MAX_LOG_RETENTION_DAYS = 3650

DEFAULT_CHAT_COMPLETION_CACHE = {
    "enabled": True,
    "ttl_seconds": 60,
    "max_entries": 256,
    "dedupe_inflight": True,
    "stream_cache": True,
    "normalize_messages": True,
    "drop_adjacent_duplicates": True,
    "drop_assistant_history": False,
}

def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


_MISSING = object()


def _normalize_optional_interval(value: object, default: int | None = None, minimum: int = 1) -> int | None:
    if value is _MISSING:
        return default
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if normalized <= 0:
        return None
    return max(minimum, normalized)


def _normalize_image_cleanup_interval_days(value: object) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        return DEFAULT_IMAGE_CLEANUP_INTERVAL_DAYS
    return normalized if normalized in IMAGE_CLEANUP_INTERVAL_CHOICES else DEFAULT_IMAGE_CLEANUP_INTERVAL_DAYS


def _normalize_log_retention_days(value: object) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        return DEFAULT_LOG_RETENTION_DAYS
    return min(max(normalized, 1), MAX_LOG_RETENTION_DAYS)


def _normalize_image_cleanup_time(value: object) -> str:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return DEFAULT_IMAGE_CLEANUP_TIME
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError:
        return DEFAULT_IMAGE_CLEANUP_TIME
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return DEFAULT_IMAGE_CLEANUP_TIME
    return f"{hour:02d}:{minute:02d}"


def _normalize_log_cleanup_time(value: object) -> str:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return DEFAULT_LOG_CLEANUP_TIME
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError:
        return DEFAULT_LOG_CLEANUP_TIME
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return DEFAULT_LOG_CLEANUP_TIME
    return f"{hour:02d}:{minute:02d}"


def _normalize_chat_completion_cache_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), DEFAULT_CHAT_COMPLETION_CACHE["enabled"]),
        "ttl_seconds": _normalize_positive_int(
            source.get("ttl_seconds"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["ttl_seconds"]),
            0,
        ),
        "max_entries": _normalize_positive_int(
            source.get("max_entries"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["max_entries"]),
            1,
        ),
        "dedupe_inflight": _normalize_bool(
            source.get("dedupe_inflight"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["dedupe_inflight"]),
        ),
        "stream_cache": _normalize_bool(
            source.get("stream_cache"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["stream_cache"]),
        ),
        "normalize_messages": _normalize_bool(
            source.get("normalize_messages"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["normalize_messages"]),
        ),
        "drop_adjacent_duplicates": _normalize_bool(
            source.get("drop_adjacent_duplicates"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_adjacent_duplicates"]),
        ),
        "drop_assistant_history": _normalize_bool(
            source.get("drop_assistant_history"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_assistant_history"]),
        ),
    }


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int | None
    refresh_all_accounts_interval_minute: int | None


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_dir():
        print(
            f"Warning: {name} at '{path}' is a directory, ignoring it and falling back to other configuration sources.",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = _read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    refresh_interval = _normalize_optional_interval(raw_config.get("refresh_account_interval_minute", _MISSING), 5)
    refresh_all_interval = _normalize_optional_interval(raw_config.get("refresh_all_accounts_interval_minute", _MISSING))

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
        refresh_all_accounts_interval_minute=refresh_all_interval,
    )


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._storage_backend: StorageBackend | None = None
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def refresh_account_interval_minute(self) -> int | None:
        return _normalize_optional_interval(self.data.get("refresh_account_interval_minute", _MISSING), 5)

    @property
    def refresh_all_accounts_interval_minute(self) -> int | None:
        return _normalize_optional_interval(self.data.get("refresh_all_accounts_interval_minute", _MISSING))

    @property
    def image_retention_days(self) -> int:
        try:
            return max(1, int(self.data.get("image_retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def log_retention_days(self) -> int:
        return _normalize_log_retention_days(self.data.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS))

    @property
    def log_auto_cleanup(self) -> bool:
        # 默认关闭：自动清理会真的删数据，必须由用户显式打开。
        return self.data.get("log_auto_cleanup") is True

    @property
    def log_cleanup_time(self) -> str:
        return _normalize_log_cleanup_time(self.data.get("log_cleanup_time"))

    @property
    def image_cleanup_interval_days(self) -> int:
        return _normalize_image_cleanup_interval_days(self.data.get("image_cleanup_interval_days"))

    @property
    def image_cleanup_time(self) -> str:
        return _normalize_image_cleanup_time(self.data.get("image_cleanup_time"))

    @property
    def image_cleanup_schedule_configured(self) -> bool:
        return "image_cleanup_interval_days" in self.data and "image_cleanup_time" in self.data

    @property
    def image_poll_timeout_secs(self) -> int:
        try:
            return max(1, int(self.data.get("image_poll_timeout_secs", DEFAULT_IMAGE_POLL_TIMEOUT_SECS)))
        except (TypeError, ValueError):
            return DEFAULT_IMAGE_POLL_TIMEOUT_SECS

    @property
    def image_poll_interval_secs(self) -> float:
        try:
            return max(0.5, float(self.data.get("image_poll_interval_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_poll_initial_wait_secs(self) -> float:
        """Image generation upstream takes ~30s; polling immediately wastes requests
        and trips a transient 429. Default 10s gives the conversation document time
        to commit before the first poll."""
        try:
            return max(0.0, float(self.data.get("image_poll_initial_wait_secs", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    @property
    def image_account_concurrency(self) -> int:
        try:
            return max(1, int(self.data.get("image_account_concurrency", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def image_parallel_generation(self) -> bool:
        value = self.data.get("image_parallel_generation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_rate_limited_accounts(self) -> bool:
        value = self.data.get("auto_remove_rate_limited_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def log_levels(self) -> list[str]:
        levels = self.data.get("log_levels")
        if not isinstance(levels, list):
            return []
        allowed = {"debug", "info", "warning", "error"}
        return [level for item in levels if (level := str(item or "").strip().lower()) in allowed]

    @property
    def sensitive_words(self) -> list[str]:
        words = self.data.get("sensitive_words")
        return [word for item in words if (word := str(item or "").strip())] if isinstance(words, list) else []

    @property
    def ai_review(self) -> dict[str, object]:
        value = self.data.get("ai_review")
        return value if isinstance(value, dict) else {}

    @property
    def global_system_prompt(self) -> str:
        return str(self.data.get("global_system_prompt") or "").strip()

    @property
    def default_upstream_model_name(self) -> str:
        return str(self.data.get("default_upstream_model_name") or "gpt-5-5").strip()

    @property
    def default_thinking_effort(self) -> str:
        value = str(self.data.get("default_thinking_effort") or "auto").strip().lower()
        return value if value in {"auto", "standard", "extended", "max"} else "auto"

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def image_thumbnails_dir(self) -> Path:
        path = DATA_DIR / "image_thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self) -> int:
        cutoff = time.time() - self.image_retention_days * 86400
        removed = 0
        for path in self.images_dir.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        for path in sorted((p for p in self.images_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        return removed

    @property
    def base_url(self) -> str:
        return str(
            os.getenv("CHATGPT2API_BASE_URL")
            or self.data.get("base_url")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get(self) -> dict[str, object]:
        data = dict(self.data)
        data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
        data["refresh_all_accounts_interval_minute"] = self.refresh_all_accounts_interval_minute
        data["image_retention_days"] = self.image_retention_days
        data["log_retention_days"] = self.log_retention_days
        data["log_auto_cleanup"] = self.log_auto_cleanup
        data["log_cleanup_time"] = self.log_cleanup_time
        data["image_cleanup_interval_days"] = self.image_cleanup_interval_days
        data["image_cleanup_time"] = self.image_cleanup_time
        data["image_poll_timeout_secs"] = self.image_poll_timeout_secs
        data["image_poll_interval_secs"] = self.image_poll_interval_secs
        data["image_poll_initial_wait_secs"] = self.image_poll_initial_wait_secs
        data["image_account_concurrency"] = self.image_account_concurrency
        data["image_parallel_generation"] = self.image_parallel_generation
        data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
        data["auto_remove_rate_limited_accounts"] = self.auto_remove_rate_limited_accounts
        data["log_levels"] = self.log_levels
        data["sensitive_words"] = self.sensitive_words
        data["ai_review"] = self.ai_review
        data["global_system_prompt"] = self.global_system_prompt
        data["default_upstream_model_name"] = self.default_upstream_model_name
        data["default_thinking_effort"] = self.default_thinking_effort
        data["chat_completion_cache"] = self.get_chat_completion_cache_settings()
        data.pop("auth-key", None)
        return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def update(self, data: dict[str, object]) -> dict[str, object]:
        next_data = dict(self.data)
        next_data.update(dict(data or {}))
        if "refresh_account_interval_minute" in next_data:
            next_data["refresh_account_interval_minute"] = _normalize_optional_interval(
                next_data.get("refresh_account_interval_minute"), 5
            )
        if "refresh_all_accounts_interval_minute" in next_data:
            next_data["refresh_all_accounts_interval_minute"] = _normalize_optional_interval(
                next_data.get("refresh_all_accounts_interval_minute")
            )
        if "image_cleanup_interval_days" in next_data:
            next_data["image_cleanup_interval_days"] = _normalize_image_cleanup_interval_days(
                next_data.get("image_cleanup_interval_days")
            )
        if "image_cleanup_time" in next_data:
            next_data["image_cleanup_time"] = _normalize_image_cleanup_time(next_data.get("image_cleanup_time"))
        if "log_retention_days" in next_data:
            next_data["log_retention_days"] = _normalize_log_retention_days(next_data.get("log_retention_days"))
        if "log_cleanup_time" in next_data:
            next_data["log_cleanup_time"] = _normalize_log_cleanup_time(next_data.get("log_cleanup_time"))
        if "chat_completion_cache" in next_data:
            next_data["chat_completion_cache"] = _normalize_chat_completion_cache_settings(
                next_data.get("chat_completion_cache")
            )
        self.data = next_data
        self._save()
        return self.get()

    def get_chat_completion_cache_settings(self) -> dict[str, object]:
        return _normalize_chat_completion_cache_settings(self.data.get("chat_completion_cache"))

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend


config = ConfigStore(CONFIG_FILE)
