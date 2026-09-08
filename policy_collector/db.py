"""数据落地层：SQLite 五类记录。

表结构（与任务口径一致）：
    source_configs  来源配置（站点/栏目/规则/最近检查时间）
    fetch_records   采集记录（页面URL/原始文件位置/状态/错误/哈希）
    policies        政策及版本（标题/文号/机关/日期/分类/审核状态/判断理由）
    attachments     附件记录（所属政策/URL/保存位置/校验值/解析状态）
    run_logs        运行与审核记录（批次/模型版本/人工纠正/耗时）

数据库访问全部经本层集中封装，便于后续换 PostgreSQL 等目标库。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_configs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    site          TEXT DEFAULT '',
    region        TEXT DEFAULT '',
    category      TEXT DEFAULT '',
    enabled       INTEGER DEFAULT 1,
    list_url      TEXT DEFAULT '',
    link_selector TEXT DEFAULT '',
    include       TEXT DEFAULT '[]',      -- JSON list
    exclude       TEXT DEFAULT '[]',      -- JSON list
    max_pages     INTEGER DEFAULT 3,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER,
    page_url      TEXT,
    title         TEXT DEFAULT '',
    status        TEXT DEFAULT 'discovered', -- discovered/downloaded/failed/excluded/processed
    error         TEXT DEFAULT '',
    content_sha256 TEXT DEFAULT '',
    raw_path      TEXT DEFAULT '',
    discovered_at TEXT,
    downloaded_at TEXT,
    processed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_url ON fetch_records(page_url);
CREATE INDEX IF NOT EXISTS idx_fetch_src ON fetch_records(source_id);

CREATE TABLE IF NOT EXISTS policies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_key    TEXT NOT NULL,           -- 归一化标识（文号或标题指纹）
    version       INTEGER DEFAULT 1,
    title         TEXT DEFAULT '',
    wenhao        TEXT DEFAULT '',
    issuing_authority TEXT DEFAULT '',
    page_date     TEXT DEFAULT '',
    doc_date      TEXT DEFAULT '',
    region        TEXT DEFAULT '',
    site          TEXT DEFAULT '',
    page_url      TEXT DEFAULT '',
    doc_type      TEXT DEFAULT '其他',
    category      TEXT DEFAULT '',          -- guide/access/guarantee/incentive（,分隔）
    category_names TEXT DEFAULT '',
    is_investment_policy TEXT DEFAULT 'pending',
    need_review   INTEGER DEFAULT 0,
    reason        TEXT DEFAULT '',
    evidence      TEXT DEFAULT '',
    confidence    REAL,
    model_version TEXT DEFAULT '',
    review_status TEXT DEFAULT 'pending',   -- pending/confirmed/adjusted/rejected
    content       TEXT DEFAULT '',
    content_sha256 TEXT DEFAULT '',
    source_fetch_id INTEGER,
    created_at    TEXT,
    updated_at    TEXT,
    UNIQUE(policy_key, version)
);
CREATE INDEX IF NOT EXISTS idx_policy_key ON policies(policy_key);

CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id     INTEGER NOT NULL,
    name          TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    local_path    TEXT DEFAULT '',
    fmt           TEXT DEFAULT '',
    sha256        TEXT DEFAULT '',
    parse_status  TEXT DEFAULT 'not_parsed',
    parsed_text   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_att_policy ON attachments(policy_id);

CREATE TABLE IF NOT EXISTS run_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    source_id     INTEGER,
    kind          TEXT DEFAULT 'manual',    -- manual/scheduled
    status        TEXT DEFAULT 'ok',        -- ok/partial/failed
    summary       TEXT DEFAULT '{}',        -- JSON: discovered/downloaded/ingested/dup/failed/review
    model_version TEXT DEFAULT '',
    started_at    TEXT,
    finished_at   TEXT,
    note          TEXT DEFAULT ''           -- 人工纠正等记录
);
CREATE INDEX IF NOT EXISTS idx_run_id ON run_logs(run_id);
"""


class Database:
    """SQLite 封装：连接管理 + 基础 DAO。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ---------- 来源 ----------
    def upsert_source(self, name: str, **fields: Any) -> int:
        with self.tx() as cur:
            cur.execute("SELECT id FROM source_configs WHERE name=?", (name,))
            row = cur.fetchone()
            if row:
                sets = ", ".join(f"{k}=?" for k in fields)
                cur.execute(f"UPDATE source_configs SET {sets} WHERE id=?", (*fields.values(), row["id"]))
                return int(row["id"])
            cols = ",".join(["name"] + list(fields.keys()))
            marks = ",".join(["?"] * (len(fields) + 1))
            cur.execute(f"INSERT INTO source_configs ({cols}) VALUES ({marks})", (name, *fields.values()))
            return int(cur.lastrowid)

    def list_sources(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM source_configs"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._conn:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY id")]

    def get_source(self, name: str) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM source_configs WHERE name=?", (name,)).fetchone()
            return dict(r) if r else None

    def get_source_by_id(self, sid: int) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM source_configs WHERE id=?", (sid,)).fetchone()
            return dict(r) if r else None

    def touch_source(self, sid: int) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE source_configs SET last_checked_at=? WHERE id=?", (now(), sid))

    # ---------- 采集记录 ----------
    def add_fetch(self, source_id: int, page_url: str, status: str = "discovered",
                  title: str = "", error: str = "", sha256: str = "", raw_path: str = "") -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO fetch_records(source_id,page_url,status,title,error,content_sha256,raw_path,discovered_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (source_id, page_url, status, title, error, sha256, raw_path, now()),
            )
            return int(cur.lastrowid)

    def update_fetch(self, fetch_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(f"UPDATE fetch_records SET {sets} WHERE id=?", (*fields.values(), fetch_id))

    def list_fetch(self, source_id: Optional[int] = None, status: Optional[str] = None,
                   limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM fetch_records WHERE 1=1", []
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn:
            return [dict(r) for r in self._conn.execute(sql, args)]

    # ---------- 政策及版本 ----------
    def find_policy(self, policy_key: str) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute(
                "SELECT * FROM policies WHERE policy_key=? ORDER BY version DESC LIMIT 1", (policy_key,)
            ).fetchone()
            return dict(r) if r else None

    def insert_policy(self, p: dict) -> int:
        cols = list(p.keys())
        marks = ",".join(["?"] * len(cols))
        with self.tx() as cur:
            cur.execute(
                f"INSERT INTO policies ({','.join(cols)}) VALUES ({marks})", [p[c] for c in cols]
            )
            return int(cur.lastrowid)

    def add_policy_version(self, policy_key: str, data: dict) -> tuple[int, int]:
        """同一 policy_key 已有记录则 version+1 追加新版本，返回 (policy_id, version)。"""
        prev = self.find_policy(policy_key)
        version = (prev["version"] + 1) if prev else 1
        data["policy_key"] = policy_key
        data["version"] = version
        data["created_at"] = now()
        data["updated_at"] = now()
        pid = self.insert_policy(data)
        return pid, version

    def update_policy(self, pid: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as cur:
            cur.execute(f"UPDATE policies SET {sets} WHERE id=?", (*fields.values(), pid))

    def get_policy(self, pid: int) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM policies WHERE id=?", (pid,)).fetchone()
            return dict(r) if r else None

    def query_policies(self, region: str = "", category: str = "", keyword: str = "",
                       review_status: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM policies WHERE 1=1", []
        if region:
            sql += " AND region=?"
            args.append(region)
        if category:
            sql += " AND (category LIKE ? OR category_names LIKE ?)"
            args += [f"%{category}%", f"%{category}%"]
        if keyword:
            sql += " AND (title LIKE ? OR content LIKE ? OR wenhao LIKE ?)"
            args += [f"%{keyword}%"] * 3
        if review_status:
            sql += " AND review_status=?"
            args.append(review_status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn:
            return [dict(r) for r in self._conn.execute(sql, args)]

    # ---------- 附件 ----------
    def add_attachment(self, policy_id: int, att: dict) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO attachments(policy_id,name,url,local_path,fmt,sha256,parse_status,parsed_text)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (policy_id, att.get("name", ""), att.get("url", ""), att.get("local_path", ""),
                 att.get("fmt", ""), att.get("sha256", ""), att.get("parse_status", "not_parsed"),
                 att.get("parsed_text", "")),
            )
            return int(cur.lastrowid)

    def list_attachments(self, policy_id: int) -> list[dict]:
        with self._conn:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM attachments WHERE policy_id=? ORDER BY id", (policy_id,))]

    # ---------- 运行日志 ----------
    def start_run(self, run_id: str, source_id: Optional[int], kind: str = "manual") -> int:
        with self.tx() as cur:
            cur.execute("INSERT INTO run_logs(run_id,source_id,kind,status,started_at,summary)"
                        " VALUES(?,?,?,?,?,?)", (run_id, source_id, kind, "running", now(), "{}"))
            return int(cur.lastrowid)

    def finish_run(self, run_id: str, summary: dict, status: str = "ok",
                   model_version: str = "", note: str = "") -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE run_logs SET status=?,summary=?,finished_at=?,model_version=?,note=? WHERE run_id=?",
                (status, json.dumps(summary, ensure_ascii=False), now(), model_version, note, run_id),
            )

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn:
            return [dict(r) for r in self._conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,))]

    def get_run_summary(self, run_id: str) -> Optional[dict]:
        with self._conn:
            r = self._conn.execute("SELECT * FROM run_logs WHERE run_id=?", (run_id,)).fetchone()
            return dict(r) if r else None

    # ---------- 仪表盘统计 ----------
    def dashboard(self) -> dict:
        with self._conn:
            def one(sql: str, *a: Any) -> int:
                return int(self._conn.execute(sql, a).fetchone()[0])

            return {
                "sources": one("SELECT COUNT(*) FROM source_configs"),
                "sources_enabled": one("SELECT COUNT(*) FROM source_configs WHERE enabled=1"),
                "policies": one("SELECT COUNT(*) FROM policies"),
                "pending_review": one("SELECT COUNT(*) FROM policies WHERE need_review=1"),
                "confirmed": one("SELECT COUNT(*) FROM policies WHERE review_status IN ('confirmed','adjusted','confirmed_auto')"),
                "rejected": one("SELECT COUNT(*) FROM policies WHERE review_status='rejected'"),
                "fetches": one("SELECT COUNT(*) FROM fetch_records"),
                "runs": one("SELECT COUNT(*) FROM run_logs"),
                "attachments": one("SELECT COUNT(*) FROM attachments"),
            }
