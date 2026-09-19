"""聚合成品：把验证存活的多个源合并成终端客户端可直接导入的文件。

- v2ray: 多源的节点 URI 合并 → 去重（键 = 剥掉 #fragment 的 URI，保留首个原文）→ base64。
- clash: 多源的 proxies 合并 → 去重（键 = 剥掉 name 的 proxy dict）→ 注入 base_clash.yaml 模板。
- telegram: 裸节点 URI 去重（键同上：剥掉 fragment 备注）。

确定性：入参先按源 URL 排序，源内按原文出现顺序；输入相同则输出逐字节相同。
全部纯函数、不联网，可直接被离线自检覆盖。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Dict, List, Tuple

from bot.parse.base64_chain import decode_chain, is_likely_b64
from bot.parse.clash_yaml import parse_clash_proxies_raw
from bot.parse.node_list import extract_node_uris, parse_nodes

logger = logging.getLogger("merge")

ALL_NODES_TOKEN = "__ALL_NODES__"
_MAX_TEXT = 2_000_000
_USERINFO_RE = re.compile(r"://([^@]+)@")


def uri_identity(uri: str) -> str:
    """节点去重键：协议|主机|端口|参数 + 协议专属鉴权（vless/trojan 的 userinfo uuid）。

    不含 remark：vmess 的 ps 在 base64 里、vless/trojan 的在 #fragment 里，都不参与键。
    同 host 不同协议/端口/uuid/传输方式 -> 键不同，不会被误并；只改名字 -> 键相同，被去重。
    """
    nodes, _ = parse_nodes(uri)
    if not nodes:
        return uri.split("#", 1)[0]
    nd = nodes[0]
    key = nd.canonical_str()
    proto = uri.split(":", 1)[0].lower()
    if proto in ("vless", "trojan"):
        m = _USERINFO_RE.search(uri)
        if m:
            key += "|user=" + m.group(1)
    return key


def _fair_truncate(buckets: Dict[str, List], max_nodes: int) -> List:
    """按源分桶 + 轮转截断：保证节点多的源挤不掉其它源的覆盖。结果确定。"""
    if max_nodes <= 0:
        return []
    buckets = {u: list(v) for u, v in buckets.items()}
    urls = sorted(buckets)
    out: List = []
    quota = max_nodes
    while quota > 0:
        advanced = False
        for u in urls:
            if quota <= 0 or not buckets[u]:
                continue
            out.append(buckets[u].pop(0))
            quota -= 1
            advanced = True
        if not advanced:
            break
    return out


def _protocol_stats(nodes) -> Dict[str, int]:
    stats: Dict[str, int] = {}
    for n in nodes:
        if isinstance(n, str):
            proto = n.split(":", 1)[0].lower()
        elif isinstance(n, dict):
            proto = n.get("type", "")
        else:
            proto = getattr(n, "protocol", "")
        stats[proto] = stats.get(proto, 0) + 1
    return stats


def merge_v2ray(sources: List[Tuple[str, bytes]], max_nodes: int) -> Tuple[str, Dict]:
    """入参 list[(url, bytes)] -> (base64 文本, stats)。空输入返回 ("", stats)。"""
    buckets: Dict[str, List[str]] = {}
    seen: set = set()
    total = 0
    for url, data in sorted(sources):
        text = data.decode("utf-8", errors="replace")
        if is_likely_b64(text.strip()) and len(text) < _MAX_TEXT:
            text = decode_chain(text)
        kept = []
        for uri in extract_node_uris(text):
            total += 1
            key = uri_identity(uri)
            if key in seen:
                continue
            seen.add(key)
            kept.append(uri)
        if kept:
            buckets[url] = kept
    picked = _fair_truncate(buckets, max_nodes)
    merged_text = "\n".join(picked) + ("\n" if picked else "")
    stats = {
        "sources": len(buckets),
        "nodes_before_dedup": total,
        "merged": len(picked),
        "truncated": total > 0 and len(picked) < total,
        "per_protocol": _protocol_stats(picked),
    }
    if not picked:
        return "", stats
    return base64.b64encode(merged_text.encode("utf-8")).decode("ascii"), stats


def _proxy_key(p: dict) -> str:
    """去重键：只去掉展示性 name，保留 type/server/port/凭据/传输字段。"""
    body = {k: v for k, v in p.items() if k != "name"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


def _unique_names(proxies: List[dict]) -> List[dict]:
    used: set = set()
    out = []
    for p in proxies:
        name = str(p.get("name", "")).strip() or f"node-{len(out) + 1}"
        base, i = name, 0
        while name in used:
            i += 1
            name = f"{base}#{i}"
        used.add(name)
        body = dict(p)
        body["name"] = name
        out.append(body)
    return out


def merge_clash(sources: List[Tuple[str, bytes]], max_nodes: int,
                template_path: str) -> Tuple[str, Dict]:
    """入参 list[(url, bytes)] + 模板路径 -> (完整 YAML 文本, stats)。"""
    try:
        import yaml
    except ImportError:
        return "", {"error": "pyyaml missing"}
    try:
        with open(template_path, encoding="utf-8") as f:
            template = yaml.safe_load(f.read())
        if not isinstance(template, dict):
            return "", {"error": f"template not a mapping: {template_path}"}
    except OSError as e:
        return "", {"error": f"template unreadable: {e}"}

    buckets: Dict[str, List[dict]] = {}
    seen: set = set()
    total = 0
    for url, data in sorted(sources):
        text = data.decode("utf-8", errors="replace")
        kept = []
        for p in parse_clash_proxies_raw(text):
            total += 1
            key = _proxy_key(p)
            if key in seen:
                continue
            seen.add(key)
            kept.append(p)
        if kept:
            buckets[url] = kept
    picked = _fair_truncate(buckets, max_nodes)
    merged_proxies = _unique_names(picked)

    template["proxies"] = merged_proxies
    node_names = [p["name"] for p in merged_proxies]
    groups = template.get("proxy-groups")
    if isinstance(groups, list):
        for g in groups:
            if not isinstance(g, dict):
                continue
            plist = g.get("proxies")
            if isinstance(plist, list) and ALL_NODES_TOKEN in plist:
                g["proxies"] = [x for x in plist if x != ALL_NODES_TOKEN] + node_names
    try:
        body = yaml.safe_dump(template, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
    except Exception as e:
        return "", {"error": f"yaml dump failed: {e}"}
    stats = {
        "sources": len(buckets),
        "proxies_before_dedup": total,
        "merged": len(merged_proxies),
        "truncated": total > 0 and len(merged_proxies) < total,
        "per_protocol": _protocol_stats(merged_proxies),
    }
    if not merged_proxies:
        return "", stats
    return body, stats


def merge_telegram(raw_nodes: List[str]) -> str:
    """telegram 裸节点去重：原文顺序不变，键 = 连接身份（见 uri_identity）。"""
    seen: set = set()
    out = []
    for uri in raw_nodes:
        key = uri_identity(uri)
        if key in seen:
            continue
        seen.add(key)
        out.append(uri)
    return "\n".join(out) + ("\n" if out else "")


def sources_list_text(urls: List[str]) -> str:
    return "\n".join(dict.fromkeys(urls)) + ("\n" if urls else "")