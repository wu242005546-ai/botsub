"""将聚合成品写入本地仓库根目录，由 GitHub Actions 的 git push 自动同步到当前 botsub 仓库。

对外发布的是"终端客户端可直接导入"的成品，不是源 URL 清单：
  sub_v2ray.txt      -> base64 节点订阅（V2RAYN / v2rayNG 直接订阅导入）
  sub_clash.txt      -> 合并后的完整 Clash YAML（Clash Verge / Mihomo 直接订阅导入）
  sub_telegram.txt   -> Telegram 频道裸节点 URI 列表（去重后，直接导入）
  sub_v2ray_sources.txt / sub_clash_sources.txt -> 本轮验证存活的源 URL 清单（供自取）

raw 订阅地址：
  https://raw.githubusercontent.com/wu242005546-ai/botsub/main/sub_clash.txt
  https://raw.githubusercontent.com/wu242005546-ai/botsub/main/sub_v2ray.txt
  https://raw.githubusercontent.com/wu242005546-ai/botsub/main/sub_telegram.txt
"""

from __future__ import annotations

import logging
import os
from typing import Dict

logger = logging.getLogger("push")


class SubscriptionPusher:
    def __init__(self, cfg, session=None):
        self.repo = getattr(cfg, "SUB_REPO_NAME", "") or "botsub"
        self.branch = getattr(cfg, "SUB_REPO_BRANCH", "") or "main"

    def push_subscriptions(self, products: Dict[str, str]) -> dict[str, str]:
        """将成品文件写入本地仓库根目录，供 git push 同步。返回 {文件名: raw_url}。"""
        repo_root = os.getcwd()
        os.makedirs(repo_root, exist_ok=True)

        raw_urls: dict[str, str] = {}
        for fname, body in products.items():
            fname = os.path.basename(fname)
            path = os.path.join(repo_root, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            raw_urls[fname] = (
                f"https://raw.githubusercontent.com/wu242005546-ai/{self.repo}/"
                f"{self.branch}/{fname}"
            )
            logger.info("push: wrote %s (%d bytes)", path, len(body))

        if raw_urls:
            logger.info("push: %d file(s) written, commit via git push", len(raw_urls))
        return raw_urls