"""
单一配置入口：内置默认 < 环境变量 < CLI
所有可调参数一律由环境变量驱动，GitHub Actions 的 workflow 输入才能真正生效。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return default if v == "" else v


def _env_list(name: str, default: str = "") -> List[str]:
    raw = _env(name, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ---------- GitHub ----------
    GITHUB_TOKEN: str = field(default_factory=lambda: _env("PAT_TOKEN") or _env("GITHUB_TOKEN"))
    REQUEST_TIMEOUT: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 30))
    REQUEST_DELAY: float = field(default_factory=lambda: _env_int("REQUEST_DELAY", 0.3))

    # ---------- 搜索 ----------
    # 仓库级搜索（只匹配 repo 名/描述/README，不匹配文件内容）：用描述性词汇，
    # 协议 scheme 字符串（vmess:// 等）挪到 CODE_SEARCH_KEYWORDS 走 code search，命中率高得多。
    SEARCH_KEYWORDS: List[str] = field(default_factory=lambda: _env_list(
        "SEARCH_KEYWORDS", "node_list,free clash,free v2ray,free nodes,subscribe,subscription,clash config,v2ray config"))
    # 代码内容搜索关键词：匹配文件内容里的协议链接，命中的就是真正含节点的文件所在仓库
    CODE_SEARCH_KEYWORDS: List[str] = field(default_factory=lambda: _env_list(
        "CODE_SEARCH_KEYWORDS", "vmess://,vless://,trojan://,ss://"))
    REPOS_PER_KEYWORD: int = field(default_factory=lambda: _env_int("REPOS_PER_KEYWORD", 5))
    MAX_REPOS_TOTAL: int = field(default_factory=lambda: _env_int("MAX_REPOS_TOTAL", 100))
    # 每个 Provider 每天的保底/上限名额：先到先得会导致 GitHubSearch 一家独占预算，
    # 把 code search / GitLab / Gitee 挤到 0（实测发生过）。四路各自独立限额，互不挤占。
    HISTORY_REPOS_LIMIT: int = field(default_factory=lambda: _env_int("HISTORY_REPOS_LIMIT", 30))
    GITHUB_SEARCH_REPOS_LIMIT: int = field(default_factory=lambda: _env_int("GITHUB_SEARCH_REPOS_LIMIT", 35))
    CODE_SEARCH_REPOS_LIMIT: int = field(default_factory=lambda: _env_int("CODE_SEARCH_REPOS_LIMIT", 15))
    FOREIGN_REPOS_LIMIT: int = field(default_factory=lambda: _env_int("FOREIGN_REPOS_LIMIT", 20))
    SEARCH_PUSHED_DAYS: int = field(default_factory=lambda: _env_int("SEARCH_PUSHED_DAYS", 30))
    # 启用 GitLab / Gitee 作为额外无配额来源。gitee 的公开搜索接口对常见关键词
    # 长期返回 total_count:0（连不带 token 都一样），默认关掉，需要时手动加回 "gitlab,gitee"
    ENABLE_FOREIGN_HOSTS: List[str] = field(default_factory=lambda: _env_list(
        "ENABLE_FOREIGN_HOSTS", "gitlab"))
    GITEE_TOKEN: str = field(default_factory=lambda: _env("GITEE_TOKEN"))
    # 调试/白名单：优先扫描（不消耗搜索配额）
    DEBUG_REPOSITORIES: List[str] = field(default_factory=lambda: _env_list("DEBUG_REPOSITORIES"))

    # ---------- Telegram ----------
    # 公开频道用户名列表（逗号分隔，@可省略），如 "freenode_share,clashnode"
    # 为空则完全跳过，不产生任何请求。不需要 Bot Token（走公开预览页 t.me/s/<channel>）。
    TELEGRAM_CHANNELS: List[str] = field(default_factory=lambda: _env_list("TELEGRAM_CHANNELS"))

    # ---------- 扫描 ----------
    MAX_TREE_FALLBACK_DEPTH: int = field(default_factory=lambda: _env_int("MAX_TREE_FALLBACK_DEPTH", 3))
    MAX_FILES_PER_REPO: int = field(default_factory=lambda: _env_int("MAX_FILES_PER_REPO", 25))
    MAX_CANDIDATE_FILES_TOTAL: int = field(default_factory=lambda: _env_int("MAX_CANDIDATE_FILES_TOTAL", 400))
    MAX_CONTENT_SIZE: int = field(default_factory=lambda: _env_int("MAX_CONTENT_SIZE", 1024 * 1024))

    # ---------- 验证 ----------
    VERIFY_SUBSCRIPTIONS: bool = field(default_factory=lambda: _env_bool("VERIFY_SUBSCRIPTIONS", True))
    MIN_VALID_NODES: int = field(default_factory=lambda: _env_int("MIN_VALID_NODES", 3))
    MAX_VERIFY_CANDIDATES: int = field(default_factory=lambda: _env_int("MAX_VERIFY_CANDIDATES", 150))
    MIN_SCORE_TO_VERIFY: int = field(default_factory=lambda: _env_int("MIN_SCORE_TO_VERIFY", 25))
    MAX_CONCURRENT_VERIFY: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_VERIFY", 12))
    VERIFY_TIMEOUT: int = field(default_factory=lambda: _env_int("VERIFY_TIMEOUT", 20))

    # ---------- 缓存 ----------
    CONTENT_CACHE_TTL_HOURS: int = field(default_factory=lambda: _env_int("CONTENT_CACHE_TTL_HOURS", 12))
    VERIFY_CACHE_TTL_HOURS: int = field(default_factory=lambda: _env_int("VERIFY_CACHE_TTL_HOURS", 24))

    # ---------- 状态存储 ----------
    STATE_DB: str = field(default_factory=lambda: _env("STATE_DB", "output/state.db"))
    STATE_MAX_AGE_DAYS: int = field(default_factory=lambda: _env_int("STATE_MAX_AGE_DAYS", 60))

    # ---------- 输出 ----------
    OUTPUT_DIR: str = field(default_factory=lambda: _env("OUTPUT_DIR", "output"))
    KEEP_HISTORY_RUNS: int = field(default_factory=lambda: _env_int("KEEP_HISTORY_RUNS", 7))

    # ---------- 邮件 ----------
    QQ_EMAIL: str = field(default_factory=lambda: _env("QQ_EMAIL"))
    QQ_EMAIL_AUTH_CODE: str = field(default_factory=lambda: _env("QQ_EMAIL_AUTH_CODE"))
    TO_EMAIL: str = field(default_factory=lambda: _env("TO_EMAIL"))
    # 无新增时仍发送每日摘要
    NOTIFY_ALWAYS: bool = field(default_factory=lambda: _env_bool("NOTIFY_ALWAYS", True))

    # ---------- 订阅推送 ----------
    # 将验证后的订阅推送到当前 botsub 仓库，供 subs-check / V2RAYN 通过 raw URL 订阅
    SUB_REPO_NAME: str = field(default_factory=lambda: _env("SUB_REPO_NAME", "botsub"))
    SUB_REPO_BRANCH: str = field(default_factory=lambda: _env("SUB_REPO_BRANCH", "main"))

    @property
    def email_enabled(self) -> bool:
        return bool(self.QQ_EMAIL and self.QQ_EMAIL_AUTH_CODE and self.TO_EMAIL)

    @classmethod
    def load(cls) -> "Config":
        return cls()