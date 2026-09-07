"""分析与打分：纯确定性逻辑，输出 {score, reasons[]}。不做智能化，只做可解释的统计。"""

from __future__ import annotations

import re
from typing import List

from bot.domain import AnalysisResult, ExtractionResult, SourceIdentity

_KNOWN_HOST_HINTS = {
    "cdn", "raw.githubusercontent.com", "fastly", "cloudfront", "gcore",
    "cf-workers", "rancher", "railway", "vercel", "supabase",
}
_SUSPICIOUS_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "example.com"}


def classify_text(text: str) -> str:
    """根据内容识别资源类型：clash / v2ray / base64 / unknown。"""
    from bot.parse.clash_yaml import looks_like_clash
    from bot.parse.node_list import _PROTO_RE

    if looks_like_clash(text):
        return "clash"
    if _PROTO_RE.search(text):
        return "v2ray"
    if re.search(r"vmess|vless|trojan|ssr://|hysteria2", text):
        return "v2ray"
    text_clean = re.sub(r"\s+", "", text)
    if text_clean and len(text_clean) > 20 and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", text_clean):
        return "base64"
    return "unknown"


def _host_score(url: str) -> int:
    parts = url.split("/", 3)
    host = parts[2].lower() if len(parts) > 2 else ""
    if any(h in host for h in _KNOWN_HOST_HINTS) or host.endswith(".git"):
        return 25
    if re.match(r"^([0-9]{1,3}\.){3}[0-9]{1,3}", host):
        return 5
    if any(s in host for s in _SUSPICIOUS_HOSTS):
        return -100
    return 8


def _url_quality_score(url: str) -> int:
    score = 0
    lower = url.lower()
    if "raw.githubusercontent.com" in lower or "/raw/" in lower:
        score += 10
    if any(k in lower for k in ("clash", "subscribe", "subscription", "sub", "proxy", "node")):
        score += 15
    if any(k in lower for k in ("/api/v1/client/subscribe", "/api/v1/client/api", "gettoken")):
        score += 30
    if re.search(r"\.(yaml|yml)$", lower):
        score += 10
    if re.search(r"\.(txt|list|sub|conf|json)$", lower):
        score += 5
    return score


def score_extraction(ex: ExtractionResult, provider_weight: int = 0) -> AnalysisResult:
    """对一次解析结果打分。确定性公式：内容质量 + url 特征 + provenance。"""
    target = ex.target
    reasons: List[str] = []
    score = 0

    nodes = ex.nodes
    n = len(nodes)
    if n > 0:
        score += min(40, 5 + n * 2)
        hosts = {nd.host for nd in nodes}
        if len(hosts) >= 3:
            score += 10
            reasons.append(f"{len(hosts)} hosts")
        protos = {nd.protocol for nd in nodes}
        if len(protos) > 1:
            score += 5
            reasons.append(f"{len(protos)} protocols")
    else:
        score -= 5

    score += _host_score(target.canonical)
    score += _url_quality_score(target.canonical)
    score += provider_weight

    if ex.raw_urls:
        score += 5
        reasons.append(f"{len(ex.raw_urls)} raw links")

    score = max(0, min(100, score))
    if reasons:
        reasons.insert(0, f"{n} nodes" if n else "no nodes")
    return AnalysisResult(
        target=target,
        kind=_kind_for(ex),
        score=score,
        reasons=reasons,
        node_count=n,
        unique_protocols=sorted({nd.protocol for nd in nodes}),
        fingerprint=_fingerprint(nodes),
    )


def _kind_for(ex: ExtractionResult) -> str:
    if ex.parser == "clash":
        return "clash"
    if ex.parser == "node_list" and ex.nodes:
        return "v2ray"
    return "unknown"


def _fingerprint(nodes) -> str:
    """节点集合指纹：guarantee 稳定、password 不可逆。"""
    canon_strs = sorted({nd.canonical_str() for nd in nodes})
    import hashlib

    return hashlib.sha256("|".join(canon_strs).encode("utf-8")).hexdigest()[:16]


def dedupe_nodes(nodes) -> List:
    seen: set = set()
    out = []
    for nd in nodes:
        fp = nd.fingerprint()
        if fp not in seen:
            seen.add(fp)
            out.append(nd)
    return out


def score_for_link(url: str, provider_weight: int = 0) -> AnalysisResult:
    """对裸链接（尚未拉取内容）的预打分。"""
    canonical = SourceIdentity.normalize(url)
    target = SourceIdentity(url=canonical, canonical=canonical, kind_hint="unknown")
    return AnalysisResult(
        target=target,
        kind="unknown",
        score=max(0, min(100, _host_score(canonical) + _url_quality_score(canonical) + provider_weight)),
        reasons=["pre-verify link"],
    )


def canonicalize_node_id(nd) -> str:
    """节点身份规范串（用于 fingerprint，可对外）。"""
    return nd.canonical_str()