"""Discovery：Provider 协议可插拔。GitHub 是其中一个 Provider，pipeline 不感知 GitHub 细节。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

from bot.gh import GitHubClient
from bot.domain import RepoInfo
from bot.store import Store
from config import Config

logger = logging.getLogger("discovery")


class DiscoveryProvider:
    """所有 Provider 的协议：输入配置与历史，返回 RepoInfo 列表。"""

    async def discover(self, seen: set, limit: int) -> List[RepoInfo]:
        raise NotImplementedError


@dataclass
class GitHubSearchProvider(DiscoveryProvider):
    """按关键词搜索仓库（REST + GraphQL 兜底）。"""

    cfg: Config
    gh: GitHubClient

    async def _query(self, keyword: str) -> str:
        qualifiers = ["fork:false"]
        if self.cfg.SEARCH_PUSHED_DAYS:
            since = (datetime.utcnow() - timedelta(days=self.cfg.SEARCH_PUSHED_DAYS)).strftime("%Y-%m-%d")
            qualifiers.append(f"pushed:>={since}")
        return f"{keyword} {' '.join(qualifiers)}"

    async def _graphql_fallback(self, keyword: str, per_page: int) -> List[RepoInfo]:
        query = """query($q: String!, $n: Int!) {
          search(query: $q, type: REPOSITORY, first: $n) {
            nodes {
              ... on Repository {
                nameWithOwner fullName: nameWithOwner
                defaultBranchRef { name }
                pushedAt
              }
            }
          }
        }"""
        body = {"query": query, "variables": {"q": keyword, "n": per_page}}
        headers = {
            "Authorization": f"Bearer {self.cfg.GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "sub-bot-v2",
        }
        try:
            async with self.gh.session.post("https://api.github.com/graphql", json=body, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("graphql search %s -> %s", keyword, resp.status)
                    return []
                data = await resp.json()
                nodes = (data.get("data", {}) or {}).get("search", {}).get("nodes", []) or []
                out = []
                for n in nodes:
                    fn = n.get("nameWithOwner") or n.get("fullName") or ""
                    db = (n.get("defaultBranchRef") or {}).get("name") or "main"
                    out.append(RepoInfo(full_name=fn, branch=db, default_branch=db,
                                        pushed_at=n.get("pushedAt") or "", provenance="search"))
                return out
        except Exception as e:
            logger.warning("graphql search error: %s", e)
            return []

    async def discover(self, seen: set, limit: int) -> List[RepoInfo]:
        out: List[RepoInfo] = []
        for keyword in self.cfg.SEARCH_KEYWORDS:
            if len(out) >= limit:
                break
            query = await self._query(keyword)
            items = await self.gh.search_repos(query, self.cfg.REPOS_PER_KEYWORD)
            want = []
            if items:
                for it in items:
                    full = it.get("full_name", "")
                    db = it.get("default_branch") or "main"
                    want.append(RepoInfo(full_name=full, branch=db, default_branch=db,
                                         pushed_at=it.get("pushed_at") or "", provenance="search"))
            else:
                want = await self._graphql_fallback(query, self.cfg.REPOS_PER_KEYWORD)
            for repo in want:
                if repo.full_name in seen:
                    continue
                seen.add(repo.full_name)
                out.append(repo)
                if len(out) >= limit:
                    break
            logger.info("keyword=%s hits=%d total=%d", keyword, len(want), len(out))
        return out[:limit]


@dataclass
class GitHubDebugProvider(DiscoveryProvider):
    """DEBUG_REPOSITORIES 白名单：不消耗搜索配额。"""

    cfg: Config
    gh: GitHubClient

    async def discover(self, seen: set, limit: int) -> List[RepoInfo]:
        out: List[RepoInfo] = []
        for full_name in self.cfg.DEBUG_REPOSITORIES:
            meta = await self.gh.get_repo(full_name)
            if not meta:
                continue
            db = meta.get("default_branch") or "main"
            repo = RepoInfo(full_name=full_name, branch=db, default_branch=db,
                            pushed_at=meta.get("pushed_at") or "", provenance="debug")
            if repo.full_name in seen:
                continue
            seen.add(repo.full_name)
            out.append(repo)
        return out[:limit]


@dataclass
class ForeignSearchProvider(DiscoveryProvider):
    """GitLab / Gitee 搜索：无 GitHub 配额消耗。"""

    cfg: Config
    foreign: "ForeignClient"

    async def discover(self, seen: set, limit: int) -> List[RepoInfo]:
        out: List[RepoInfo] = []
        for host in self.cfg.ENABLE_FOREIGN_HOSTS:
            for keyword in self.cfg.SEARCH_KEYWORDS:
                if len(out) >= limit:
                    break
                items = await self.foreign.search_repos(host, keyword, self.cfg.REPOS_PER_KEYWORD)
                for repo in items:
                    if repo.full_name in seen:
                        continue
                    seen.add(repo.full_name)
                    out.append(repo)
                    if len(out) >= limit:
                        break
                if items:
                    logger.info("foreign[%s] keyword=%s hits=%d total=%d",
                                host, keyword, len(items), len(out))
        return out[:limit]


@dataclass
class HistoryProvider(DiscoveryProvider):
    """从 state.db 历史中召回曾经的活跃仓库（对"不再命中关键词但仍有价值"的源友好）。"""

    cfg: Config
    gh: GitHubClient
    store: Store

    async def discover(self, seen: set, limit: int) -> List[RepoInfo]:
        out: List[RepoInfo] = []
        repos = self.store.get_history_repos(self.cfg.MAX_REPOS_TOTAL)
        for full_name, pushed in repos:
            if full_name in seen:
                continue
            meta = await self.gh.get_repo(full_name)
            if not meta:
                continue
            db = meta.get("default_branch") or "main"
            seen.add(full_name)
            out.append(RepoInfo(full_name=full_name, branch=db, default_branch=db,
                                pushed_at=meta.get("pushed_at") or pushed or "", provenance="history"))
        return out[:limit]


def build_providers(cfg: Config, gh: GitHubClient, store, foreign: "ForeignClient") -> List[DiscoveryProvider]:
    providers: List[DiscoveryProvider] = []
    if cfg.DEBUG_REPOSITORIES:
        providers.append(GitHubDebugProvider(cfg=cfg, gh=gh))
    providers.append(GitHubSearchProvider(cfg=cfg, gh=gh))
    if foreign is not None and cfg.ENABLE_FOREIGN_HOSTS:
        providers.append(ForeignSearchProvider(cfg=cfg, foreign=foreign))
    providers.append(HistoryProvider(cfg=cfg, gh=gh, store=store))
    return providers