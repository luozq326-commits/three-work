"""笔记生成与落库。

抽取结果 → 中文精读笔记（Markdown）→ 存库 → 导出文件。
笔记格式固定七段，与 prompts.NOTE_SYSTEM 一致，方便后续跨文献对比。
"""

import json
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
    elements    TEXT DEFAULT '{}',
    term_rate   REAL DEFAULT 1.0,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_path ON note(paper_path);
"""

SECTIONS = (
    "一句话总结",
    "研究问题",
    "方法要点",
    "关键数据",
    "创新点",
    "局限与我的疑问",
    "可借鉴点",
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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


def build_note(elements: Elements, title: str = "", use_model: bool = True) -> str:
    """把抽取结果整理成中文笔记。模型失败时退回模板笔记。"""
    if not use_model:
        return _fallback_note(elements, title) + _evidence_appendix(elements)

    payload = json.dumps(
        {k: v for k, v in elements.data.items() if k != "evidence"},
        ensure_ascii=False,
        indent=1,
    )
    try:
        body = llm.chat(
            [
                {"role": "system", "content": NOTE_SYSTEM},
                {
                    "role": "user",
                    "content": f"论文标题：{title or '未知'}\n\n结构化要素：\n{payload}",
                },
            ]
        )
    except Exception:
        return _fallback_note(elements, title) + _evidence_appendix(elements)

    if not body.strip().startswith("#"):
        body = f"# {title or '未命名论文'}\n\n{body.strip()}"

    # 模型爱用 ### 当章节标题，统一压回 ##，保证七段结构在渲染时同级
    for name in SECTIONS:
        stem = name.split("与")[0].strip()
        body = re.sub(
            rf"^#{{1,6}}\s*({re.escape(stem)}[^\n]*)$", r"## \1", body, flags=re.M
        )

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


def export_markdown(note_id: int, path: str) -> str:
    """把一条笔记写成 .md 文件，返回路径。"""
    row = load(note_id)
    if not row:
        raise ValueError(f"笔记 {note_id} 不存在")
    with open(path, "w", encoding="utf-8") as f:
        f.write(row["note_md"])
    return path
