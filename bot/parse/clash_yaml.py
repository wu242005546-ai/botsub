"""Clash YAML 解析：抽取 proxies（节点）与 proxy-providers/订阅 url。纯函数。"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from bot.domain import Node

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


_URL_FIND_RE = re.compile(r"(?:url|urls?):\s*['\"]?(https?://[^'\",\s}\]]+)['\"]?", re.I)
_NODE_FIELDS = ("name", "type", "server", "port", "uuid", "password", "cipher", "ws-opts", "network")


def looks_like_clash(text: str) -> bool:
    if len(text) > 4_000_000:
        return False
    head = text[:4000]
    return bool(re.search(r"(?:^|\n)\s*(proxies|proxy-providers):", head))


def _node_from_dict(d: dict) -> Optional[Node]:
    try:
        proto = str(d.get("type", "")).lower()
        server = str(d.get("server", "")).strip()
        port = int(d.get("port", 0))
    except (TypeError, ValueError):
        return None
    if proto not in ("ss", "vmess", "vless", "trojan", "ssr", "hysteria2"):
        return None
    if not server or port <= 0:
        return None
    params = {}
    for key, val in d.items():
        if key in _NODE_FIELDS:
            continue
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            try:
                import json

                params[key] = json.dumps(val, sort_keys=True)
            except Exception:
                params[key] = str(val)
        else:
            params[key] = str(val)
    return Node(
        protocol=proto,
        host=server,
        port=port,
        params=params,
        remark=str(d.get("name", ""))[:120],
    )


def parse_clash_yaml(text: str) -> Tuple[List[Node], List[str]]:
    """返回 (nodes, raw_urls)。raw_urls 来自 proxy-providers/订阅字段。"""
    if yaml is None:
        return [], []
    nodes: List[Node] = []
    raw_urls: List[str] = []
    doc = _safe_load(text)
    if not isinstance(doc, dict):
        return [], []

    provs = doc.get("proxy-providers") or {}
    if isinstance(provs, dict):
        for p in provs.values():
            if isinstance(p, dict) and p.get("url"):
                raw_urls.append(str(p["url"]).strip())
            elif isinstance(p, list):
                for item in p:
                    if isinstance(item, dict) and item.get("url"):
                        raw_urls.append(str(item["url"]).strip())

    proxies = doc.get("proxies")
    if isinstance(proxies, list):
        for item in proxies:
            if isinstance(item, dict):
                n = _node_from_dict(item)
                if n:
                    nodes.append(n)
    return nodes, raw_urls


def parse_clash_proxies_raw(text: str) -> List[dict]:
    """返回 YAML 中 `proxies:` 段的原始 proxy dict 列表（用于聚合成品，保留完整字段）。

    只处理内联 proxies；proxy-providers 指向的外部订阅不属于本文件内容，不递归。
    """
    if yaml is None:
        return []
    doc = _safe_load(text)
    if not isinstance(doc, dict):
        return []
    proxies = doc.get("proxies")
    if not isinstance(proxies, list):
        return []
    return [p for p in proxies if isinstance(p, dict)]


def _safe_load(text: str):
    try:
        return yaml.safe_load(text)
    except Exception:
        return None


def extract_raw_urls_from_yaml(text: str) -> List[str]:
    """兜底：在任意 yaml/文本里抓 https 订阅链接。"""
    urls = []
    for m in _URL_FIND_RE.finditer(text):
        u = m.group(1).strip()
        if u not in urls:
            urls.append(u)
    return urls