"""Telegram 公开频道 Provider。

不需要 Bot Token / API ID：t.me/s/<channel> 是 Telegram 官方提供的
公开频道预览页，匿名 HTTP GET 即可访问（专为搜索引擎/嵌入预览设计）。
只能看公开频道，看不了私有群组/频道——那类需要 Bot Token 或用户会话，
本实现不涉及。

频道消息和"仓库文件"的形状不一样，接入方式也刻意分成两条腿，
不硬塞进"仓库扫描"那条统一管道：

1. 消息里贴的是外部订阅链接（https://...sub）
   -> 这类本来就是"指向内容的指针"，和仓库文件里扫出来的内嵌链接
      （main.py 的 discovered_links）完全同构，直接汇入同一个池子，
      复用现成的打分 -> 验活 -> 输出流程，不另开小灶。

2. 消息里直接贴的是裸节点链接（vmess:// / ss:// / ...）
   -> 这类内容本身就是最终数据，没有"外部资源"可供重新拉取验活，
      塞进现有 verify.py（它的验活逻辑是"重新 HTTP GET canonical URL"）
      会必然失败。所以这类节点只做静态语法校验（复用 node_list.py
      现成的逐协议解析函数），校验通过就原样收进
      output/current/sub_telegram_nodes_current.txt，不冒充"已验活"。
"""

from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass, field
from typing import List, Tuple

import aiohttp

from config import Config
from bot.parse.node_list import (
    parse_ss_url, parse_vmess_url, parse_vless_trojan_url, parse_ssr_url,
)

logger = logging.getLogger("telegram")

_MSG_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_PROTO_RE = re.compile(r"(vmess|vless|trojan|ss|ssr|hysteria2)://([^\s\"'<>]+)", re.I)

_VALIDATORS = {
    "vmess": parse_vmess_url,
    "vless": parse_vless_trojan_url,
    "trojan": parse_vless_trojan_url,
    "ss": parse_ss_url,
    "ssr": parse_ssr_url,
}


def _strip_html(fragment: str) -> str:
    text = _BR_RE.sub("\n", fragment)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text)


@dataclass
class TelegramScanResult:
    channel: str
    raw_nodes: List[str] = field(default_factory=list)       # 校验通过的裸节点原始链接（未去重）
    sub_links: List[str] = field(default_factory=list)       # 消息里贴的外部订阅链接（未去重）
    messages_scanned: int = 0


def _extract_from_text(text: str) -> Tuple[List[str], List[str]]:
    """从一段消息纯文本里，分别取出「校验通过的裸节点链接」和「外部订阅链接」。"""
    raw_nodes: List[str] = []
    for m in _PROTO_RE.finditer(text):
        proto = m.group(1).lower()
        uri = m.group(0)
        validator = _VALIDATORS.get(proto)
        # hysteria2 目前 node_list.py 里没有独立校验函数，原样放行，交给下游 subs-check 自己测活
        if validator is None or validator(uri) is not None:
            raw_nodes.append(uri)

    sub_links: List[str] = []
    for m in re.finditer(r"https?://[^\s\"'<>]+", text):
        u = m.group(0).rstrip(",.;))]}")
        if not re.search(r"(vmess|vless|trojan|ss|ssr|hysteria2)://", u):
            sub_links.append(u)
    return raw_nodes, sub_links


async def _fetch_channel_html(session: aiohttp.ClientSession, channel: str, timeout: int) -> str:
    channel = channel.strip().lstrip("@")
    if not channel:
        return ""
    url = f"https://t.me/s/{channel}"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "Mozilla/5.0 (sub-bot-v2 telegram-provider)"},
        ) as resp:
            if resp.status != 200:
                logger.warning("telegram channel=%s -> HTTP %s", channel, resp.status)
                return ""
            return await resp.text(errors="replace")
    except Exception as e:
        logger.warning("telegram channel=%s fetch error: %s", channel, e)
        return ""


async def scan_channel(session: aiohttp.ClientSession, channel: str, timeout: int) -> TelegramScanResult:
    """抓取单个公开频道预览页，拆出裸节点链接与外部订阅链接。失败返回空结果，不抛异常。"""
    channel_clean = channel.strip().lstrip("@")
    result = TelegramScanResult(channel=channel_clean)
    html_body = await _fetch_channel_html(session, channel, timeout)
    if not html_body:
        return result
    for m in _MSG_TEXT_RE.finditer(html_body):
        text = _strip_html(m.group(1)).strip()
        if not text:
            continue
        result.messages_scanned += 1
        nodes, links = _extract_from_text(text)
        result.raw_nodes.extend(nodes)
        result.sub_links.extend(links)
    logger.info(
        "telegram channel=%s messages=%d raw_nodes=%d sub_links=%d",
        channel_clean, result.messages_scanned, len(result.raw_nodes), len(result.sub_links),
    )
    return result


async def scan_all_channels(cfg: Config, session: aiohttp.ClientSession) -> List[TelegramScanResult]:
    """按配置扫描所有启用频道。TELEGRAM_CHANNELS 为空则直接返回 []，零开销、零请求。"""
    channels: List[str] = getattr(cfg, "TELEGRAM_CHANNELS", []) or []
    if not channels:
        return []
    out: List[TelegramScanResult] = []
    for ch in channels:
        out.append(await scan_channel(session, ch, cfg.REQUEST_TIMEOUT))
    return out
