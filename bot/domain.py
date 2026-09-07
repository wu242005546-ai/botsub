"""领域模型：注册表（records）+ 传递类型。反对胖 Source，这里只有不可变记录与转换。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


def sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Node:
    """规范化后的节点摘要。identity 只进 fingerprint，绝不落库明文。"""

    protocol: str  # ss / vmess / vless / trojan / ssr / hysteria2
    host: str
    port: int
    params: Dict[str, str] = field(default_factory=dict)
    remark: str = ""

    def canonical_str(self) -> str:
        parts = [self.protocol, self.host, str(self.port)]
        for k in sorted(self.params):
            parts.append(f"{k}={self.params[k]}")
        return "|".join(parts)

    def fingerprint(self) -> str:
        return sha256_hex(self.canonical_str().encode("utf-8"))[:16]


@dataclass(frozen=True)
class SourceIdentity:
    """节点的身份（URL canonical 化后的稳定 key）。"""

    url: str
    canonical: str
    kind_hint: str = "unknown"  # clash / v2ray / protocol / unknown

    @staticmethod
    def normalize(raw_url: str) -> str:
        """canonical 化：去 fragment、显式端口、去空白、host 小写。query 保留（含凭据潜在量参与 hash）。"""
        from urllib.parse import urlsplit, urlunsplit

        raw_url = raw_url.strip().split("#", 1)[0].strip()
        try:
            parts = urlsplit(raw_url)
            if not parts.scheme or not parts.netloc:
                return raw_url
            host = parts.hostname or ""
            port = parts.port
            netloc = host
            if port and not (
                (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
            ):
                netloc = f"{host}:{port}"
            path = parts.path.rstrip("/")
            query = parts.query
            return urlunsplit((parts.scheme, netloc, path, query, ""))
        except Exception:
            return raw_url


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    branch: str
    default_branch: str
    pushed_at: str = ""
    provenance: str = "search"  # search / debug / history


@dataclass
class SourceContent:
    """内容获取结果（一条 canonical source 一次 acquisition）。"""

    identity: SourceIdentity
    data: bytes
    content_hash: str
    fetched_at: str
    from_cache: bool = False
    error: Optional[str] = None
    http_status: int = 0


@dataclass
class ExtractionResult:
    """解析结果：从内容里提取的原始信息（纯函数产物）。"""

    target: SourceIdentity
    nodes: List[Node] = field(default_factory=list)
    raw_urls: List[str] = field(default_factory=list)  # 文本中发现的订阅链接
    parser: str = ""
    errors: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """打分/分类结果（deterministic + 可配置权重）。"""

    target: SourceIdentity
    kind: str = "unknown"
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    node_count: int = 0
    unique_protocols: List[str] = field(default_factory=list)
    fingerprint: str = ""


@dataclass
class VerificationResult:
    """验证结果：一次真实 HTTP 验证的产出。"""

    target: SourceIdentity
    ok: bool
    node_count: int = 0
    unique_protocols: List[str] = field(default_factory=list)
    content_hash: str = ""
    checked_at: str = ""
    error: Optional[str] = None
    status_code: int = 0
    reused_cache: bool = False
    kind: str = "unknown"