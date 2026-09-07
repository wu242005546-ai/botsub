"""base64 链式解析：订阅内容可能是多层 base64 包裹，递归解到能看懂为止。"""

from __future__ import annotations

import base64
import re
from typing import Optional

_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _normalize_b64(s: str) -> str:
    s = re.sub(r"\s+", "", s)
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return s


def decode_b64(s: str) -> Optional[str]:
    """尝试单层 base64 解码；失败返回 None，绝不抛出。"""
    try:
        raw = _normalize_b64(s)
        return base64.b64decode(raw, validate=False).decode("utf-8", errors="replace")
    except Exception:
        return None


def is_likely_b64(s: str) -> bool:
    s = re.sub(r"\s+", "", s)
    if not s or len(s) < 8:
        return False
    # 合法 base64：总长是 4 的倍数，padding 最多 2 个且只能出现在末尾
    if len(s) % 4 != 0:
        return False
    return bool(_B64_RE.match(s))


def decode_chain(text: str, max_depth: int = 4) -> str:
    """递归解 base64 链；解到非 base64 或深度用尽为止，返回解码后文本。
    若链路上出现明文订阅片段（vmess:// / ss:// / trojan:// 等）立即停止。"""
    cur = text
    for _ in range(max_depth):
        if not is_likely_b64(cur):
            return cur
        # 已含明文协议前缀则不再继续解
        if re.search(r"(vmess|vless|ss|trojan|ssr|hysteria2)://", cur):
            return cur
        dec = decode_b64(cur)
        if dec is None:
            return cur
        cur = dec
    return cur