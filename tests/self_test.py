"""离线自检：纯函数解析 + 打分，不联网。运行：python tests/self_test.py"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_base64_chain():
    from bot.parse.base64_chain import decode_chain, is_likely_b64, decode_b64
    assert is_likely_b64("aGVsbG8=")
    assert not is_likely_b64("hello world")
    assert decode_b64("aGVsbG8=") == "hello"
    # 双层 base64
    inner = "aGVsbG8="
    import base64
    outer = base64.b64encode(inner.encode()).decode()
    assert decode_chain(outer) == "hello"


def test_node_list_parse():
    from bot.parse.node_list import parse_nodes
    txt = "vmess://" + _b64url(
        '{"v":"2","ps":"demo","add":"1.2.3.4","port":"443","id":"abc","net":"tcp"}'
    ) + "\ntrojan://pw@5.6.7.8:8443?security=tls\n"
    nodes, urls = parse_nodes(txt)
    assert len(nodes) == 2, f"expected 2 nodes, got {len(nodes)}"
    vmess = [n for n in nodes if n.protocol == "vmess"][0]
    trojan = [n for n in nodes if n.protocol == "trojan"][0]
    assert vmess.host == "1.2.3.4" and vmess.port == 443
    assert trojan.host == "5.6.7.8" and trojan.port == 8443
    assert len(vmess.fingerprint()) == 16


def _b64url(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode()


def test_clash_yaml():
    from bot.parse.clash_yaml import parse_clash_yaml, looks_like_clash
    yaml = """proxies:
  - name: n1
    type: ss
    server: server.example.com
    port: 8388
    cipher: aes-256-gcm
    password: secret
proxy-providers:
  p1:
    type: http
    url: https://example.com/sub.txt
"""
    assert looks_like_clash(yaml)
    nodes, urls = parse_clash_yaml(yaml)
    assert len(nodes) == 1
    assert urls == ["https://example.com/sub.txt"]


def test_analyze():
    from bot.analyze import score_extraction, score_for_link
    from bot.domain import ExtractionResult, SourceIdentity
    from bot.parse.node_list import parse_nodes

    def _vmess(host: str) -> str:
        return "vmess://" + _b64url('{"v":"2","add":"%s","port":"443","id":"x","ps":"s"}' % host)

    txt = "\n".join(_vmess(f"{i}.example.com") for i in range(1, 6))
    nodes, urls = parse_nodes(txt)
    ex = ExtractionResult(target=SourceIdentity("x", "https://raw.githubusercontent.com/a/b/main/x.clash"),
                          nodes=nodes, parser="node_list")
    ar = score_extraction(ex)
    assert ar.score > 30, ar
    assert ar.kind in ("v2ray", "clash")
    link_ar = score_for_link("https://raw.githubusercontent.com/a/b/main/clash.txt")
    assert link_ar.score > 0


def test_fingerprint_deterministic():
    from bot.domain import Node
    a = Node("ss", "host.example", 443, {"password": "secret"})
    b = Node("ss", "host.example", 443, {"password": "secret"})
    assert a.fingerprint() == b.fingerprint()
    c = Node("ss", "host.example", 443, {"password": "other"})
    assert a.fingerprint() != c.fingerprint()


def test_store_lifecycle():
    import tempfile
    from bot.store import Store
    d = tempfile.mkdtemp()
    s = Store(os.path.join(d, "t.db"))
    lc1 = s.classify("https://x/y", ok=False, content_hash="h1")
    assert lc1.status == "FAILED" and lc1.failure_count == 1
    lc2 = s.classify("https://x/y", ok=False, content_hash="h1")
    assert lc2.status == "FAILED" and lc2.failure_count == 2
    lc3 = s.classify("https://x/y", ok=False, content_hash="h1")
    assert lc3.status == "DEAD" and lc3.failure_count == 3
    lc4 = s.classify("https://x/y", ok=True, content_hash="h2")
    assert lc4.status == "ACTIVE" and lc4.went_dead_now
    # 新源 OK -> ACTIVE is_new
    lc5 = s.classify("https://z", ok=True, content_hash="h3")
    assert lc5.is_new and lc5.status == "ACTIVE"
    s.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")