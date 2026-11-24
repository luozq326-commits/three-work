"""笔记生成与落库。

抽取结果 → 中文精读笔记（Markdown）→ 存库 → 导出文件。
笔记格式固定七段，与 prompts.NOTE_SYSTEM 一致，方便后续跨文献对比。
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import config, llm
from .extract import Elements
from .prompts import NOTE_SYSTEM

SCHEMA = """
CREATE TABLE IF NOT EXISTS note (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_path  TEXT NOT NULL,
    title       TEXT DEFAULT '',
    note_md     TEXT NOT NULL,
    user_note   TEXT DEFAULT '',
    elements    TEXT DEFAULT '{}',
    term_rate   REAL DEFAULT 1.0,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_path ON note(paper_path);
CREATE TABLE IF NOT EXISTS paper_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_hash    TEXT NOT NULL UNIQUE,
    note_id     INTEGER,
    pdf_path    TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    backend     TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    terms_ver   TEXT DEFAULT '',
    data        TEXT NOT NULL,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_note ON paper_snapshot(note_id);
CREATE TABLE IF NOT EXISTS image_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT DEFAULT '',
    src_text    TEXT NOT NULL,
    dst_text    TEXT NOT NULL,
    chars       INTEGER DEFAULT 0,
    term_rate   REAL DEFAULT 1.0,
    seconds     REAL DEFAULT 0.0,
    created_at  TEXT
);
"""

SECTIONS = (
    "核心结论",
    "研究问题",
    "方法要点",
    "关键数据",
    "创新点",
    "局限与我的疑问",
    "可借鉴点",
)

# 每节的写作要点：逐节生成时一次只喂一条，避免模型把七节混成一锅
NOTE_SECTIONS = (
    ("核心结论", "用 300 字说清这篇论文最后立住了什么结论，靠什么证据立住的，成立的前提是什么。"),
    ("研究问题", "用 300 字讲清它要解决的具体问题、此前做法的卡点，以及这个问题为什么重要。"),
    ("方法要点", "用 300 字按步骤讲方法怎么运作：输入是什么、每一步做什么、关键设计为什么必须这样。"),
    ("关键数据", "用 300 字列具体数字：数据集、指标、提升幅度、对比对象。数字必须来自给定材料。"),
    ("创新点", "用 300 字逐条评估作者宣称的创新，指出哪些是真创新、哪些是组合或包装，并给依据。"),
    ("局限与我的疑问", "用 300 字写局限：作者承认的、你看出来的、以及读完后你自己想问作者的问题。"),
    ("可借鉴点", "用 300 字写这篇论文里哪些做法能搬到自己的研究或工程里，搬的时候要注意什么。"),
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 旧库补列：批注是后加的能力，老数据不该因此读不出来
    cols = {r[1] for r in conn.execute("PRAGMA table_info(note)")}
    if "user_note" not in cols:
        conn.execute("ALTER TABLE note ADD COLUMN user_note TEXT DEFAULT ''")
    return conn


def _fallback_note(elements: Elements, title: str) -> str:
    """模型不可用时的模板笔记。宁可给出干巴巴的结构，也不要让链路断掉。"""
    d = elements.data
    lines = [f"# {title or '未命名论文'}", ""]

    def bullet(items, empty="（未抽取到）"):
        items = [i for i in (items or []) if i]
        return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"

    lines += ["## 一句话总结", d.get("research_question") or "（未抽取到）", ""]
    lines += ["## 研究问题", d.get("gap") or "（未抽取到）", ""]
    lines += ["## 方法要点", bullet(d.get("method")), ""]
    kr = d.get("key_results") or []
    lines += [
        "## 关键数据",
        "\n".join(
            f"- {k.get('metric', '')}：{k.get('value', '')}（对比 {k.get('baseline', '无')}）"
            for k in kr
        )
        or "- （未抽取到）",
        "",
    ]
    lines += ["## 创新点", bullet(d.get("novelty")), ""]
    lines += ["## 局限与我的疑问", bullet(d.get("limitations")), ""]
    lines += ["## 可借鉴点", bullet(d.get("future_work")), ""]
    return "\n".join(lines)


def _has_section(body: str, name: str) -> bool:
    """判断笔记里是否已有这一段。模型常把"局限与我的疑问"简写成"局限"，按主干词匹配。"""
    stem = name.split("与")[0].strip()
    return name in body or (stem and stem in body)


def _evidence_appendix(elements: Elements, limit: int = 8) -> str:
    """把回查通过的原文证据附在笔记末尾，让每条结论都能溯源。"""
    good = elements.verified_evidence()[:limit]
    if not good:
        return ""
    lines = ["", "## 原文依据", ""]
    for e in good:
        lines.append(f"- **{e['field']}**：{e['quote'][:180]}")
    return "\n".join(lines)


def build_note(
    elements: Elements, title: str = "", use_model: bool = True, context: str = ""
) -> str:
    """把抽取结果整理成中文笔记。模型失败时退回模板笔记。

    context 是论文正文节选：只给结构化字段的话，模型写不出 300 字/节，
    只能反复复述同样几句话。宁可多喂上下文。
    """
    if not use_model:
        return _fallback_note(elements, title) + _evidence_appendix(elements)

    payload = json.dumps(
        {k: v for k, v in elements.data.items() if k != "evidence"},
        ensure_ascii=False,
        indent=1,
    )
    head = (
        f"论文标题：{title or '未知'}\n\n结构化要素：\n{payload}\n\n"
        + (f"论文正文节选（写细节时以这里为准）：\n{context}" if context else "")
    )

    # 逐节生成：一次让模型写七节，实测每节只有 80~130 字，怎么要求都写不够。
    # 每节单独一次调用，300 字才稳。
    parts: list[str] = []
    try:
        for name, brief in NOTE_SECTIONS:
            user = (
                f"{head}\n\n---\n现在只写「{name}」这一节。\n本节要写：{brief}\n"
                f"篇幅不少于 300 字，只输出这一节，用「## {name}」开头。"
            )
            body = llm.chat(
                [{"role": "system", "content": NOTE_SYSTEM}, {"role": "user", "content": user}],
                timeout=200,
                max_tokens=900,
            ).strip()
            if not body.startswith("##"):
                body = f"## {name}\n\n{body}"
            parts.append(body)
    except Exception:
        return _fallback_note(elements, title) + _evidence_appendix(elements)

    body = f"# {title or '未命名论文'}\n\n" + "\n\n".join(parts)

    # 缺哪一段就补哪一段，保证七段结构完整
    missing = [s for s in SECTIONS if not _has_section(body, s)]
    if missing:
        body += "\n\n" + "\n\n".join(
            f"## {s}\n- （模型未覆盖，请人工补充）" for s in missing
        )
    return (body.strip() + "\n" + _evidence_appendix(elements)).strip() + "\n"


def save(
    paper_path: str,
    title: str,
    note_md: str,
    elements: Elements | None = None,
    term_rate: float = 1.0,
) -> int:
    """存一条笔记，返回 id。同一篇论文多次生成会各存一条，不覆盖历史。"""
    payload = json.dumps(
        elements.data if elements else {}, ensure_ascii=False
    )
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO note (paper_path, title, note_md, elements, term_rate, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                paper_path,
                title,
                note_md,
                payload,
                term_rate,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid


def list_notes(paper_path: str | None = None) -> list[dict]:
    sql = """SELECT n.id, n.paper_path, n.title, n.term_rate, n.created_at,
                    CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END AS has_snapshot
             FROM note n
             LEFT JOIN paper_snapshot s ON s.note_id = n.id"""
    args: tuple = ()
    if paper_path:
        sql += " WHERE n.paper_path = ?"
        args = (paper_path,)
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY n.id DESC", args).fetchall()]
        for r in rows:
            r["has_snapshot"] = bool(r.get("has_snapshot"))
        return rows


def save_snapshot(
    pdf_hash: str,
    note_id: int | None,
    task_data: dict,
    model: str = "",
    terms_ver: str = "",
) -> None:
    """持久化单篇论文的完整精读快照（解析章节+译文+要素+总结+笔记），用于秒开复用。"""
    if not pdf_hash:
        return
    keep = {
        "pdf": task_data.get("pdf", ""),
        "title": task_data.get("title", ""),
        "pages": task_data.get("pages", 0),
        "backend": task_data.get("backend", ""),
        "ocr_used": bool(task_data.get("ocr_used")),
        "sections": task_data.get("sections") or [],
        "elements": task_data.get("elements"),
        "summary": task_data.get("summary") or "",
        "note_md": task_data.get("note_md") or "",
        "note_id": note_id,
        "timings": task_data.get("timings") or {},
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            """INSERT INTO paper_snapshot
               (pdf_hash, note_id, pdf_path, title, backend, model, terms_ver, data, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pdf_hash) DO UPDATE SET
                 note_id=COALESCE(excluded.note_id, paper_snapshot.note_id),
                 pdf_path=excluded.pdf_path,
                 title=excluded.title,
                 backend=excluded.backend,
                 model=excluded.model,
                 terms_ver=excluded.terms_ver,
                 data=excluded.data,
                 updated_at=excluded.updated_at""",
            (
                pdf_hash,
                note_id,
                keep["pdf"],
                keep["title"],
                keep["backend"],
                model,
                terms_ver,
                json.dumps(keep, ensure_ascii=False),
                now,
            ),
        )


def _row_to_snapshot(row) -> dict | None:
    if not row:
        return None
    try:
        payload = json.loads(row["data"])
    except Exception:
        return None
    payload["pdf_hash"] = row["pdf_hash"]
    payload["snapshot_model"] = row["model"] or ""
    payload["snapshot_terms_ver"] = row["terms_ver"] or ""
    payload["snapshot_updated_at"] = row["updated_at"] or ""
    if row["note_id"]:
        payload["note_id"] = row["note_id"]
    return payload


def load_snapshot_by_hash(pdf_hash: str) -> dict | None:
    if not pdf_hash:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM paper_snapshot WHERE pdf_hash = ?", (pdf_hash,)
        ).fetchone()
        return _row_to_snapshot(row)


def load_snapshot_by_note(note_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM paper_snapshot WHERE note_id = ? ORDER BY updated_at DESC LIMIT 1",
            (note_id,),
        ).fetchone()
        return _row_to_snapshot(row)


def update_snapshot_fields(note_id: int, note_md: str | None = None, summary: str | None = None) -> None:
    """笔记正文或 AI 总结更新时，同步写回对应快照。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, data FROM paper_snapshot WHERE note_id = ? ORDER BY updated_at DESC LIMIT 1",
            (note_id,),
        ).fetchone()
        if not row:
            return
        try:
            payload = json.loads(row["data"])
        except Exception:
            return
        if note_md is not None:
            payload["note_md"] = note_md
        if summary is not None:
            payload["summary"] = summary
        conn.execute(
            "UPDATE paper_snapshot SET data = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                row["id"],
            ),
        )


def load(note_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM note WHERE id = ?", (note_id,)).fetchone()
        return dict(row) if row else None


def update(note_id: int, note_md: str | None = None, user_note: str | None = None) -> bool:
    """改笔记正文或批注。只改传进来的字段，None 表示不动。"""
    sets, args = [], []
    if note_md is not None:
        sets.append("note_md = ?")
        args.append(note_md)
    if user_note is not None:
        sets.append("user_note = ?")
        args.append(user_note)
    if not sets:
        return False
    args.append(note_id)
    with _conn() as conn:
        cur = conn.execute(f"UPDATE note SET {', '.join(sets)} WHERE id = ?", args)
        return cur.rowcount > 0


def delete_note(note_id: int) -> bool:
    """删除单条历史笔记，并联动清理其阅读快照与检索分块。"""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM note WHERE id = ?", (note_id,))
        conn.execute("DELETE FROM paper_snapshot WHERE note_id = ?", (note_id,))
        try:
            conn.execute("DELETE FROM note_chunk WHERE note_id = ?", (note_id,))
        except sqlite3.Error:
            pass
        return cur.rowcount > 0


def clear_notes(only_without_snapshot: bool = False) -> int:
    """批量清理历史笔记。only_without_snapshot=True 时仅清掉无阅读快照的旧记录。"""
    with _conn() as conn:
        if only_without_snapshot:
            ids = [
                r["id"]
                for r in conn.execute(
                    """SELECT n.id FROM note n
                       LEFT JOIN paper_snapshot s ON s.note_id = n.id
                       WHERE s.id IS NULL"""
                ).fetchall()
            ]
            if not ids:
                return 0
            ph = ",".join("?" for _ in ids)
            cur = conn.execute(f"DELETE FROM note WHERE id IN ({ph})", ids)
            try:
                conn.execute(f"DELETE FROM note_chunk WHERE note_id IN ({ph})", ids)
            except sqlite3.Error:
                pass
            return cur.rowcount
        cur = conn.execute("DELETE FROM note")
        conn.execute("DELETE FROM paper_snapshot")
        try:
            conn.execute("DELETE FROM note_chunk")
        except sqlite3.Error:
            pass
        return cur.rowcount


def save_image_history(
    name: str,
    src_text: str,
    dst_text: str,
    chars: int = 0,
    term_rate: float = 1.0,
    seconds: float = 0.0,
) -> int:
    """保存一条图片翻译历史记录，返回 id。"""
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO image_history (name, src_text, dst_text, chars, term_rate, seconds, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name or "image.png",
                src_text,
                dst_text,
                chars or len(src_text),
                term_rate if term_rate is not None else 1.0,
                seconds or 0.0,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid


def list_image_history(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, name, substr(src_text, 1, 80) AS preview, chars, term_rate, seconds, created_at
               FROM image_history ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def load_image_history(item_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM image_history WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def delete_image_history(item_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM image_history WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def clear_image_history() -> int:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM image_history")
        return cur.rowcount


def export_markdown(note_id: int, path: str) -> str:
    """把一条笔记写成 .md 文件（含批注），返回路径。"""
    row = load(note_id)
    if not row:
        raise ValueError(f"笔记 {note_id} 不存在")
    body = row["note_md"]
    if (row.get("user_note") or "").strip():
        body += "\n\n## 我的批注\n\n" + row["user_note"].strip() + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path
