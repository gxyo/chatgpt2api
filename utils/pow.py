import hashlib
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Sequence

import pybase64

DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
from utils.helper import new_uuid


CORES = [8, 16, 24, 32]
DOCUMENT_KEYS = ["__reactContainer$fzelfjyxej8", "_reactListening5dehydibo78", "location"]
SCREEN_RESOLUTIONS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160]]


class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.data_build = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src")
        if not src:
            return
        self.script_sources.append(src)
        match = re.search(r"c/[^/]*/_", src)
        if match:
            self.data_build = match.group(0)


def parse_pow_resources(html_content: str) -> tuple[list[str], str]:
    parser = ScriptSrcParser()
    parser.feed(html_content)
    script_sources = parser.script_sources or [DEFAULT_POW_SCRIPT]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html_content)
        if match:
            data_build = match.group(1)
    return script_sources, data_build


def _legacy_parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def build_pow_config(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
) -> list[Any]:
    navigator_key = random.choice([
        "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
        "storage−[object StorageManager]",
        "locks−[object LockManager]",
        "appCodeName−Mozilla",
        "permissions−[object Permissions]",
        "share−function share() { [native code] }",
        "webdriver−false",
        "managed−[object NavigatorManagedData]",
        "canShare−function canShare() { [native code] }",
        "vendor−Google Inc.",
        "mediaDevices−[object MediaDevices]",
        "vibrate−function vibrate() { [native code] }",
        "storageBuckets−[object StorageBucketManager]",
        "mediaCapabilities−[object MediaCapabilities]",
        "cookieEnabled−true",
        "virtualKeyboard−[object VirtualKeyboard]",
        "product−Gecko",
        "presentation−[object Presentation]",
        "onLine−true",
        "mimeTypes−[object MimeTypeArray]",
        "credentials−[object CredentialsContainer]",
        "serviceWorker−[object ServiceWorkerContainer]",
        "keyboard−[object Keyboard]",
        "gpu−[object GPU]",
        "doNotTrack",
        "serial−[object Serial]",
        "pdfViewerEnabled−true",
        "language−zh-CN",
        "geolocation−[object Geolocation]",
        "userAgentData−[object NavigatorUAData]",
        "getUserMedia−function getUserMedia() { [native code] }",
        "sendBeacon−function sendBeacon() { [native code] }",
        "hardwareConcurrency−32",
        "windowControlsOverlay−[object WindowControlsOverlay]",
    ])
    window_key = random.choice([
        "0",
        "window",
        "self",
        "document",
        "name",
        "location",
        "customElements",
        "history",
        "navigation",
        "innerWidth",
        "innerHeight",
        "scrollX",
        "scrollY",
        "visualViewport",
        "screenX",
        "screenY",
        "outerWidth",
        "outerHeight",
        "devicePixelRatio",
        "screen",
        "chrome",
        "navigator",
        "onresize",
        "performance",
        "crypto",
        "indexedDB",
        "sessionStorage",
        "localStorage",
        "scheduler",
        "alert",
        "atob",
        "btoa",
        "fetch",
        "matchMedia",
        "postMessage",
        "queueMicrotask",
        "requestAnimationFrame",
        "setInterval",
        "setTimeout",
        "caches",
        "__NEXT_DATA__",
        "__BUILD_MANIFEST",
        "__NEXT_PRELOADREADY",
    ])
    script_source = random.choice(list(script_sources)) if script_sources else DEFAULT_POW_SCRIPT
    return [
        sum(random.choices(SCREEN_RESOLUTIONS, k=1)[0]),
        _legacy_parse_time(),
        4294705152,
        1,
        user_agent,
        script_source,
        data_build,
        "en-US",
        "en-US,es-US,en,es",
        random.random(),
        navigator_key,
        random.choice(DOCUMENT_KEYS),
        window_key,
        time.perf_counter() * 1000,
        new_uuid(),
        "",
        random.choice(CORES),
        time.time() * 1000 - (time.perf_counter() * 1000),
        0, 0, 0, 0, 0, 0,
        0,  # 0 = edge/chrome, 1 = firefox
    ]


def sentinel_hash(text: str) -> str:
    """当前网页 sentinel SDK 用的 hash：FNV-1a 32 位 + avalanche。

    必须按 UTF-16 code unit 迭代：JS 的 ``charCodeAt`` 看到的是 code unit，
    非 BMP 字符在 Python 里是一个 code point、在 JS 里是两个 unit，直接
    ``ord()`` 会对不上。

    旧的 SHA3-512 方案已经不被上游接受，PoW 结果也不再是无后缀的裸 base64。
    """
    h = 2166136261
    for unit in _utf16_units(text):
        h = ((h ^ unit) * 0x01000193) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return format(h & 0xFFFFFFFF, "08x")


def _utf16_units(text: str) -> list[int]:
    """把字符串拆成 JS 语义的 UTF-16 code unit 序列（代理对算两个单元）。"""
    raw = text.encode("utf-16-le")
    return [raw[index] | (raw[index + 1] << 8) for index in range(0, len(raw), 2)]


def build_proof_token_v2(
    seed: str,
    difficulty: str,
    base_config: list[Any],
    limit: int = 500_000,
) -> str:
    """用 25 字段的 p 解出当前版本的 proof token（FNV-1a，结果带 ``~S`` 后缀）。

    ``base_config`` 是 prepare 时提交的那个 ``p`` 解出来的数组；PoW 只改两个槽位：
    下标 3 放 nonce、下标 9 放已经跑掉的毫秒数，其余字段原样保留——上游是把整段
    JSON 重新 hash 的，动别的字节能过校验但和网页对不上。

    传进来的一定是副本，调用方的列表不会被改。
    """
    difficulty = str(difficulty or "")
    if not difficulty:
        raise ValueError("proof token difficulty is required")
    config = list(base_config)
    started = time.time()
    for nonce in range(limit):
        config[3] = nonce
        config[9] = round((time.time() - started) * 1000)
        encoded = pybase64.b64encode(
            json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode()
        if sentinel_hash(seed + encoded)[: len(difficulty)] <= difficulty:
            return "gAAAAAB" + encoded + "~S"
    raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")


def decode_requirements_p(p_token: str) -> list[Any]:
    """把 prepare 用的 ``p`` 还原成字段数组，PoW 要在原字段上改。

    解不出来就抛 ValueError——宁可直接报错，也不要拿一份猜出来的字段去解一个
    必然不通过的 PoW。
    """
    payload = p_token.removeprefix("gAAAAAC").removeprefix("gAAAAAB")
    try:
        decoded = pybase64.b64decode(payload)
        config = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise ValueError("requirements token is not a decodable 25-field payload") from exc
    if not isinstance(config, list):
        raise ValueError("requirements token payload is not a field array")
    return config


def _pow_generate(seed: str, difficulty: str, config: list[Any], limit: int = 500000) -> tuple[str, bool]:
    target = bytes.fromhex(difficulty)
    diff_len = len(difficulty) // 2
    seed_bytes = seed.encode()
    static_1 = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode()
    static_2 = ("," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ",").encode()
    static_3 = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode()
    for i in range(limit):
        final_json = static_1 + str(i).encode() + static_2 + str(i >> 1).encode() + static_3
        encoded = pybase64.b64encode(final_json)
        digest = hashlib.sha3_512(seed_bytes + encoded).digest()
        if digest[:diff_len] <= target:
            return encoded.decode(), True
    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + pybase64.b64encode(f'"{seed}"'.encode()).decode()
    return fallback, False


def build_requirements_token(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
) -> str:
    """构造 prepare 提交的 ``p``（25 字段，``gAAAAAC`` 前缀）。

    新旧链路共用同一份字段表，差别只在 PoW 用哪种 hash、finalize 提交哪些字段名；
    所以这个函数不叫 legacy —— 它现在就是网页提交的那份 ``p``。
    """
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build)
    return "gAAAAAC" + pybase64.b64encode(
        json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()


# 旧名字继续可用：调用方和测试桩都还按这个名字 import。
build_legacy_requirements_token = build_requirements_token


def build_proof_token(
    seed: str,
    difficulty: str,
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
) -> str:
    """旧版 PoW（SHA3-512、无后缀）。新链路请用 `build_proof_token_v2`。"""
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build)
    answer, solved = _pow_generate(seed, difficulty, config)
    if not solved:
        raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
    return "gAAAAAB" + answer


def build_proof_token_from_p(p_token: str, seed: str, difficulty: str) -> str:
    """新版入口：直接在 prepare 用的那份 ``p`` 上解 PoW。"""
    return build_proof_token_v2(seed, difficulty, decode_requirements_p(p_token))
