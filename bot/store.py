"""SQLite state.db：sources / verification / runs / search_terms / repos_history。

职责：
- 源生命周期状态机：NEW -> ACTIVE <-> STALE -> FAILED -> DEAD（保留身份，可 REVIVED）。
- raw 内容与凭据永不落库（只存 hash / 节点指纹 / 统计）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    url TEXT PRIMARY KEY,
    kind TEXT DEFAULT 'unknown',
    score INTEGER DEFAULT 0,
    content_hash TEXT DEFAULT '',
    node_count INTEGER DEFAULT 0,
    unique_hosts INTEGER DEFAULT 0,
    protocols TEXT DEFAULT '',
    fingerprint TEXT DEFAULT '',
    status TEXT DEFAULT 'ACTIVE',
    failure_count INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    last_ok TEXT,
    last_dead TEXT,
    last_error TEXT,
    provenance TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS verification (
    url TEXT PRIMARY KEY,
    ok INTEGER DEFAULT 0,
    node_count INTEGER DEFAULT 0,
    unique_hosts INTEGER DEFAULT 0,
    protocols TEXT DEFAULT '',
    content_hash TEXT DEFAULT '',
    error TEXT,
    kind TEXT DEFAULT 'unknown',
    checked_at TEXT,
    checked_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_verification_ok ON verification(ok);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER DEFAULT -1,
    summary TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS search_terms (
    term TEXT PRIMARY KEY,
    last_picked TEXT
);
CREATE TABLE IF NOT EXISTS repos_history (
    full_name TEXT PRIMARY KEY,
    provenance TEXT DEFAULT 'search',
    first_seen TEXT,
    last_seen TEXT
);
"""

# 生命周期状态
ACTIVE = "ACTIVE"
STALE = "STALE"
FAILED = "FAILED"
DEAD = "DEAD"


@dataclass
class Lifecycle:
    status: str
    failure_count: int
    first_seen: str
    last_seen: str
    last_ok: str
    last_dead: str
    went_dead_now: bool = False
    is_new: bool = False
    hash_changed: bool = False


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        try:
            with self._lock:
                self.conn.close()
        except Exception:
            pass

    # ---------- runs ----------
    def start_run(self, run_id: str, started_at: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at) VALUES(?,?)", (run_id, started_at)
            )
            self.conn.commit()

    def finish_run(self, run_id: str, exit_code: int, summary: dict) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, exit_code=?, summary=? WHERE run_id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), exit_code,
                 json.dumps(summary, ensure_ascii=False), run_id),
            )
            self.conn.commit()

    def prune_runs(self, keep: int = 7) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM runs WHERE run_id NOT IN "
                "(SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?)", (keep,)
            )
            self.conn.commit()

    # ---------- lifecycle ----------
    def classify(self, url: str, ok: bool, content_hash: str,
                 kind: str = "", node_count: int = 0,
                 protocols: str = "", fingerprint: str = "", score: int = 0,
                 provenance: dict = None, checked_at: str = "") -> Lifecycle:
        """状态机判定。返回新状态。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        provenance = provenance or {}
        with self._lock:
            row = self.conn.execute("SELECT * FROM sources WHERE url=?", (url,)).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO sources(url, kind, score, content_hash, node_count,
                       unique_hosts, protocols, fingerprint, status, failure_count,
                       first_seen, last_seen, last_ok, last_dead, last_error, provenance)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (url, kind, score, content_hash, node_count,
                     (protocols.count(",") + 1) if protocols else 0, protocols, fingerprint,
                     ACTIVE if ok else FAILED, 0 if ok else 1,
                     now, now, now if ok else None, None, None,
                     json.dumps({"provenance": provenance}, ensure_ascii=False)),
                )
                self.conn.commit()
                return Lifecycle(status=ACTIVE if ok else FAILED, failure_count=0 if ok else 1,
                                 first_seen=now, last_seen=now, last_ok=now if ok else "",
                                 last_dead="", is_new=True)

            cur_status = row["status"]
            failure_count = int(row["failure_count"] or 0)
            last_dead = row["last_dead"] or ""
            went_dead = False
            hash_changed = bool(content_hash and row["content_hash"] and content_hash != row["content_hash"])

            if ok:
                failure_count = 0
                status = ACTIVE
                if cur_status in (DEAD, FAILED, STALE):
                    # REVIVED：dead -> active，单独标记
                    self.conn.execute(
                        "UPDATE sources SET status=?, failure_count=?, last_seen=?, last_ok=?,"
                        " last_error=NULL, content_hash=?, kind=?, node_count=?, unique_hosts=?,"
                        " protocols=?, fingerprint=?, score=?, provenance=? WHERE url=?",
                        (status, 0, now, now, content_hash, kind, node_count,
                         (protocols.count(",") + 1) if protocols else 0, protocols,
                         fingerprint, score,
                         json.dumps({"provenance": provenance}, ensure_ascii=False), url),
                    )
                    if cur_status == DEAD:
                        went_dead = True
            else:
                failure_count += 1
                status = ACTIVE if failure_count == 0 else (DEAD if failure_count >= 3 else FAILED)
                if status != cur_status and status == DEAD:
                    last_dead = now
                self.conn.execute(
                    "UPDATE sources SET failure_count=?, status=?, last_error=?, last_seen=? WHERE url=?",
                    (failure_count, status, "verify_failed", now, url),
                )
            self.conn.commit()
            return Lifecycle(status=status, failure_count=failure_count,
                             first_seen=row["first_seen"], last_seen=now,
                             last_ok=row["last_ok"] or (now if ok else row["last_ok"] or ""),
                             last_dead=last_dead, went_dead_now=went_dead,
                             hash_changed=hash_changed)

    def get_source(self, url: str) -> Optional[Dict]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM sources WHERE url=?", (url,)).fetchone()
            return dict(row) if row else None

    def get_history_repos(self, limit: int = 40) -> List[Tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT full_name, last_seen FROM repos_history ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r["full_name"], r["last_seen"]) for r in rows]

    def record_repo(self, full_name: str, provenance: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self.conn.execute(
                """INSERT INTO repos_history(full_name, provenance, first_seen, last_seen)
                   VALUES(?,?,?,?)
                   ON CONFLICT(full_name) DO UPDATE SET last_seen=?,
                   provenance=excluded.provenance""",
                (full_name, provenance, now, now, now),
            )
            self.conn.commit()

    # ---------- verification cache ----------
    def get_verification(self, url: str) -> Optional[Dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM verification WHERE url=?", (url,)
            ).fetchone()
            return dict(row) if row else None

    def record_verification(self, url: str, ok: bool, node_count: int, content_hash: str,
                            checked_at: str, error: str = None, unique_hosts: int = 0,
                            protocols: str = "", kind: str = "unknown") -> None:
        checked_ts = time.time()
        with self._lock:
            self.conn.execute(
                """INSERT INTO verification(url, ok, node_count, unique_hosts, protocols,
                   content_hash, error, kind, checked_at, checked_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET ok=excluded.ok,
                   node_count=excluded.node_count, unique_hosts=excluded.unique_hosts,
                   protocols=excluded.protocols, content_hash=excluded.content_hash,
                   error=excluded.error, kind=excluded.kind,
                   checked_at=excluded.checked_at, checked_ts=excluded.checked_ts""",
                (url, 1 if ok else 0, node_count, unique_hosts, protocols,
                 content_hash, error, kind, checked_at, checked_ts),
            )
            self.conn.commit()

    # ---------- search terms ----------
    def touch_term(self, term: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self.conn.execute(
                "INSERT INTO search_terms(term, last_picked) VALUES(?,?) "
                "ON CONFLICT(term) DO UPDATE SET last_picked=?",
                (term, now, now),
            )
            self.conn.commit()

    # ---------- housekeeping ----------
    def mark_stale(self, stale_days: int = 14) -> int:
        """把很久未见但未死的源标为 STALE。"""
        import datetime

        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=stale_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cur = self.conn.execute(
                "UPDATE sources SET status=? WHERE status=? AND last_seen < ?",
                (STALE, ACTIVE, cutoff),
            )
            self.conn.commit()
            return cur.rowcount

    def prune_old_dead(self, max_age_days: int) -> int:
        """清理超龄 DEAD 记录（身份保留：仅清理日志权重）。"""
        import datetime

        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cur = self.conn.execute("DELETE FROM sources WHERE status=? AND last_seen < ?",
                                    (DEAD, cutoff))
            self.conn.commit()
            return cur.rowcount

    def stats(self) -> Dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) c FROM sources GROUP BY status"
            ).fetchall()
            return {r["status"]: r["c"] for r in rows}