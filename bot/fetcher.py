"""统一 Fetcher：一次 pipeline run 内同一 canonical source 只 acquisition 一次（含 retry）。

内容缓存（filesystem，控制"要不要重新下载"）与验证缓存（SQLite，控制"要不要重新验证"）分离。
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import aiohttp

from config import Config
from bot.domain import SourceIdentity

logger = logging.getLogger("fetcher")


@dataclass
class FetchResult:
    status: int = 0
    error: Optional[str] = None
    data: bytes = b""
    from_cache: bool = False
    content_hash: str = ""
    fetched_at: str = ""
    attempts: int = 1


class ContentCache:
    """Filesystem 内容缓存：key = sha256(repo_url)，value = 原始字节。"""

    def __init__(self, cfg: Config):
        self.dir = os.path.join(cfg.OUTPUT_DIR, "_content_cache")
        os.makedirs(self.dir, exist_ok=True)
        self.ttl = cfg.CONTENT_CACHE_TTL_HOURS * 3600

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.bin")

    def get(self, key: str, max_age: Optional[float] = None) -> Optional[bytes]:
        p = self._path(key)
        try:
            st = os.stat(p)
            age = time.time() - st.st_mtime
            if age <= (max_age if max_age is not None else self.ttl):
                with open(p, "rb") as f:
                    return f.read()
        except OSError:
            return None
        return None

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        tmp = p + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except OSError as e:
            logger.warning("content cache write failed: %s", e)

    def prune(self, keep_hours: int = 72) -> int:
        """清理过期缓存文件。"""
        p = self.dir
        if not os.path.isdir(p):
            return 0
        cutoff = time.time() - keep_hours * 3600
        removed = 0
        for name in os.listdir(p):
            fp = os.path.join(p, name)
            try:
                if os.path.isfile(fp) and os.stat(fp).st_mtime < cutoff:
                    os.remove(fp)
                    removed += 1
            except OSError:
                pass
        return removed


class DomainLimiter:
    """按域名限流：同一 host 在 min_interval 内最多请求一次。"""

    def __init__(self, min_interval: float = 1.0):
        self._last: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.min_interval = min_interval

    async def wait(self, identity: SourceIdentity) -> None:
        host = _netloc(identity.canonical)
        if not host:
            return
        async with self._lock:
            last = self._last.get(host)
            now = time.monotonic()
            if last is not None:
                wait = self.min_interval - (now - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


def _netloc(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc.split(":")[0].lower()
    except Exception:
        return ""


class Fetcher:
    """统一内容获取。目标：一次 pipeline 内同 URL 仅一次真实请求。"""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession, cache: Optional[ContentCache] = None):
        self.cfg = cfg
        self.session = session
        self.cache = cache or ContentCache(cfg)
        self._memo: Dict[str, FetchResult] = {}
        self._limiter = DomainLimiter(cfg.REQUEST_DELAY)

    async def fetch(self, url: str, use_cache: bool = True, max_age: Optional[float] = None) -> FetchResult:
        """获取内容。use_cache=True 时命中 filesystem 内容缓存。"""
        ident = SourceIdentity.normalize(url)
        if ident in self._memo:
            return self._memo[ident]

        cache_key = hashlib.sha256(ident.encode("utf-8")).hexdigest()
        if use_cache:
            cached = self.cache.get(cache_key, max_age=max_age)
            if cached is not None:
                res = FetchResult(
                    status=200,
                    data=cached,
                    from_cache=True,
                    content_hash=hashlib.sha256(cached).hexdigest(),
                    fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                self._memo[ident] = res
                return res

        res = await self._fetch_live(ident)
        if res.status == 200 and res.data:
            self.cache.put(cache_key, res.data)
        self._memo[ident] = res
        return res

    async def _fetch_live(self, url: str) -> FetchResult:
        identity = SourceIdentity(url=url, canonical=url)
        await self._limiter.wait(identity)

        last_error: Optional[str] = None
        attempts = 0
        for attempt in range(1, 4):
            attempts = attempt
            timeout = aiohttp.ClientTimeout(total=self.cfg.REQUEST_TIMEOUT)
            headers = {"User-Agent": "SUB-BOT-V2/2.0 (github.com; daily validator)"}
            try:
                async with self.session.get(url, timeout=timeout, headers=headers, ssl=False) as resp:
                    if resp.status in (404, 403, 410):
                        return FetchResult(status=resp.status, error=f"HTTP {resp.status}")
                    if resp.status != 200:
                        last_error = f"HTTP {resp.status}"
                        if resp.status >= 500 and attempt < 3:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return FetchResult(status=resp.status, error=last_error, attempts=attempt)
                    data = await resp.read()
                    if len(data) > self.cfg.MAX_CONTENT_SIZE:
                        return FetchResult(status=200, error="content too large", attempts=attempt)
                    if resp.headers.get("Content-Encoding") == "gzip":
                        try:
                            data = gzip.decompress(data)
                        except Exception:
                            pass
                    ctype = resp.headers.get("Content-Type", "")
                    if "text" in ctype or "json" in ctype or "octet" in ctype or not ctype:
                        try:
                            decoded = data.decode("utf-8", errors="replace")
                        except Exception:
                            decoded = ""
                        if data and len(decoded.strip()) == 0:
                            return FetchResult(status=200, error="empty body", attempts=attempt)
                    return FetchResult(
                        status=200,
                        data=data,
                        content_hash=hashlib.sha256(data).hexdigest(),
                        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        attempts=attempt,
                    )
            except asyncio.TimeoutError:
                last_error = "timeout"
            except aiohttp.ClientError as e:
                last_error = f"io_error: {type(e).__name__}"
            except Exception as e:  # pragma: no cover
                last_error = f"error: {e}"
            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))
        return FetchResult(status=0, error=last_error, attempts=attempts)