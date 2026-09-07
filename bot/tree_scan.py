"""Trees API 扫描：recursive=1 一次拉全；truncated 必须走 directory fallback（宁可慢不可少扫）。"""

from __future__ import annotations

import logging
from typing import List

from bot.gh import GitHubClient, TreeScanOutcome
from config import Config

logger = logging.getLogger("tree_scan")

# Clash / 订阅相关文件路径打分
FILE_SCORE = {
    ".yaml": 30, ".yml": 30, ".txt": 20, ".conf": 20, ".list": 15, ".json": 8, ".sub": 20, ".clash": 25,
}
GOOD_FILE_NAMES = {
    "clash", "node", "subscribe", "subscription", "sub", "proxy", "v2ray", "free", "config", "link", "urls",
}
BAD_FILE_NAMES = {"readme", "license", "changelog", "package", "requirements", "lock", "gitignore", "docker"}
BAD_DIRS = {".git", "node_modules", "dist", "build", "venv", ".venv", "vendor", ".github", "test", "tests"}


def score_path(path: str) -> int:
    """对仓库内文件路径打分，越高越可能是订阅。"""
    low = path.lower()
    base = low.split("/")[-1]
    if any(b in base for b in BAD_FILE_NAMES):
        return -100
    parts = set(low.replace("/", " ").replace("_", " ").replace("-", " ").split())
    if parts & BAD_DIRS:
        return -50
    score = 0
    for ext, s in FILE_SCORE.items():
        if low.endswith(ext):
            score += s
            break
    if any(g in parts for g in GOOD_FILE_NAMES) and any(g in low for g in GOOD_FILE_NAMES):
        score += 25
    if "clash" in low or "v2ray" in low:
        score += 20
    if low.endswith((".txt", ".yaml", ".yml", ".conf", ".list")) and ("sub" in low or "node" in low or "link" in low):
        score += 30
    return score


def _flatten_tree_entries(entries: List[dict]) -> List[str]:
    return [e.get("path", "") for e in entries]


class TreeScanner:
    """树的递归扫描 + truncated fallback。按 host 分派：github -> Trees API，foreign -> 各自 REST。"""

    def __init__(self, cfg: Config, gh: GitHubClient, foreign=None):
        self.cfg = cfg
        self.gh = gh
        self.foreign = foreign

    async def scan(self, repo) -> TreeScanOutcome:
        """主路径：Trees API recursive。truncated 时目录 fallback。"""
        host = getattr(repo, "host", "github")
        if host != "github":
            if self.foreign is None:
                return TreeScanOutcome(entries=[], truncated=False)
            outcome = await self.foreign.get_tree(host, repo.full_name, repo.branch)
            if outcome is None:
                return TreeScanOutcome(entries=[], truncated=False)
            paths = _flatten_tree_entries(outcome.entries)
            if outcome.truncated:
                logger.warning("[%s] %s tree truncated (foreign host fallback unsupported)", host, repo.full_name)
            return TreeScanOutcome(entries=paths, truncated=False)

        outcome = await self.gh.get_tree(repo.full_name, repo.branch)
        if outcome is None:
            return TreeScanOutcome(entries=[], truncated=False)
        entries = _flatten_tree_entries(outcome.entries)
        if not outcome.truncated:
            return TreeScanOutcome(entries=entries, truncated=False)
        logger.warning("[%s] tree truncated -> directory fallback", repo.full_name)
        fallback = await self._directory_walk(repo.full_name, repo.branch)
        merged = list(dict.fromkeys(entries + fallback))
        return TreeScanOutcome(entries=merged, truncated=False)

    async def _directory_walk(self, full_name: str, branch: str) -> List[str]:
        """contents API 递归遍历目录，深度受限。"""
        paths: List[str] = []

        async def walk(path: str, depth: int):
            if depth > self.cfg.MAX_TREE_FALLBACK_DEPTH:
                return
            items = await self.gh.get_contents_dir(full_name, path, branch)
            for item in items:
                sub = item.get("path", "")
                if item.get("type") == "dir":
                    await walk(sub, depth + 1)
                elif item.get("type") == "file":
                    if item.get("size", 0) <= self.cfg.MAX_CONTENT_SIZE:
                        paths.append(sub)

        await walk("", 0)
        return paths

    def top_files(self, entries: List[str], limit: int) -> List[str]:
        """按路径分数过滤并 top-N。"""
        scored = [(score_path(p), p) for p in entries]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [p for _, p in scored if _ > 0][:limit]