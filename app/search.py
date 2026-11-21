"""跨文献笔记检索。

把每条笔记按小节切开、向量化存库，检索时算余弦相似度。
向量模型不在时降级为字符二元组重叠打分——**检索是辅助功能，不该因为少个模型就整条链路报错**。

不引入向量数据库：笔记量级是几百到几千小节，numpy 都不必上，纯 Python 点积足够快。
"""

import array
import json
import math
import re
import sqlite3
import threading

from . import config, llm

# 建索引是"删了再插"，两个线程同时跑会产生重复小节（实测 93 条变成 186 条）
_INDEX_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS note_chunk (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id   INTEGER NOT NULL,
    title     TEXT DEFAULT '',
    section   TEXT DEFAULT '',
    text      TEXT NOT NULL,
    vec       BLOB,
    model     TEXT DEFAULT '',
    dim       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunk_note ON note_chunk(note_id);
"""

# 这些小节不参与检索：原文依据是英文引用，检索价值低且会稀释中文命中
SKIP_SECTIONS = ("原文依据",)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def chunk_note(note_md: str) -> list[tuple[str, str]]:
    """把笔记按 `## 小节` 切开，返回 [(小节名, 正文)]。"""
    parts: list[tuple[str, str]] = []
    cur_name, buf = "正文", []
    for line in note_md.split("\n"):
        m = re.match(r"^#{1,3}\s+(.*)$", line)
        if m:
            if buf and _norm_text("\n".join(buf)):
                parts.append((cur_name, _norm_text("\n".join(buf))))
            cur_name, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if buf and _norm_text("\n".join(buf)):
        parts.append((cur_name, _norm_text("\n".join(buf))))
    return [(n, t) for n, t in parts if not any(s in n for s in SKIP_SECTIONS) and len(t) > 10]


def _pack(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def _l2(vec) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def index_note(note_id: int, title: str, note_md: str, force: bool = False) -> int:
    """给一条笔记建索引。已建过且模型没换则跳过。返回写入的小节数。"""
    with _INDEX_LOCK:
        chunks = chunk_note(note_md)
        if not chunks:
            return 0

        with _conn() as conn:
            if not force:
                row = conn.execute(
                    "SELECT model, COUNT(*) n FROM note_chunk WHERE note_id = ? GROUP BY model",
                    (note_id,),
                ).fetchone()
                if row and row["n"] and row["model"] == config.EMBED_MODEL:
                    return 0

        vectors: list[list[float]] = []
        model = ""
        if llm.embed_ready():
            try:
                vectors = [_l2(v) for v in llm.embed([t for _, t in chunks])]
                model = config.EMBED_MODEL
            except Exception:
                vectors = []

        # 删除和插入放在同一个连接里，避免中途被另一个线程插队
        with _conn() as conn:
            conn.execute("DELETE FROM note_chunk WHERE note_id = ?", (note_id,))
            for i, (section, text) in enumerate(chunks):
                vec = vectors[i] if i < len(vectors) else None
                conn.execute(
                    """INSERT INTO note_chunk (note_id, title, section, text, vec, model, dim)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        note_id,
                        title,
                        section,
                        text,
                        _pack(vec) if vec else None,
                        model,
                        len(vec) if vec else 0,
                    ),
                )
        return len(chunks)


def index_all(force: bool = False) -> dict:
    """给所有笔记建索引。返回统计。"""
    from . import note as note_mod

    with _conn() as conn:
        rows = conn.execute("SELECT id, title, note_md FROM note ORDER BY id").fetchall()
    indexed = chunks = 0
    for r in rows:
        n = index_note(r["id"], r["title"] or "", r["note_md"], force=force)
        if n:
            indexed += 1
            chunks += n
    return {"notes": len(rows), "indexed": indexed, "chunks": chunks}


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[^\w\u4e00-\u9fa5]+", "", s.lower())
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _keyword_score(query: str, text: str) -> float:
    """无向量时的兜底：查询与正文的字符二元组重叠率。中文不需要分词。"""
    q, t = _bigrams(query), _bigrams(text)
    if not q:
        return 0.0
    return len(q & t) / len(q)


def search(query: str, top_k: int = 5) -> dict:
    """跨文献检索。返回 {mode, hits: [{note_id, title, section, text, score}]}。

    mode 说明本次用的是向量还是关键词：界面要如实显示，别让人以为一直是语义检索。
    """
    query = (query or "").strip()
    if not query:
        return {"mode": "empty", "hits": []}

    with _conn() as conn:
        rows = conn.execute(
            "SELECT note_id, title, section, text, vec, model FROM note_chunk"
        ).fetchall()
    if not rows:
        return {"mode": "empty", "hits": []}

    hits: list[dict] = []
    mode = "keyword"
    if llm.embed_ready() and any(r["vec"] for r in rows):
        try:
            qv = _l2(llm.embed([query])[0])
            mode = "vector"
            for r in rows:
                if not r["vec"]:
                    continue
                v = _unpack(r["vec"])
                if len(v) != len(qv):
                    continue
                hits.append(
                    {
                        "note_id": r["note_id"],
                        "title": r["title"],
                        "section": r["section"],
                        "text": r["text"],
                        "score": round(sum(a * b for a, b in zip(qv, v)), 4),
                    }
                )
        except Exception:
            hits = []

    if not hits:
        mode = "keyword"
        hits = [
            {
                "note_id": r["note_id"],
                "title": r["title"],
                "section": r["section"],
                "text": r["text"],
                "score": round(_keyword_score(query, r["text"] + r["section"]), 4),
            }
            for r in rows
        ]

    hits = [h for h in hits if h["score"] > 0.01]
    hits.sort(key=lambda h: h["score"], reverse=True)
    return {"mode": mode, "hits": hits[:top_k]}
