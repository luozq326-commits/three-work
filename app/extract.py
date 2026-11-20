"""要素抽取：章节挑选 → 模型抽取 → evidence 校验 → 证据打分重排。

为什么不是「让模型输出 JSON 就完事」：
小模型极容易把没读到的内容编成 JSON。这里的做法是要求每条要素附一条英文原句，
然后在**本地**把原句回查原文（不依赖模型自述），回查不到的当场标出来。
这一步借鉴 paper-qa 的三段式：先攒候选证据，再逐条打分，最后只用高分证据下结论。
"""

import re
from dataclasses import dataclass, field

from . import llm, pdf_parse
from .prompts import EXTRACT_SYSTEM, EVIDENCE_SCORE_SYSTEM

# 抽取时优先喂给模型的章节：摘要给问题，方法给做法，实验给数字
PICK_KINDS = ("abstract", "introduction", "method", "experiment", "other", "conclusion")

# 这些字段是列表，其余是字符串，用于校验结构
LIST_FIELDS = ("method", "dataset", "metrics", "novelty", "limitations", "future_work")
DICT_LIST_FIELDS = ("key_results", "evidence")


@dataclass
class Elements:
    data: dict
    evidence_checked: list[dict] = field(default_factory=list)
    hit_rate: float = 1.0
    raw_context: str = ""

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def verified_evidence(self) -> list[dict]:
        return [e for e in self.evidence_checked if e.get("verified")]

    def report(self) -> dict:
        filled = sorted(
            k for k, v in self.data.items() if k != "evidence" and v not in ("", [], None)
        )
        return {
            "evidence_total": len(self.evidence_checked),
            "evidence_verified": len(self.verified_evidence()),
            "hit_rate": round(self.hit_rate, 3),
            "fields": filled,
            # 一个字段都没抽出来，基本是模型把 schema 抄回来后被过滤光了
            "empty": not filled,
        }


def _norm(text: str) -> str:
    """归一化：去 markdown 标记、合并空白、小写。用于原句回查。"""
    text = re.sub(r"(\*\*|__|\*|_|`)", "", text)
    text = text.replace("\u00ad", "")
    return re.sub(r"\s+", " ", text).strip().lower()


def pick_context(paper: pdf_parse.ParsedPaper, max_chars: int = 6000) -> str:
    """按章节类型拼抽取上下文，超出预算时按比例截断而不是直接砍尾。"""
    picked = [s for s in paper.sections if s.kind in PICK_KINDS and s.text.strip()]
    if not picked:
        return paper.raw_text[:max_chars]

    # 摘要和实验章的信息密度最高，给更大配额
    weight = {"abstract": 1.5, "method": 1.2, "experiment": 1.2, "conclusion": 0.8}
    total_w = sum(weight.get(s.kind, 1.0) for s in picked)
    parts = []
    for s in picked:
        quota = int(max_chars * weight.get(s.kind, 1.0) / total_w)
        body = s.text.strip()
        parts.append(f"[{s.kind}] {body[:quota]}")
    return "\n\n".join(parts)


def _verify_quote(quote: str, haystack_norm: str) -> dict:
    """在原文里回查一句引用。先精确匹配，再退化到前 60 字符片段。"""
    q = _norm(quote)
    if not q:
        return {"verified": False, "match": "empty"}
    if q in haystack_norm:
        return {"verified": True, "match": "exact"}
    head = q[:60]
    if len(head) >= 20 and head in haystack_norm:
        return {"verified": True, "match": "prefix"}
    # 长句可能被换行/连字符打断，用前 8 个词再试一次
    words = q.split()[:8]
    if len(words) >= 5:
        frag = " ".join(words)
        if frag in haystack_norm:
            return {"verified": True, "match": "fragment"}
    return {"verified": False, "match": "miss"}


# 小模型会把 prompt 里的字段说明原样抄回来当答案，这类值等同于空。
# 见过的最典型："quote": "支撑该字段的原文英文句子"、"method": ["方法要点，每条一句"]
_PLACEHOLDER_MARKERS = (
    "上面的某个字段名",
    "支撑该字段",
    "方法要点",
    "每条一句",
    "指标名",
    "对比对象",
    "这篇论文要解决",
    "前人方法的不足",
    "使用的数据集",
    "评价指标",
    "创新点，每条",
    "后续工作方向",
    "字段名",
)


def _is_placeholder(value) -> bool:
    s = str(value).strip()
    if not s:
        return True
    if any(m in s for m in _PLACEHOLDER_MARKERS):
        return True
    return bool(re.fullmatch(r"<[^>]*>", s))


def _coerce(data: dict) -> dict:
    """把小模型常见的结构错误拉回来：该是列表的给了字符串、该是字符串的给了列表。"""
    out = {}
    for key in (
        "research_question",
        "gap",
    ):
        v = data.get(key, "")
        v = v if isinstance(v, str) else (v[0] if isinstance(v, list) and v else "")
        out[key] = "" if _is_placeholder(v) else v
    for key in LIST_FIELDS:
        v = data.get(key, [])
        if isinstance(v, str):
            v = [v] if v.strip() else []
        out[key] = [str(x).strip() for x in v if not _is_placeholder(x)]
    for key in DICT_LIST_FIELDS:
        v = data.get(key, [])
        if isinstance(v, dict):
            v = [v]
        kept = []
        for x in v:
            if not isinstance(x, dict):
                continue
            if key == "evidence":
                if _is_placeholder(x.get("quote")) or _is_placeholder(x.get("field")):
                    continue
            elif _is_placeholder(x.get("metric")) or _is_placeholder(x.get("value")):
                continue
            kept.append(x)
        out[key] = kept
    return out


def extract_elements(
    paper: pdf_parse.ParsedPaper, max_chars: int = 6000, retries: int = 1
) -> Elements:
    """抽取结构化要素并回查 evidence。"""
    context = pick_context(paper, max_chars)
    if not context.strip():
        raise ValueError("解析结果为空，无法抽取")

    raw = llm.chat_json(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": context},
        ],
        retries=retries,
    )
    data = _coerce(raw)

    haystack = _norm(paper.raw_text)
    checked = []
    for item in data.get("evidence", []):
        quote = str(item.get("quote", "")).strip()
        verdict = _verify_quote(quote, haystack)
        checked.append(
            {
                "field": str(item.get("field", "")).strip(),
                "quote": quote,
                **verdict,
            }
        )
    data["evidence"] = checked

    hit_rate = (
        sum(1 for e in checked if e["verified"]) / len(checked) if checked else 1.0
    )
    return Elements(data=data, evidence_checked=checked, hit_rate=hit_rate, raw_context=context)


def rerank_evidence(elements: Elements, top_k: int = 6) -> list[dict]:
    """让模型给每条已回查通过的证据打分（0-3），只留高分。

    回查只能证明「这句话在原文里」，不能证明「这句话支撑该字段」。
    打分这一步就是补后者——paper-qa 的 gather evidence 阶段做的同一件事。
    """
    good = [e for e in elements.evidence_checked if e.get("verified")]
    if len(good) <= top_k:
        return good

    lines = [f"{i}. [{e['field']}] {e['quote'][:200]}" for i, e in enumerate(good)]
    try:
        scored = llm.chat_json(
            [
                {"role": "system", "content": EVIDENCE_SCORE_SYSTEM},
                {
                    "role": "user",
                    "content": "要素上下文：\n"
                    + elements.raw_context[:2000]
                    + "\n\n待打分证据：\n"
                    + "\n".join(lines),
                },
            ]
        )
        scores = {int(s["id"]): int(s["score"]) for s in scored.get("scores", [])}
    except Exception:
        # 打分失败不影响主流程，退回原始顺序
        return good[:top_k]

    for i, e in enumerate(good):
        e["score"] = scores.get(i, 0)
    good.sort(key=lambda e: e.get("score", 0), reverse=True)
    return good[:top_k]
