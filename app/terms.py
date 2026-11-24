"""术语库：SQLite 存储 + 多模式匹配 + 约束块生成 + 译文校验。

这是项目的核心创新点。四步闭环：
    抽取候选 → 用户确认入库 → 翻译时注入约束 → 译文后置校验（未命中则重译该段）
"""

import re
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS term (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL UNIQUE,
    target      TEXT NOT NULL,
    domain      TEXT DEFAULT 'general',
    definition  TEXT DEFAULT '',
    mode        TEXT DEFAULT 'force',   -- force: 必须照译 | suggest: 建议
    origin      TEXT DEFAULT 'manual',  -- manual: 人工确认 | auto: 自动提取待确认
    tgt_lng     TEXT DEFAULT '',        -- 适用目标语言，空=不限
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS variant (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id  INTEGER NOT NULL,
    form     TEXT NOT NULL,
    FOREIGN KEY (term_id) REFERENCES term(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_variant_term ON variant(term_id);
"""

# 优先级：人工 > 自动提取。数字小的先注入，冲突时人工的译法排在前面被模型优先看到。
ORIGIN_PRIORITY = {"manual": 0, "auto": 1}


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补列。项目从单机课设起步，不做版本号，只做幂等补列。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(term)")}
    if "origin" not in cols:
        conn.execute("ALTER TABLE term ADD COLUMN origin TEXT DEFAULT 'manual'")
    if "tgt_lng" not in cols:
        conn.execute("ALTER TABLE term ADD COLUMN tgt_lng TEXT DEFAULT ''")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def add_term(
    source: str,
    target: str,
    domain: str = "general",
    definition: str = "",
    mode: str = "force",
    variants: list[str] | None = None,
    origin: str = "manual",
    tgt_lng: str = "",
) -> int:
    """新增或更新术语。返回 term id。

    已存在的 source 只更新译法与属性，不覆盖 origin（人工确认过的不会被自动提取降级）。
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO term (source, target, domain, definition, mode, origin, tgt_lng, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                 target=CASE WHEN term.origin='manual' AND excluded.origin<>'manual'
                             THEN term.target ELSE excluded.target END,
                 domain=excluded.domain,
                 definition=CASE WHEN term.origin='manual' AND excluded.origin<>'manual'
                                 THEN term.definition ELSE excluded.definition END,
                 mode=CASE WHEN term.origin='manual' AND excluded.origin<>'manual'
                           THEN term.mode ELSE excluded.mode END,
                 tgt_lng=excluded.tgt_lng,
                 origin=CASE WHEN term.origin='manual' THEN 'manual' ELSE excluded.origin END""",
            (
                source.strip(),
                target.strip(),
                domain,
                definition,
                mode,
                origin,
                tgt_lng,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        row = conn.execute("SELECT id FROM term WHERE source = ?", (source.strip(),)).fetchone()
        term_id = row["id"]
        if variants:
            conn.execute("DELETE FROM variant WHERE term_id = ?", (term_id,))
            conn.executemany(
                "INSERT INTO variant (term_id, form) VALUES (?, ?)",
                [(term_id, v.strip()) for v in variants if v.strip()],
            )
        return term_id


def all_terms(domain: str | None = None) -> list[dict]:
    sql = "SELECT * FROM term"
    args: tuple = ()
    if domain:
        sql += " WHERE domain = ?"
        args = (domain,)
    with _conn() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            forms = [
                v["form"]
                for v in conn.execute(
                    "SELECT form FROM variant WHERE term_id = ?", (r["id"],)
                ).fetchall()
            ]
            out.append({**dict(r), "variants": forms})
        return out


def match_terms(text: str, domain: str | None = None, tgt_lng: str | None = None) -> list[dict]:
    """在文本中找出命中的术语。返回 [{source, target, mode, origin, hits}]。

    tgt_lng 给定时，只使用"适用该目标语言"或"未标注语言"的术语（同 BabelDOC 的语义）。
    术语量在千级以内时逐条正则足够快；上万条应换成 Aho-Corasick。
    """
    hits = []
    for term in all_terms(domain):
        if tgt_lng:
            tl = (term.get("tgt_lng") or "").lower().replace("-", "_")
            want = tgt_lng.lower().replace("-", "_")
            if tl and tl != want:
                continue
        # 变体与源词可能重叠（ablation study / ablation），
        # 必须合成单条交替式正则并按长度倒序，否则同一处会被重复计数。
        forms = sorted({term["source"], *term["variants"]}, key=len, reverse=True)
        forms = [f for f in forms if f]
        if not forms:
            continue
        pattern = (
            r"(?<![A-Za-z])(" + "|".join(re.escape(f) for f in forms) + r")(?![A-Za-z])"
        )
        count = len(re.findall(pattern, text, flags=re.IGNORECASE))
        if count:
            hits.append(
                {
                    "source": term["source"],
                    "target": term["target"],
                    "mode": term["mode"],
                    "origin": term.get("origin") or "manual",
                    "hits": count,
                }
            )
    return hits


def constraint_block(hits: list[dict]) -> str:
    """把命中的术语拼成一段提示，塞进 system prompt。

    人工确认的术语排在自动提取的前面：冲突时模型更容易采纳前面的译法。
    """
    forced = [h for h in hits if h["mode"] == "force"]
    if not forced:
        return ""
    forced.sort(key=lambda h: ORIGIN_PRIORITY.get(h.get("origin") or "manual", 9))
    lines = [f"- {h['source']} → {h['target']}" for h in forced]
    return "以下术语必须严格按给定译法翻译，不得改写：\n" + "\n".join(lines)


def verify_translation(
    source: str, translated: str, domain: str | None = None, tgt_lng: str | None = None
) -> dict:
    """校验译文是否遵守术语约束。

    返回 {consistent: bool, violations: [{source, target}], rate: float}
    rate = 命中约束的术语数 / 原文中应出现的术语数，用于评测报告。
    """
    hits = [h for h in match_terms(source, domain, tgt_lng) if h["mode"] == "force"]
    if not hits:
        return {"consistent": True, "violations": [], "rate": 1.0}

    violations = [
        {"source": h["source"], "target": h["target"]}
        for h in hits
        if h["target"] not in translated
    ]
    rate = 1 - len(violations) / len(hits)
    return {"consistent": not violations, "violations": violations, "rate": round(rate, 3)}


def seed_demo() -> int:
    """写入几条演示用术语，方便第一次跑通。返回条数。"""
    demo = [
        ("transformer", "变换器", "nlp", "基于自注意力机制的序列建模架构", ["Transformers"]),
        ("ablation study", "消融实验", "general", "逐项移除组件以验证其贡献", ["ablation"]),
        ("fine-tuning", "微调", "general", "", ["finetuning", "fine-tune"]),
        ("state-of-the-art", "当前最优", "general", "", ["SOTA"]),
        ("benchmark", "基准测试", "general", "", ["benchmarks"]),
        ("self-attention", "自注意力", "nlp", "", ["self attention"]),
    ]
    for src, tgt, dom, desc, var in demo:
        add_term(src, tgt, dom, desc, "force", var)
    return len(demo)


# ---------------------------------------------------------------- CSV 交换
# 格式对齐 BabelDOC 的术语表：source,target 必需，tgt_lng 可选（空=不限语言）。

CSV_FIELDS = ["source", "target", "tgt_lng", "domain", "definition", "mode", "origin"]


def import_csv(
    path: str,
    domain: str | None = None,
    origin: str = "manual",
    tgt_lng: str | None = None,
) -> dict:
    """从 CSV 导入术语。返回 {added, updated, skipped}。"""
    import csv

    existing = {t["source"].lower(): t for t in all_terms()}
    added = updated = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            src = (row.get("source") or "").strip()
            tgt = (row.get("target") or "").strip()
            if not src or not tgt:
                skipped += 1
                continue
            hit = existing.get(src.lower())
            add_term(
                src,
                tgt,
                domain=domain or (row.get("domain") or "general").strip(),
                definition=(row.get("definition") or "").strip(),
                mode=(row.get("mode") or "force").strip() or "force",
                origin=(row.get("origin") or origin).strip() or origin,
                tgt_lng=(row.get("tgt_lng") or tgt_lng or "").strip(),
            )
            if hit:
                updated += 1
            else:
                added += 1
    return {"added": added, "updated": updated, "skipped": skipped}


def export_csv(path: str, domain: str | None = None) -> int:
    """导出术语表为 CSV，可直接当 BabelDOC 的 --glossary-files 用。返回条数。"""
    import csv

    rows = all_terms(domain)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (r.get(k) or "") for k in CSV_FIELDS})
    return len(rows)


# ---------------------------------------------------------------- 候选抽取
# 四步闭环的第 1 步：从原文里挖候选术语，人工确认后才升级为 force。


def extract_candidates(text: str, limit: int = 15, domain: str | None = None) -> list[dict]:
    """让模型从原文里挑值得统一译法的术语。返回候选列表，不含已入库的。

    每条候选：{source, target, reason, in_db}
    """
    from . import llm
    from .prompts import TERM_EXTRACT_SYSTEM

    text = text.strip()
    if not text:
        return []
    # 长文只取前 6000 字符，候选术语集中在摘要和方法章
    data = llm.chat_json(
        [
            {"role": "system", "content": TERM_EXTRACT_SYSTEM.format(limit=limit)},
            {"role": "user", "content": text[:6000]},
        ]
    )
    known = {t["source"].lower() for t in all_terms()}
    out = []
    for item in data.get("terms", [])[:limit]:
        src = (item.get("source") or "").strip()
        tgt = (item.get("target") or "").strip()
        if not src or not tgt:
            continue
        out.append(
            {
                "source": src,
                "target": tgt,
                "domain": (item.get("domain") or domain or "general").strip() or "general",
                "reason": (item.get("reason") or "").strip(),
                "in_db": src.lower() in known,
            }
        )
    return out


def accept_candidates(candidates: list[dict], domain: str = "general") -> int:
    """把候选升级为正式术语（origin=auto，仍需人工在 UI 里过一眼）。"""
    n = 0
    for c in candidates:
        if c.get("in_db"):
            continue
        item_domain = (c.get("domain") or domain or "general").strip() or "general"
        add_term(c["source"], c["target"], domain=item_domain, origin="auto")
        n += 1
    return n


def delete_term(source: str) -> int:
    """删除一条术语及其变体。返回删除条数。"""
    with _conn() as conn:
        row = conn.execute("SELECT id FROM term WHERE source = ?", (source.strip(),)).fetchone()
        if not row:
            return 0
        conn.execute("DELETE FROM variant WHERE term_id = ?", (row["id"],))
        conn.execute("DELETE FROM term WHERE id = ?", (row["id"],))
        return 1
