"""翻译缓存。

同一段英文 + 同一个模型 + 同一版术语库 → 直接复用译文，不再调模型。
演示时最怕"点第二次又等五分钟"，这一层就是为它准备的。

键里必须包含术语库版本：改了术语译法，旧译文就不该再被复用。
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_cache (
    key        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    meta       TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_time ON tr_cache(created_at);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def terms_version() -> str:
    """术语库版本：条数 + 最大 id + 最后修改时间，任一变化都会让缓存失效。"""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, COALESCE(MAX(id),0) mx, COALESCE(MAX(created_at),'') t FROM term"
            ).fetchone()
        return f"{row['n']}-{row['mx']}-{row['t']}"
    except sqlite3.Error:
        return "0-0-"


def make_key(source: str, model: str | None = None) -> str:
    raw = f"{model or config.MAIN_MODEL}|{config.TARGET_LNG}|{terms_version()}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def get(key: str) -> dict | None:
    try:
        with _conn() as conn:
            row = conn.execute("SELECT * FROM tr_cache WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {"source": row["source"], "target": row["target"], "meta": meta}


def put(key: str, source: str, target: str, meta: dict | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO tr_cache (key, source, target, meta, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET target=excluded.target, meta=excluded.meta""",
            (
                key,
                source,
                target,
                json.dumps(meta or {}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def stats() -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(LENGTH(source)),0) chars FROM tr_cache"
        ).fetchone()
    return {"entries": row["n"], "source_chars": row["chars"]}


def clear() -> int:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM tr_cache")
        return cur.rowcount
