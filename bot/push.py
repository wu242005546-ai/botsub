"""将订阅输出写入本地目录，由 GitHub Actions 的 git push 自动同步到当前 botsub 仓库。

subs-check / V2RAYN 通过 raw.githubusercontent.com 订阅：
  https://raw.githubusercontent.com/wu242005546-ai/botsub/main/sub_clash.txt
  https://raw.githubusercontent.com/wu242005546-ai/botsub/main/sub_v2ray.txt
  https://raw.githubusercontent.com/wu242005546-ai/botsub/main/sub_telegram.txt
"""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger("push")


class SubscriptionPusher:
    def __init__(self, cfg, session=None):
        self.repo = getattr(cfg, "SUB_REPO_NAME", "") or "botsub"
        self.branch = getattr(cfg, "SUB_REPO_BRANCH", "") or "main"

    def push_subscriptions(
        self,
        clash_urls: List[str],
        v2ray_urls: List[str],
        proto_urls: List[str],
        telegram_nodes: List[str],
    ) -> dict[str, str]:
        """将订阅文件写入本地仓库根目录，供 git push 同步。返回 {文件名: raw_url}。"""
        repo_root = os.getcwd()
        os.makedirs(repo_root, exist_ok=True)

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
            path = os.path.join(repo_root, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            raw_urls[fname] = (
                f"https://raw.githubusercontent.com/wu242005546-ai/{self.repo}/"
                f"{self.branch}/{fname}"
            )
            logger.info("push: wrote %s (%d bytes)", path, len(body))

        if raw_urls:
            logger.info("push: %d subscription file(s) written, commit via git push", len(raw_urls))
        return raw_urls
