"""GitHub API client：搜索（REST/GraphQL）、trees、contents。"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional, Dict

import aiohttp

from config import Config

logger = logging.getLogger("gh")

API = "https://api.github.com"


@dataclass
class RateBudget:
    core_remaining: Optional[int] = None
    core_reset: Optional[int] = None
    search_remaining: Optional[int] = None
    search_reset: Optional[int] = None

    def update(self, headers):
        resource = headers.get("x-ratelimit-resource", "core")
        try:
            remaining = int(headers.get("x-ratelimit-remaining", ""))
            reset = int(headers.get("x-ratelimit-reset", ""))
        except (TypeError, ValueError):
            return
        if resource == "search":
            self.search_remaining = remaining
            self.search_reset = reset
        else:
            self.core_remaining = remaining
            self.core_reset = reset

    def can_search(self) -> bool:
        return self.search_remaining is None or self.search_remaining > 0

    def wait_if_needed(self, resource: str = "core"):
        """剩余不足时 sleep 到 reset。"""
        if resource == "search":
            if self.search_remaining is not None and self.search_remaining <= 0:
                wait = max(0, (self.search_reset or time.time()) - time.time())
                if wait <= 3600:
                    time.sleep(min(wait + 5, 120))
        else:
            if self.core_remaining is not None and self.core_remaining <= 0:
                wait = max(0, (self.core_reset or time.time()) - time.time())
                if wait <= 3600:
                    time.sleep(min(wait + 5, 120))


@dataclass
class TreeScanOutcome:
    entries: List[Dict] = field(default_factory=list)  # 每个含 path
    truncated: bool = False


class GitHubClient:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self.budget = RateBudget()
        self._headers = {"Accept": "application/vnd.github+json", "User-Agent": "sub-bot-v2"}
        if cfg.GITHUB_TOKEN:
            self._headers["Authorization"] = f"Bearer {cfg.GITHUB_TOKEN}"

    async def _get(self, url: str, params: Optional[Dict] = None, resource: str = "core",
                   _retried: bool = False) -> Optional[Dict]:
        self.budget.wait_if_needed(resource)
        try:
            async with self.session.get(url, headers=self._headers, params=params) as resp:
                self.budget.update(resp.headers)
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except aiohttp.ContentTypeError:
                        return None
                if resp.status == 404:
                    return None
                if resp.status in (403, 429):
                    # 403/429 在 search 资源上常是"secondary rate limit"（突发请求触发的滑动窗限流），
                    # 跟 x-ratelimit-remaining 没关系，重试前必须真的停够；优先看 Retry-After。
                    if not _retried:
                        retry_after = resp.headers.get("retry-after")
                        try:
                            wait = float(retry_after) if retry_after else 20.0
                        except ValueError:
                            wait = 20.0
                        wait = min(max(wait, 5.0), 60.0)
                        logger.warning("rate/limit hit on %s => %s, waiting %.0fs then retrying once",
                                       url, resp.status, wait)
                        await asyncio.sleep(wait)
                        return await self._get(url, params, resource, _retried=True)
                    logger.warning("rate/limit hit on %s => %s (after retry, giving up)", url, resp.status)
                    return None
                logger.warning("GET %s -> %s", url, resp.status)
                return None
        except Exception as e:
            logger.warning("GET %s error: %s", url, e)
            return None

    async def rate_limit(self) -> Optional[Dict]:
        return await self._get(f"{API}/rate_limit")

    async def search_repos(self, query: str, per_page: int, sort: str = "updated") -> List[Dict]:
        """搜索仓库，返回 metadata 列表。"""
        params = {"q": query, "per_page": str(per_page), "sort": sort, "order": "desc"}
        data = await self._get(f"{API}/search/repositories", params=params, resource="search")
        if not data:
            return []
        return data.get("items", [])

    async def search_code(self, query: str, per_page: int) -> List[Dict]:
        params = {"q": query, "per_page": str(per_page)}
        data = await self._get(f"{API}/search/code", params=params, resource="search")
        if not data:
            return []
        return data.get("items", [])

    async def get_repo(self, full_name: str) -> Optional[Dict]:
        return await self._get(f"{API}/repos/{full_name}")

    async def get_tree(self, full_name: str, branch: str) -> Optional[TreeScanOutcome]:
        """Trees API recursive=1。返回（条目, truncated 标志）。"""
        url = f"{API}/repos/{full_name}/git/trees/{urllib.parse.quote(branch)}?recursive=1"
        data = await self._get(url)
        if not data:
            return None
        truncated = bool(data.get("truncated", False))
        entries = [e for e in data.get("tree", []) if e.get("path")]
        return TreeScanOutcome(entries=entries, truncated=truncated)

    async def get_file_content(self, full_name: str, path: str, branch: str) -> Optional[str]:
        """通过 contents API 拿单文件内容（truncated fallback 判定用）。未被 pipeline 主动调用。"""
        url = f"{API}/repos/{full_name}/contents/{path}"
        try:
            async with self.session.get(
                f"{url}?ref={urllib.parse.quote(branch)}", headers=self._headers
            ) as resp:
                self.budget.update(resp.headers)
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, dict) and data.get("encoding") == "base64":
                    import base64

                    try:
                        return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
                    except Exception:
                        return None
                return None
        except Exception as e:
            logger.warning("get file content %s error: %s", path, e)
            return None

    async def get_contents_dir(self, full_name: str, path: str, branch: str) -> List[Dict]:
        """contents API 列目录（truncated fallback 时遍历用）。"""
        url = f"{API}/repos/{full_name}/contents/{path}"
        try:
            async with self.session.get(
                f"{url}?ref={urllib.parse.quote(branch)}", headers=self._headers
            ) as resp:
                self.budget.update(resp.headers)
                if resp.status != 200:
                    return []
                data = await resp.json()
                if isinstance(data, list):
                    return data
                return []
        except Exception as e:
            logger.warning("list dir %s error: %s", path, e)
            return []