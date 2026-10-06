"""PKCE (Proof Key for Code Exchange) 工具函数"""
from __future__ import annotations

import base64
import hashlib
import secrets


def code_challenge_for(code_verifier: str) -> str:
    """由 verifier 反算 S256 challenge（与 generate_pkce 用的是同一套算法）。

    有了它就能反查：谁手里那串明文算出来等于 authorize 请求带的 challenge，
    谁就能兑换这个 code——不用去猜上游把 verifier 存在哪个键名下。
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """生成 PKCE code_verifier 与对应的 code_challenge（S256）。

    Returns:
        (code_verifier, code_challenge) 元组
    """
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = code_challenge_for(code_verifier)
    return code_verifier, code_challenge
