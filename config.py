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
    SEARCH_KEYWORDS: List[str] = field(default_factory=lambda: _env_list(
        "SEARCH_KEYWORDS", "node_list,free clash,vmess://,vless://,trojan://,ss://,subscribe,subscription,clash"))
    REPOS_PER_KEYWORD: int = field(default_factory=lambda: _env_int("REPOS_PER_KEYWORD", 3))
    MAX_REPOS_TOTAL: int = field(default_factory=lambda: _env_int("MAX_REPOS_TOTAL", 40))
    SEARCH_PUSHED_DAYS: int = field(default_factory=lambda: _env_int("SEARCH_PUSHED_DAYS", 30))
    # 启用 GitLab / Gitee 作为额外无配额来源
    ENABLE_FOREIGN_HOSTS: List[str] = field(default_factory=lambda: _env_list(
        "ENABLE_FOREIGN_HOSTS", "gitlab,gitee"))
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
    MAX_VERIFY_CANDIDATES: int = field(default_factory=lambda: _env_int("MAX_VERIFY_CANDIDATES", 80))
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

    @property
    def email_enabled(self) -> bool:
        return bool(self.QQ_EMAIL and self.QQ_EMAIL_AUTH_CODE and self.TO_EMAIL)

    @classmethod
    def load(cls) -> "Config":
        return cls()