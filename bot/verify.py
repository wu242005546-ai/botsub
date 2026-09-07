"""验证：独立验证缓存（SQLite）+ 域名限流 + TTL。

与内容缓存分离：内容缓存决定"要不要重新下载"，验证缓存决定"要不要重新验证"。
内容没变 ≠ 节点没死。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List

from config import Config
from bot.fetcher import Fetcher
from bot.domain import SourceIdentity, VerificationResult


class Verifier:
    def __init__(self, cfg: Config, fetcher: Fetcher, store):
        self.cfg = cfg
        self.fetcher = fetcher
        self.store = store

    async def verify_one(self, ident: SourceIdentity, force: bool = False) -> VerificationResult:
        """验证一个源。

        - 命中验证缓存（TTL 内且内容 hash 未变）且非 force => 复用缓存结果。
        - 否则真实获取内容并解析。
        """
        checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        cached = self.store.get_verification(ident.canonical) if not force else None
        if cached:
            age = time.time() - cached.get("checked_ts", 0)
            if age < self.cfg.VERIFY_CACHE_TTL_HOURS * 3600:
                return VerificationResult(
                    target=ident,
                    ok=bool(cached.get("ok")),
                    node_count=int(cached.get("node_count", 0)),
                    content_hash=cached.get("content_hash", ""),
                    checked_at=checked_at,
                    reused_cache=True,
                    kind=cached.get("kind", "unknown"),
                )

        res = await self.fetcher.fetch(ident.canonical, use_cache=False)
        if res.status != 200 or not res.data:
            self.store.record_verification(
                ident.canonical, ok=False, node_count=0, content_hash=res.content_hash,
                error=res.error or f"HTTP {res.status}", checked_at=checked_at,
            )
            return VerificationResult(target=ident, ok=False, error=res.error or f"HTTP {res.status}",
                                      status_code=res.status, checked_at=checked_at)

        from bot.parse.node_list import parse_nodes
        from bot.parse.clash_yaml import parse_clash_yaml, looks_like_clash
        from bot.parse.base64_chain import decode_chain, is_likely_b64

        text = res.data.decode("utf-8", errors="replace")
        if is_likely_b64(text) and len(text) < 2_000_000:
            text = decode_chain(text)

        nodes: List = []
        is_clash = looks_like_clash(text)
        if is_clash:
            nodes, _ = parse_clash_yaml(text)
        if not nodes:
            nodes, _ = parse_nodes(text)

        unique_hosts = {nd.host for nd in nodes}
        protos = sorted({nd.protocol for nd in nodes})
        ok = len(unique_hosts) >= self.cfg.MIN_VALID_NODES
        kind = "clash" if is_clash and ok else ("v2ray" if ok else "unknown")

        self.store.record_verification(
            ident.canonical, ok=ok, node_count=len(nodes),
            unique_hosts=len(unique_hosts), protocols=",".join(protos),
            content_hash=res.content_hash, error=None, checked_at=checked_at, kind=kind,
        )
        return VerificationResult(
            target=ident, ok=ok, node_count=len(nodes), unique_protocols=protos,
            content_hash=res.content_hash, checked_at=checked_at, status_code=res.status, kind=kind,
        )

    async def verify_many(self, idents: List[SourceIdentity], force: bool = False) -> List[VerificationResult]:
        """并发验证，域名限流由 Fetcher 统一控制。"""
        sem = asyncio.Semaphore(self.cfg.MAX_CONCURRENT_VERIFY)

        async def one(ident):
            async with sem:
                try:
                    return await self.verify_one(ident, force)
                except Exception as e:
                    return VerificationResult(target=ident, ok=False, error=f"verify error: {e}")

        return await asyncio.gather(*(one(i) for i in idents))