"""审稿人级别的整篇深度拆解（八节，每节 ≥300 字）。

为什么逐节生成而不是一次写完：
7B 模型对"每节不少于 300 字"这种整体约束几乎不遵守，实测一次生成时
三十秒版只有 180 字、问题定位只有 130 字，退回扩写也常因标题对不上而失效。
改成每节一次调用、只让它写一节，字数才真正可控（代价是调用次数多、总耗时约 2~3 分钟）。
"""

from . import llm

# 每节一次调用；300 字中文约 500 token，给到 900 留余量，同时防止模型写飞
CALL_TIMEOUT = 200
MAX_TOKENS = 900
MIN_CHARS = 300

ROLE = """你是一位论文所属领域的资深研究者，常年为顶会审稿。
读者是计算机方向的研究生，懂基础概念但不懂论文里的专门推导，目的是判断这篇论文值不值得跟进。

硬性要求：
- 只依据给定正文，不许编造数字或结论；原文没写的就说没写。
- 术语首次出现给中英文对照和一句解释。
- 公式用 LaTeX 写，并配一句人话翻译。
- 不许出现「本文提出了一种新颖的方法」这类空话。
- 用 Markdown，标题层级只用 ## 和 ###。
- **排版要分层次**：每节拆成 2~4 个自然段，段与段之间空一行；
  并列的内容（贡献、实验、局限等）用「- 」分点，一条一行，不要挤成一段；
  关键结论用 **加粗** 标出。宁可分点，不要写成一大段。"""

# (小节标题, 该节要写什么)
SECTIONS = [
    (
        "1. 三十秒版",
        "用 3 句话讲清：它解决什么问题、用什么手段、结论是什么。不要用论文原话，用你自己的话。"
        "三句话之外可以补必要的限定条件（比如只在什么数据集、什么前提下成立）。",
    ),
    (
        "2. 问题定位",
        "回答三问：这篇论文之前的做法是什么、卡在哪一步；作者认为的痛点是真实痛点还是为了立靶子夸大；"
        "这个问题不被解决会有什么实际后果。每条都要落到原文的具体描述或数字上。",
    ),
    (
        "3. 方法与思路",
        "按「直觉 → 形式化 → 实现」三层讲：先用日常语言讲核心 idea 的直觉；"
        "再给关键公式（LaTeX，逐个符号解释含义并配一句人话翻译）；最后说工程上怎么落地。"
        "用文字箭头或表格画出数据流 / 模块依赖。"
        "明确指出哪一步是「必须这样设计」的，哪一步是「作者随手选的」。读者没学过的概念先一句话补背景。",
    ),
    (
        "4. 创新点的真伪甄别",
        "逐条列出作者宣称的贡献，每条后面标注 [真创新 / 增量改进 / 已有工作的重新组合 / 包装]，"
        "并给出判断依据（和哪篇已有工作比、差别到底有多大）。",
    ),
    (
        "5. 实验与证据",
        "实验设计能否支撑结论；有没有缺关键 baseline、缺消融、缺极端场景；"
        "数字提升是否在噪声范围内、统计显著性如何；"
        "把「实验直接测出来的」和「作者推断出来的」分开列。",
    ),
    (
        "6. 局限与不足",
        "分三类写：a) 作者自己承认的；b) 作者没提但你能看出来的（方法论缺陷、假设过强、适用边界窄）；"
        "c) 潜在的致命问题（如果存在，直接说，不要客气）。不要写「未来可以扩展到更多数据集」这种套话。",
    ),
    (
        "7. 值得跟进的方向",
        "给 3~5 条，每条包含：具体做什么、为什么现在能做、大概难度、"
        "以及它是「论文的自然延伸」还是「换一个角度重做」。",
    ),
    (
        "8. 不确定的地方",
        "凡是原文没写清、或你需要猜的，单独列出来，标注置信度（高/中/低）。宁可说不知道，不要编。",
    ),
]


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


def _write_section(title: str, brief: str, head: str, previous: str) -> str:
    """写一节。previous 是已经写好的前文，用来避免重复。"""
    user = f"{head}\n\n---\n现在只写「{title}」这一节。\n本节要写的内容：{brief}\n"
    user += f"篇幅不少于 {MIN_CHARS} 字。只输出这一节，用「## {title}」开头，不要写别的节。"
    if previous:
        user += f"\n\n前文摘要（不要重复，直接接着写）：\n{previous[-500:]}"
    body = llm.chat(
        [{"role": "system", "content": ROLE}, {"role": "user", "content": user}],
        timeout=CALL_TIMEOUT,
        max_tokens=MAX_TOKENS,
    ).strip()
    if body.startswith("##"):
        return body
    return f"## {title}\n\n{body}"


def summarize(sections, title: str = "", max_chars: int = 7000, progress=None) -> str:
    """逐节生成八节深度拆解，返回 Markdown。progress(done, total) 可选，用于回传进度。"""
    ctx = build_context(sections, max_chars)
    if not ctx.strip():
        raise ValueError("没有可用正文，无法总结")

    head = f"论文标题：{title or '未知'}\n\n正文节选：\n{ctx}"
    out: list[str] = []
    for i, (sec_title, brief) in enumerate(SECTIONS, start=1):
        out.append(_write_section(sec_title, brief, head, "\n".join(out)))
        if progress:
            progress(i, len(SECTIONS))
    return "\n\n".join(out)
