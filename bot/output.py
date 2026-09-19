"""输出现场：current/（全量当前状态）+ latest/（报告、manifest、diff）。原子写。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger("output")


def _atomic_write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


@dataclass
class RunReport:
    run_id: str
    generated_at: str
    exit_code: int
    repos_scanned: int
    candidates_found: int
    verified_total: int
    ok_total: int
    failed_total: int
    new_sources: int
    changed_sources: int
    dead_sources: int
    revived_sources: int
    source_stats: dict
    new_ids: list
    changed_ids: list
    dead_ids: list
    revived_ids: list
    links: List[str]  # 汇总后的有效链接
    manifest: dict


class OutputWriter:
    def __init__(self, output_dir: str):
        self.root = output_dir
        self.current = os.path.join(output_dir, "current")
        self.latest = os.path.join(output_dir, "latest")
        os.makedirs(self.current, exist_ok=True)
        os.makedirs(self.latest, exist_ok=True)

    def write_current(self, clash_urls: List[str], v2ray_urls: List[str],
                      protocol_urls: List[str]) -> Dict[str, str]:
        """current/：全量快照。返回 文件名 -> sha256。"""
        artifacts = {
            "sub_clash_current.txt": "\n".join(dict.fromkeys(clash_urls)) + ("\n" if clash_urls else ""),
            "sub_v2ray_current.txt": "\n".join(dict.fromkeys(v2ray_urls)) + ("\n" if v2ray_urls else ""),
            "sub_nodes_current.txt": "\n".join(dict.fromkeys(protocol_urls)) + ("\n" if protocol_urls else ""),
        }
        manifest = {}
        for name, body in artifacts.items():
            p = os.path.join(self.current, name)
            _atomic_write(p, body)
            manifest[name] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        return manifest

    def write_telegram_nodes(self, raw_nodes: List[str]) -> str:
        """output/current/sub_telegram_nodes_current.txt：Telegram频道里直接贴出的裸节点链接。

        这些链接只做过语法校验（协议格式对不对），没有像其它输出那样被重新拉取验活
        ——因为它们本身就是终点数据，没有"外部资源"可供二次请求。当作普通节点列表
        订阅源接入 subs-check 之类的下游工具时，存活与否由下游自己的测活环节负责。
        返回内容的 sha256（用于 manifest）。
        """
        body = "\n".join(dict.fromkeys(raw_nodes)) + ("\n" if raw_nodes else "")
        p = os.path.join(self.current, "sub_telegram_nodes_current.txt")
        _atomic_write(p, body)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def write_latest(self, report: RunReport) -> None:
        report_path = os.path.join(self.latest, "report.json")
        _atomic_write(report_path, json.dumps(
            {
                "run_id": report.run_id,
                "generated_at": report.generated_at,
                "exit_code": report.exit_code,
                "repos_scanned": report.repos_scanned,
                "candidates_found": report.candidates_found,
                "verified_total": report.verified_total,
                "ok_total": report.ok_total,
                "failed_total": report.failed_total,
                "new_sources": report.new_sources,
                "changed_sources": report.changed_sources,
                "dead_sources": report.dead_sources,
                "revived_sources": report.revived_sources,
                "source_stats": report.source_stats,
                "links": report.links,
            }, ensure_ascii=False, indent=2,
        ))
        manifest_path = os.path.join(self.latest, "manifest.json")
        _atomic_write(manifest_path, json.dumps(report.manifest, indent=2))
        diff = {
            "run_id": report.run_id,
            "generated_at": report.generated_at,
            "new_sources": report.new_ids,
            "changed_sources": report.changed_ids,
            "dead_sources": report.dead_ids,
            "revived_sources": report.revived_ids,
        }
        diff_path = os.path.join(self.latest, "diff.json")
        _atomic_write(diff_path, json.dumps(diff, ensure_ascii=False, indent=2))

    def write_merge_stats(self, stats: dict) -> None:
        """调试用：每次 run 的聚合成品统计（合并前/后节点数、截断、协议分布）。"""
        p = os.path.join(self.latest, "merge.json")
        _atomic_write(p, json.dumps(stats, ensure_ascii=False, indent=2))

    def write_discovery_snapshot(self, facts: dict) -> None:
        """调试用：每次 run 抓一次现场（可解释 diff 的来历）。"""
        p = os.path.join(self.latest, "discovery_snapshot.json")
        _atomic_write(p, json.dumps(facts, ensure_ascii=False, indent=2))

    def summary_txt(self, report: RunReport) -> str:
        lines = [
            f"run_id: {report.run_id}",
            f"generated_at: {report.generated_at}",
            f"exit_code: {report.exit_code}",
            f"repos_scanned: {report.repos_scanned}",
            f"candidates_found: {report.candidates_found}",
            f"verified: {report.ok_total}/{report.verified_total} ok",
            f"new: {report.new_sources}, changed: {report.changed_sources}, "
            f"dead: {report.dead_sources}, revived: {report.revived_sources}",
            f"sources by status: {report.source_stats}",
        ]
        return "\n".join(lines) + "\n"