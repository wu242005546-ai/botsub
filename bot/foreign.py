"""GitLab / Gitee 统一客户端：搜索仓库（REST）+ 仓库 tree（REST）+ raw URL 构造。

与 GitHub 的差异：
  - API base 不同（gitlab.com/api/v4、gitee.com/api/v5）
  - GitLab tree 用分页列表式 repository/tree?recursive=true；Gitee 用 git/trees?recursive=1
  - raw 文件 URL 格式不同
本节只负责"上游访问"，不涉及解析/打分，Pipeline 不感知平台细节。
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import List, Optional

import aiohttp

from bot.domain import RepoInfo
from bot.gh import TreeScanOutcome
from config import Config

logger = logging.getLogger("foreign")

GITLAB_API = "https://gitlab.com/api/v4"
GITEE_API = "https://gitee.com/api/v5"


def gitlab_raw_url(full_name: str, branch: str, path: str) -> str:
    return f"https://gitlab.com/{full_name}/-/raw/{urllib.parse.quote(branch)}/{path}"


def gitee_raw_url(full_name: str, branch: str, path: str) -> str:
    return f"https://gitee.com/{full_name}/raw/{urllib.parse.quote(branch)}/{path}"


class ForeignClient:
    """GitLab / Gitee REST 客户端。按 host 分派同名方法。"""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self._headers = {"Accept": "application/json", "User-Agent": "sub-bot-v2"}

    async def _get_json(self, url: str, params: Optional[dict] = None, _retried: bool = False):
        try:
            async with self.session.get(url, headers=self._headers, params=params) as resp:
                if resp.status != 200:
                    # GitLab 未鉴权公开搜索经常间歇性 500，重试一次再放弃
                    if resp.status >= 500 and not _retried:
                        logger.info("foreign GET %s -> %s, retrying once", url, resp.status)
                        await asyncio.sleep(1.5)
                        return await self._get_json(url, params, _retried=True)
                    logger.warning("foreign GET %s -> %s", url, resp.status)
                    return None
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError:
                    return None
        except Exception as e:
            logger.warning("foreign GET %s error: %s", url, e)
            return None

    # ---------- 搜索 ----------

    async def search_gitlab(self, keyword: str, per_page: int) -> List[RepoInfo]:
        params = {"search": keyword, "per_page": str(per_page), "order_by": "last_activity_at", "sort": "desc"}
        data = await self._get_json(f"{GITLAB_API}/projects", params)
        if not isinstance(data, list):
            return []
        out: List[RepoInfo] = []
        for it in data:
            full = it.get("path_with_namespace") or ""
            if not full:
                continue
            db = it.get("default_branch") or "main"
            out.append(RepoInfo(
                full_name=full, branch=db, default_branch=db,
                pushed_at=it.get("last_activity_at") or "", provenance="gitlab", host="gitlab",
            ))
        return out

    async def search_gitee(self, keyword: str, per_page: int) -> List[RepoInfo]:
        params = {"q": keyword, "per_page": str(per_page)}
        if not self.cfg.GITEE_TOKEN:
            logger.info("gitee search skipped: GITEE_TOKEN not set (search API requires it)")
            return []
        params["access_token"] = self.cfg.GITEE_TOKEN
        data = await self._get_json(f"{GITEE_API}/search/repositories", params)
        if not isinstance(data, list):
            return []
        out: List[RepoInfo] = []
        for it in data:
            full = it.get("full_name") or (it.get("namespace") and it.get("path")) or ""
            if not full:
                continue
            db = it.get("default_branch") or "master"
            out.append(RepoInfo(
                full_name=full, branch=db, default_branch=db,
                pushed_at=it.get("pushed_at") or it.get("updated_at") or "", provenance="gitee", host="gitee",
            ))
        return out

    # ---------- tree ----------

    async def tree_gitlab(self, full_name: str, branch: str) -> Optional[TreeScanOutcome]:
        url = f"{GITLAB_API}/projects/{urllib.parse.quote(full_name, safe='')}/repository/tree"
        entries: list = []
        page = 1
        while page <= 10:
            params = {"ref": branch, "recursive": "true", "per_page": "100", "page": str(page)}
            data = await self._get_json(url, params)
            if not isinstance(data, list) or not data:
                break
            for it in data:
                if it.get("type") == "blob":
                    entries.append({"path": it.get("path", "")})
            if len(data) < 100:
                break
            page += 1
        return TreeScanOutcome(entries=entries, truncated=False)

    async def tree_gitee(self, full_name: str, branch: str) -> Optional[TreeScanOutcome]:
        url = f"{GITEE_API}/repos/{full_name}/git/trees/{urllib.parse.quote(branch)}"
        data = await self._get_json(url, {"recursive": "1"})
        if not isinstance(data, dict):
            return None
        tree = data.get("tree") or []
        entries = [{"path": e.get("path", "")} for e in tree if isinstance(e, dict) and e.get("type") == "blob"]
        return TreeScanOutcome(entries=entries, truncated=bool(data.get("truncated", False)))

    # ---------- 统一入口 ----------

    async def get_tree(self, host: str, full_name: str, branch: str) -> Optional[TreeScanOutcome]:
        if host == "gitlab":
            return await self.tree_gitlab(full_name, branch)
        if host == "gitee":
            return await self.tree_gitee(full_name, branch)
        return None

    async def search_repos(self, host: str, keyword: str, per_page: int) -> List[RepoInfo]:
        if host == "gitlab":
            return await self.search_gitlab(keyword, per_page)
        if host == "gitee":
            return await self.search_gitee(keyword, per_page)
        return []

    def raw_url(self, host: str, full_name: str, branch: str, path: str) -> str:
        if host == "gitlab":
            return gitlab_raw_url(full_name, branch, path)
        if host == "gitee":
            return gitee_raw_url(full_name, branch, path)
        raise ValueError(f"unsupported host: {host}")