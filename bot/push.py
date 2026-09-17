"""将订阅输出推送到独立的 GitHub 仓库，供 subs-check / V2RAYN 通过 raw URL 订阅。"""

from __future__ import annotations

import logging
import os
from typing import List

import aiohttp

from config import Config

logger = logging.getLogger("push")


def _raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"


class SubscriptionPusher:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self._headers = {"Accept": "application/vnd.github+json", "User-Agent": "sub-bot-v2"}
        if cfg.GITHUB_TOKEN:
            self._headers["Authorization"] = f"Bearer {cfg.GITHUB_TOKEN}"

        self.owner = getattr(cfg, "SUB_REPO_OWNER", "") or os.environ.get(
            "SUB_REPO_OWNER", cfg.GITHUB_TOKEN and "wu242005546-ai" or ""
        )
        self.repo = getattr(cfg, "SUB_REPO_NAME", "") or os.environ.get(
            "SUB_REPO_NAME", "botsub-subscriptions"
        )
        self.branch = getattr(cfg, "SUB_REPO_BRANCH", "") or os.environ.get(
            "SUB_REPO_BRANCH", "main"
        )
        self.enabled = bool(self.owner and self.repo)

    async def _api_put_file(self, path: str, content: str) -> bool:
        """用 GitHub Contents API 写入文件（自动创建或更新）。"""
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{path}"
        import base64

        body = {
            "message": f"chore(subs): update {path}",
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": self.branch,
        }
        try:
            async with self.session.put(
                url,
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (200, 201):
                    logger.info("push: %s -> %s", path, _raw_url(self.owner, self.repo, self.branch, path))
                    return True
                if resp.status == 404:
                    logger.warning("push: repo %s/%s not found or branch %s missing", self.owner, self.repo, self.branch)
                    return False
                text = await resp.text()
                logger.warning("push: %s -> HTTP %s: %s", path, resp.status, text[:200])
                return False
        except Exception as e:
            logger.warning("push: %s error: %s", path, e)
            return False

    async def push_subscriptions(
        self,
        clash_urls: List[str],
        v2ray_urls: List[str],
        proto_urls: List[str],
        telegram_nodes: List[str],
    ) -> dict[str, str]:
        """将订阅文件推送到目标 repo。返回 {文件名: raw_url}。"""
        if not self.enabled:
            logger.info("push: disabled (no SUB_REPO_OWNER / SUB_REPO_NAME configured)")
            return {}

        raw_urls: dict[str, str] = {}

        files: dict[str, str] = {}
        if clash_urls:
            files["sub_clash.txt"] = "\n".join(dict.fromkeys(clash_urls)) + "\n"
        if v2ray_urls:
            files["sub_v2ray.txt"] = "\n".join(dict.fromkeys(v2ray_urls)) + "\n"
        if proto_urls:
            files["sub_nodes.txt"] = "\n".join(dict.fromkeys(proto_urls)) + "\n"
        if telegram_nodes:
            files["sub_telegram.txt"] = "\n".join(dict.fromkeys(telegram_nodes)) + "\n"

        for fname, body in files.items():
            ok = await self._api_put_file(fname, body)
            if ok:
                raw_urls[fname] = _raw_url(self.owner, self.repo, self.branch, fname)

        if raw_urls:
            logger.info("push: %d subscription file(s) pushed to %s/%s", len(raw_urls), self.owner, self.repo)
        else:
            logger.info("push: no files to push")

        return raw_urls
