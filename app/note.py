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
    sql = "SELECT id, paper_path, title, term_rate, created_at FROM note"
    args: tuple = ()
    if paper_path:
        sql += " WHERE paper_path = ?"
        args = (paper_path,)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", args).fetchall()]


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
