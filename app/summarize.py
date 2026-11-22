"""整篇总结：把论文压成一段能快速判断"值不值得读"的中文摘要。

和 extract.py 的区别：
- extract 抽的是结构化字段（研究问题、方法、指标…），要求每条带原文证据，偏严谨
- summarize 生成的是连贯叙述，偏可读，用于界面右栏的"AI 总结"

两者互补：字段为空时，总结仍能给出整体判断。
"""

from . import llm
from .prompts import SUMMARY_SYSTEM


def build_context(sections, max_chars: int = 7000) -> str:
    """挑摘要 + 引言 + 方法 + 实验 + 结论，按配额拼上下文。"""
    weight = {
        "abstract": 1.6,
        "introduction": 1.2,
        "method": 1.4,
        "experiment": 1.3,
        "conclusion": 1.0,
        "other": 0.8,
    }
    picked = [s for s in sections if s.kind in weight and (s.text or "").strip()]
    if not picked:
        return ""
    total_w = sum(weight[s.kind] for s in picked)
    parts = []
    for s in picked:
        quota = int(max_chars * weight[s.kind] / total_w)
        parts.append(f"[{s.kind}] {s.text.strip()[:quota]}")
    return "\n\n".join(parts)


def summarize(sections, title: str = "", max_chars: int = 7000) -> str:
    """生成整篇总结（Markdown）。模型不可用时抛 LLMError，由调用方决定怎么提示。"""
    ctx = build_context(sections, max_chars)
    if not ctx.strip():
        raise ValueError("没有可用正文，无法总结")
    return llm.chat(
        [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": f"论文标题：{title or '未知'}\n\n正文节选：\n{ctx}",
            },
        ]
    ).strip()
