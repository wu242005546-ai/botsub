"""v2ray 风格节点列表解析：vmess://vless://trojan://ss://ssr:// 与普通文本订阅。纯函数。"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import List, Optional, Tuple

from bot.domain import Node

_PROTO_RE = re.compile(
    r"(vmess|vless|trojan|ss|ssr|hysteria2)://"
    r"([^\s#\n]+)",
    re.I,
)


def _b64url_decode(s: str) -> str:
    s = s.strip()
    if not s:
        return ""
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    s = s.replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _join_base(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def _to_node(proto: str, host: str, port: int, params: dict, remark: str) -> Optional[Node]:
    if not host or not (1 <= port <= 65535):
        return None
    return Node(proto, host, port, params, remark)


def parse_ss_url(u: str) -> Optional[Node]:
    # ss://base64(host:port) 或 ss://method:pass@host:port
    m = re.match(r"ss://([^@]+)@([^:]+):(\d+)", u)
    if m:
        userinfo = m.group(1)
        # userinfo 有时是不带 ":" 的裸 base64（ss://<base64>@host:port 变体），不能假定一定能 split 出两段
        if ":" not in userinfo:
            userinfo = _b64url_decode(userinfo) or userinfo
        if ":" not in userinfo:
            return None
        cipher, password = userinfo.split(":", 1)
        params = {"cipher": cipher, "password": password}
        try:
            return _to_node("ss", m.group(2), int(m.group(3)), params, "")
        except (ValueError, TypeError):
            return None
    m = re.match(r"ss://([^#?]+)", u)
    if not m:
        return None
    dec = _b64url_decode(m.group(1))
    if not dec:
        return None
    if "@" in dec:
        left, right = dec.rsplit("@", 1)
        if ":" in right:
            host, port_s = right.rsplit(":", 1)
            if ":" in left:
                method, pw = left.split(":", 1)
            else:
                method, pw = left, ""
            try:
                return _to_node("ss", host, int(port_s), {"cipher": method, "password": pw}, "")
            except (ValueError, TypeError):
                return None
    return None


def parse_vmess_url(u: str) -> Optional[Node]:
    m = re.match(r"vmess://([^#\n]+)", u)
    if not m:
        return None
    dec = _b64url_decode(m.group(1))
    if not dec:
        return None
    try:
        data = json.loads(dec)
        host = str(data.get("add", "")).strip()
        port = int(data.get("port", 0))
        if not host or not (1 <= port <= 65535):
            return None
        params = {k: str(v) for k, v in data.items() if k not in ("add", "port", "ps")}
        return Node("vmess", host, port, params, str(data.get("ps", ""))[:120])
    except (ValueError, TypeError):
        return None


def parse_vless_trojan_url(u: str) -> Optional[Node]:
    m = re.match(r"(vless|trojan)://([^@]+)@([^:?#]+):(\d+)([?#].*)?$", u)
    if not m:
        return None
    proto, id_, host, port_s, suffix = m.groups()
    params = {}
    if suffix and suffix.startswith("?"):
        qs = urllib.parse.parse_qs(suffix[1:])
        for k, v in qs.items():
            params[k] = v[0]
    try:
        return _to_node(proto, host, int(port_s), params, params.get("remarks", ""))
    except (ValueError, TypeError):
        return None


def parse_ssr_url(u: str) -> Optional[Node]:
    m = re.match(r"ssr://([^#\n]+)", u)
    if not m:
        return None
    dec = _b64url_decode(m.group(1))
    if not dec:
        return None
    # host:port:proto:method:obfs:pass_b64/?params
    m2 = re.match(r"([^:]+):(\d+):([^:]+):([^:]+):([^:]+):([^/]+)(/.*)?$", dec)
    if not m2:
        return None
    host, port_s, proto, method, obfs, pw_b64, _suffix = m2.groups()
    pw = _b64url_decode(pw_b64)
    params = {"cipher": method, "protocol": proto, "obfs": obfs, "password": pw}
    try:
        return _to_node("ssr", host, int(port_s), params, "")
    except (ValueError, TypeError):
        return None


def extract_node_uris(text: str) -> List[str]:
    """从文本里抓出所有节点 URI 原文（含 #fragment 备注）。用于聚合输出。"""
    return [m.group(0) for m in _PROTO_RE.finditer(text)]


def parse_nodes(text: str) -> Tuple[List[Node], List[str]]:
    """返回 (nodes, raw_urls)。raw_urls 为与节点无关的裸露 https 订阅链接。"""
    nodes: List[Node] = []
    found_urls: List[str] = []
    for m in _PROTO_RE.finditer(text):
        url = m.group(0)
        parser = {
            "vmess": parse_vmess_url,
            "vless": parse_vless_trojan_url,
            "trojan": parse_vless_trojan_url,
            "ss": parse_ss_url,
            "ssr": parse_ssr_url,
        }.get(m.group(1).lower())
        if parser:
            n = parser(url)
            if n:
                nodes.append(n)
    # 独立订阅链接：https://... 且后面不带节点特征
    for m in re.finditer(r"https?://[^\s\"]+", text):
        u = m.group(0).rstrip(",.;))]}")
        if not re.search(r"(vmess|vless|trojan|ss|ssr)://", u):
            if u not in found_urls:
                found_urls.append(u)
    return nodes, found_urls