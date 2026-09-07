"""SUB_BOT V2 流水线入口。

退出码：
  0 = 成功（全部完成，可能部分验证失败但流程走完）
  1 = 部分失败（有验证失败，仍产出；邮件照发并带失败清单）
  2 = 致命（初始化/发现阶段失败，无有效产出）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Dict, List, Tuple

import aiohttp

from config import Config
from bot.analyze import score_extraction, score_for_link
from bot.discovery import build_providers
from bot.domain import (
    AnalysisResult, ExtractionResult, Node, RepoInfo, SourceIdentity, VerificationResult,
)
from bot.fetcher import Fetcher
from bot.foreign import ForeignClient, gitlab_raw_url, gitee_raw_url
from bot.gh import GitHubClient
from bot.notify import Notifier
from bot.output import OutputWriter, RunReport
from bot.parse.base64_chain import decode_chain, is_likely_b64
from bot.parse.clash_yaml import parse_clash_yaml, looks_like_clash
from bot.parse.node_list import parse_nodes
from bot.store import Store
from bot.tree_scan import TreeScanner
from bot.verify import Verifier

logger = logging.getLogger("subbot")


def make_run_id() -> str:
    from uuid import uuid4

    return (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:4])


def log_setup(level: int = logging.INFO, output_dir: str = "output"):
    os.makedirs(output_dir, exist_ok=True)
    handlers = [logging.StreamHandler()]
    try:
        fh = logging.FileHandler(os.path.join(output_dir, "run.log"), encoding="utf-8")
        handlers.append(fh)
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def raw_url_for(repo: RepoInfo, path: str) -> str:
    host = getattr(repo, "host", "github")
    if host == "gitlab":
        return gitlab_raw_url(repo.full_name, repo.branch, path)
    if host == "gitee":
        return gitee_raw_url(repo.full_name, repo.branch, path)
    return f"https://raw.githubusercontent.com/{repo.full_name}/{repo.branch}/{path}"


def parse_and_extract(url: str, data: bytes, repo_note: str = "") -> ExtractionResult:
    """纯解析：任意字节 -> ExtractionResult。"""
    identity = SourceIdentity(repo_note if repo_note else url, url)
    if not data:
        return ExtractionResult(target=identity)
    text = data.decode("utf-8", errors="replace")
    errors: List[str] = []
    nodes: List[Node] = []
    raw_urls: List[str] = []

    # base64 链
    if is_likely_b64(text.strip()) and len(text) < 2_000_000:
        text = decode_chain(text)
    # clash yaml
    if looks_like_clash(text):
        nodes, raw_urls = parse_clash_yaml(text)
        parser = "clash"
    else:
        nodes, raw_urls = parse_nodes(text)
        parser = "node_list"
    return ExtractionResult(target=identity, nodes=nodes, raw_urls=raw_urls, parser=parser, errors=errors)


def group_urls(results: List[Tuple[VerificationResult, str]]) -> Tuple[List[str], List[str], List[str]]:
    clash, v2ray, proto = [], [], []
    for res, kind in results:
        if not res.ok:
            continue
        u = res.target.canonical
        if kind == "clash":
            clash.append(u)
        elif kind == "v2ray":
            v2ray.append(u)
        else:
            proto.append(u)
    return clash, v2ray, proto


async def run_pipeline(cfg: Config, args) -> int:
    run_id = make_run_id()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    store = Store(cfg.STATE_DB)
    store.start_run(run_id, started)
    writer = OutputWriter(cfg.OUTPUT_DIR)

    try:
        timeout = aiohttp.ClientTimeout(total=cfg.REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            gh = GitHubClient(cfg, session)
            foreign = ForeignClient(cfg, session)
            fetcher = Fetcher(cfg, session)
            verifier = Verifier(cfg, fetcher, store)
            scanner = TreeScanner(cfg, gh, foreign=foreign)

            # ---------- 1. discovery ----------
            providers = build_providers(cfg, gh, store, foreign)
            seen: set = set()
            repos: List[RepoInfo] = []
            for prov in providers:
                chunk = await prov.discover(seen, cfg.MAX_REPOS_TOTAL - len(repos))
                repos.extend(chunk)
                if len(repos) >= cfg.MAX_REPOS_TOTAL:
                    break
            for r in repos:
                store.record_repo(r.full_name, r.provenance)
            logger.info("discovered %d repos", len(repos))

            # ---------- 2. scan tree -> candidate files ----------
            candidates: List[str] = []  # url
            provenance_map: Dict[str, RepoInfo] = {}
            for repo in repos:
                outcome = await scanner.scan(repo.full_name, repo.branch)
                top = scanner.top_files(outcome.entries, cfg.MAX_FILES_PER_REPO)
                for path in top:
                    url = raw_url_for(repo, path)
                    if url in candidates:
                        continue
                    candidates.append(url)
                    provenance_map[url] = repo
                if len(candidates) >= cfg.MAX_CANDIDATE_FILES_TOTAL:
                    break
            logger.info("candidate files: %d", len(candidates))

            # ---------- 3. fetch + extract ----------
            # 统一的单次获取入口（Fetcher memo 保证同 URL 一次）
            async def fetch_and_extract(url: str, repo: RepoInfo) -> Tuple[bytes, ExtractionResult]:
                res = await fetcher.fetch(url, use_cache=True)
                if res.status != 200 or not res.data:
                    return res.data, ExtractionResult(
                        target=SourceIdentity(repo.full_name + "/" + url.split("/")[-1].split("?")[0], url),
                        errors=[res.error or f"HTTP {res.status}"],
                    )
                return res.data, parse_and_extract(url, res.data, repo_note=repo.full_name)

            extraction_cache: Dict[str, ExtractionResult] = {}
            sem = asyncio.Semaphore(cfg.MAX_CONCURRENT_VERIFY)

            async def extract_one(url: str, repo: RepoInfo):
                async with sem:
                    try:
                        _data, ex = await fetch_and_extract(url, repo)
                    except Exception as e:
                        ex = ExtractionResult(target=SourceIdentity(repo.full_name, url), errors=[str(e)])
                    extraction_cache[url] = ex
                    return ex

            results: List[ExtractionResult] = await asyncio.gather(
                *(extract_one(u, provenance_map[u]) for u in candidates)
            )
            results = [r for r in results if r is not None]

            # 提取出的内嵌订阅链接 -> 补进候选池
            discovered_links: List[str] = []
            for ex in results:
                for link in ex.raw_urls:
                    link = SourceIdentity.normalize(link)
                    if link and link not in discovered_links and link not in candidates:
                        discovered_links.append(link)
            logger.info("extracted %d files, found %d embedded links",
                        len(results), len(discovered_links))

            # ---------- 4. analyze / score ----------
            analyzed: Dict[str, AnalysisResult] = {}
            for url, ex in extraction_cache.items():
                analyzed[url] = score_extraction(ex)
                analyzed[url].target = SourceIdentity(ex.target.canonical, ex.target.canonical)
            for link in discovered_links:
                analyzed[link] = score_for_link(link)

            ranked = sorted(
                (a for a in analyzed.values() if a.score > 0),
                key=lambda a: -a.score,
            )
            logger.info("analyzed %d candidates", len(ranked))

            # ---------- 5. verify ----------
            verify_targets: List[SourceIdentity] = []
            for a in ranked:
                if a.score < cfg.MIN_SCORE_TO_VERIFY:
                    continue
                if not cfg.VERIFY_SUBSCRIPTIONS:
                    break
                verify_targets.append(a.target)
                if len(verify_targets) >= cfg.MAX_VERIFY_CANDIDATES:
                    break
            logger.info("verifying %d candidates", len(verify_targets))

            vres: List[VerificationResult] = []
            if verify_targets:
                vres = await verifier.verify_many(verify_targets)
                vres = [r for r in vres if r is not None]

            # ---------- 6. classify lifecycle ----------
            new_ids: List[str] = []
            changed_ids: List[str] = []
            dead_ids: List[str] = []
            revived_ids: List[str] = []

            # 对 OK 的验证结果：分类 & 生成最终归属 kind
            ok_source_kind: Dict[str, str] = {}
            for res in vres:
                # 优先用真实验证出的 kind（clash 检测），否则回退到分析时的 kind
                a = analyzed.get(res.target.canonical)
                kind = res.kind if res.kind != "unknown" else (a.kind if a else "v2ray")
                ok_source_kind[res.target.canonical] = kind
                # 生命周期 state
                lc = store.classify(
                    url=res.target.canonical, ok=res.ok, content_hash=res.content_hash,
                    kind=ok_source_kind.get(res.target.canonical, "v2ray"),
                    node_count=res.node_count,
                    protocols=",".join(res.unique_protocols),
                    score=a.score if a else 0,
                    provenance={"source": "pipeline"},
                )
                if res.ok:
                    if lc.went_dead_now:
                        revived_ids.append(res.target.canonical)
                    elif lc.is_new:
                        new_ids.append(res.target.canonical)
                    elif lc.hash_changed:
                        changed_ids.append(res.target.canonical)
                else:
                    if lc.status == "DEAD":
                        dead_ids.append(res.target.canonical)
                    # 首次失败：进入 FAILED；未到 DEAD 不算"本次新死"

            ok_results: List[Tuple[VerificationResult, str]] = [
                (res, ok_source_kind.get(res.target.canonical, "v2ray"))
                for res in vres if res.ok
            ]

            # ---------- 7. output ----------
            clash_urls, v2ray_urls, proto_urls = group_urls(ok_results)
            manifest = writer.write_current(clash_urls, v2ray_urls, proto_urls)

            all_fail = [r.target.canonical for r in vres if not r.ok]
            failed_total = len(all_fail)
            stats = store.stats()

            summary = {
                "repos_scanned": len(repos),
                "candidates_found": len(ranked),
                "verified_total": len(vres),
                "ok_total": len(ok_results),
                "failed_total": failed_total,
                "new": len(new_ids),
                "changed": len(changed_ids),
                "dead": len(dead_ids),
                "revived": len(revived_ids),
            }
            exit_code = 1 if failed_total > 0 else 0

            report = RunReport(
                run_id=run_id, generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                exit_code=exit_code, repos_scanned=len(repos), candidates_found=len(ranked),
                verified_total=len(vres), ok_total=len(ok_results), failed_total=failed_total,
                new_sources=len(new_ids), changed_sources=len(changed_ids),
                dead_sources=len(dead_ids), revived_sources=len(revived_ids),
                new_ids=new_ids, changed_ids=changed_ids,
                dead_ids=dead_ids, revived_ids=revived_ids,
                source_stats=stats,
                links=clash_urls + v2ray_urls + proto_urls,
                manifest=manifest,
            )
            writer.write_latest(report)
            writer.write_discovery_snapshot({
                "run_id": run_id, "repos": [r.full_name for r in repos],
                "candidates": len(candidates), "verified": len(vres),
            })
            logger.info("pipeline summary: %s", summary)

            # ---------- 8. notify ----------
            if not args.no_email:
                notifier = Notifier(cfg)
                prefix = "SUB_BOT_V2" + (f" [{exit_code}]" if exit_code else "")
                subject = f"{prefix} run={run_id}: +{len(new_ids)} new / -{len(all_fail)} failed"
                lines = writer.summary_txt(report)
                lines += "\n" + "=" * 40 + "\n"
                if new_ids:
                    lines += f"\nNEW ({len(new_ids)}):\n" + "\n".join(new_ids)
                if all_fail:
                    lines += f"\nFAILED ({len(all_fail)}):\n" + "\n".join(all_fail[:40])
                if not new_ids and not all_fail:
                    lines += "\n(no new sources this run)"
                lines += "\n\nAll links: " + "\n".join(clash_urls + v2ray_urls)
                notifier.send(subject, notifier.build_summary(subject, lines.split("\n")))

            store.finish_run(run_id, exit_code, summary)
            store.mark_stale(stale_days=14)
            store.prune_old_dead(cfg.STATE_MAX_AGE_DAYS)
            store.prune_runs(cfg.KEEP_HISTORY_RUNS)
            store.close()
            return exit_code

    except KeyboardInterrupt:
        store.close()
        return 2
    except Exception as e:
        logger.exception("fatal pipeline error: %s", e)
        store.finish_run(run_id, 2, {"fatal": str(e)})
        store.close()
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="SUB_BOT V2 每日订阅扫描")
    parser.add_argument("--keywords", nargs="*", default=None, help="覆盖搜索关键词")
    parser.add_argument("--debug-repos", nargs="*", default=None, help="调试仓库白名单")
    parser.add_argument("--no-email", action="store_true", help="跳过邮件")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    args = parser.parse_args()

    cfg = Config.load()
    if args.keywords:
        cfg.SEARCH_KEYWORDS = args.keywords
    if args.debug_repos:
        cfg.DEBUG_REPOSITORIES = args.debug_repos
    log_setup(logging.DEBUG if args.debug else logging.INFO, cfg.OUTPUT_DIR)
    return asyncio.run(run_pipeline(cfg, args))


if __name__ == "__main__":
    sys.exit(main())