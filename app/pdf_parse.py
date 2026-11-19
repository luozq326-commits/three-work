"""PDF 解析：文本抽取 → 版面重排 → 章节切分 → 语义单元。

两个解析后端：

- ``layout``（默认）：借用 PyMuPDF 2.0 自带的文档版面分析（ONNX，能力对标 MinerU），
  由它给出阅读顺序、段落合并、标题层级和表格结构，输出 markdown 后再转回纯文本。
  标题层级来自版面模型，不用正则猜，误判率远低于纯启发式。
- ``blocks``：纯 PyMuPDF 文本块 + 中线分栏启发式，作为 pymupdf4llm 不可用时的兜底。

扫描版（无文本层）返回 needs_ocr=True。
"""

import re
from dataclasses import dataclass, field

import pymupdf

try:  # PyMuPDF 2.0 的 markdown 转换器，可选
    import pymupdf4llm

    _HAS_4LLM = True
except ImportError:  # pragma: no cover
    pymupdf4llm = None
    _HAS_4LLM = False


# 章节标题匹配。顺序即优先级，先匹配到的生效。
# experiment 必须排在 method 之前，否则 "6.2 Model Variations" 会被 "model" 抢走。
SECTION_RULES = [
    ("references", r"^(references|bibliography|参考文献)$"),
    ("appendix", r"^(appendix|appendices)\b|^[A-Z]\.\s+\w"),
    ("abstract", r"^(abstract|summary|摘要)\b"),
    ("introduction", r"^(\d+\.?\s*)?(introduction|背景|引言)\b"),
    (
        "conclusion",
        r"^(\d+(\.\d+)?\.?\s*)?(conclusion|conclusions|concluding|summary and|limitation)\b",
    ),
    ("acknowledg", r"^acknowledg(e)?ments?\b"),
    (
        "experiment",
        r"^(\d+(\.\d+)?\.?\s*)?(experiment|experiments|experimental|evaluation|results|"
        r"ablation|analysis|comparison|benchmark|variation)\b",
    ),
    ("discussion", r"^(\d+(\.\d+)?\.?\s*)?(discussion)\b"),
    (
        "related",
        r"^(\d+(\.\d+)?\.?\s*)?(related work|related works|background|literature review|prior work)\b",
    ),
    (
        "method",
        r"^(\d+(\.\d+)?\.?\s*)?(method|methods|methodology|approach|proposed|model|architecture|"
        r"framework|implementation|training|preliminar|why)\b",
    ),
]

# 无标题层级时的强特征标题：宁可漏判，也不要误判正文里的 "model. The existence of..."
STRONG_HEADINGS = re.compile(
    r"^(references|bibliography|acknowledg(e)?ments?|附录|参考文献)$", re.IGNORECASE
)
# 和正文黏在一起的参考文献标题，如 "... inspiration. References [1] Jimmy Lei Ba ..."
_INLINE_REFS = re.compile(r"(references|bibliography|参考文献)\s*(?=\[\d+\])", re.IGNORECASE)
SECTION_RULES = [(name, re.compile(p, re.IGNORECASE)) for name, p in SECTION_RULES]

# markdown 行内标记：**粗体**、*斜体*、`代码`
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")
# 表格分隔行 |---|---|
_MD_TABLE_RULE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")
# 图内文字块（版面模型把示意图里的标注单独圈出来）
_PICTURE_BLOCK = re.compile(r"<!--\s*(Start|End) of picture text\s*-->")


@dataclass
class Section:
    kind: str          # abstract / method / conclusion ...
    title: str         # 原始标题文本
    page: int          # 起始页码，从 1 开始
    text: str
    level: int = 2     # 标题层级，1=一级标题，2=二级，0=无标题（front matter）

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class ParsedPaper:
    path: str
    page_count: int
    needs_ocr: bool
    sections: list[Section] = field(default_factory=list)
    raw_text: str = ""
    backend: str = "layout"

    def section(self, kind: str) -> Section | None:
        for s in self.sections:
            if s.kind == kind:
                return s
        return None

    def outline(self) -> list[tuple[str, str, int]]:
        return [(s.kind, s.title, s.char_count) for s in self.sections]

    def body(self, kinds: tuple[str, ...] | None = None) -> str:
        """按章节拼回正文，用于翻译和抽取。"""
        picked = [s for s in self.sections if kinds is None or s.kind in kinds]
        return "\n\n".join(s.text.strip() for s in picked if s.text.strip())


# ---------------------------------------------------------------- blocks 后端


def _page_text(page) -> str:
    """单页取文本。双栏页按左栏读完再读右栏重排，避免跨栏串行。

    这是兜底路径：只看块的位置，不看内容，遇到三栏或图文混排会出错。
    """
    width = page.rect.width
    blocks = [b for b in page.get_text("blocks") if b[4].strip()]
    if not blocks:
        return ""

    # 存在横跨中线的块 → 判定为单栏版式（标题、通栏图注等）
    spanning = any(b[0] < width * 0.45 and b[2] > width * 0.55 for b in blocks)

    if spanning or len(blocks) < 4:
        blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
    else:
        mid = width / 2
        left = [b for b in blocks if (b[0] + b[2]) / 2 < mid]
        right = [b for b in blocks if (b[0] + b[2]) / 2 >= mid]
        left.sort(key=lambda b: b[1])
        right.sort(key=lambda b: b[1])
        blocks = left + right

    return "\n".join(b[4].strip() for b in blocks)


# ---------------------------------------------------------------- layout 后端


def _md_to_lines(md: str) -> list[tuple[int, str]]:
    """markdown → [(标题层级, 纯文本)]。

    层级 0 表示正文。表格分隔行、图内文字块、强调标记都在这里清掉，
    保证下游拿到的 section.text 是干净散文——evidence 引用要能精确匹配原文。
    """
    lines: list[tuple[int, str]] = []
    in_picture = False
    for raw in md.split("\n"):
        line = raw.rstrip()
        marker = _PICTURE_BLOCK.search(line)
        if marker:
            in_picture = marker.group(1) == "Start"
            continue
        if in_picture:
            continue
        if not line.strip():
            lines.append((0, ""))
            continue
        if _MD_TABLE_RULE.match(line.strip()):
            continue

        level = 0
        stripped = line.lstrip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            level = min(hashes, 3)
            stripped = stripped[hashes:].strip()
        elif stripped.startswith("|"):
            # 表格单元格 | a | b | → a b，避免竖线进正文
            stripped = " ".join(c.strip() for c in stripped.strip("|").split("|") if c.strip())
            if not stripped:
                continue

        text = _MD_EMPHASIS.sub("", stripped).strip()
        if text:
            lines.append((level, text))
    return lines


def _md_pages(doc) -> list[str]:
    """整篇转 markdown 后按页返回。一次转换比逐页转换快一个数量级。"""
    pymupdf4llm.use_layout(True)
    chunks = pymupdf4llm.to_markdown(doc, page_chunks=True)
    pages = [c.get("text", "") if isinstance(c, dict) else str(c) for c in chunks]
    # 页数与文档对不上时，宁可当成一页，也不要错位
    return pages if len(pages) == doc.page_count else ["\n".join(pages)]


# ---------------------------------------------------------------- 章节切分


def _match_heading(line: str) -> tuple[str, str] | None:
    """判断一行是否为章节标题（无标题层级信息时的兜底）。"""
    s = line.strip()
    if not s or len(s) > 60:
        return None
    if s.endswith((".", ",", ";", ":")) and len(s) > 20:
        return None
    for kind, pattern in SECTION_RULES:
        if pattern.match(s):
            return kind, s
    return None


def split_sections(pages: list[str]) -> list[Section]:
    """按标题把全文切成章节。未命中标题的内容归入上一个章节。"""
    sections: list[Section] = []
    current = Section(kind="front", title="(front matter)", page=1, text="")

    for page_no, text in enumerate(pages, start=1):
        for line in text.split("\n"):
            hit = _match_heading(line)
            if hit:
                if current.text.strip():
                    sections.append(current)
                current = Section(kind=hit[0], title=hit[1], page=page_no, text="")
            else:
                current.text += line + "\n"

    if current.text.strip():
        sections.append(current)
    return sections


_EXPERIMENT_HINT = re.compile(
    r"\b(ablation|variation|analys[ie]s|comparison|evaluat|results?|experiment)\b", re.IGNORECASE
)


def _classify(title: str) -> str | None:
    """标题 → 章节类型。认不出返回 None。

    实验/分析类关键词出现在标题任意位置时优先判为 experiment，
    否则 "6.2 Model Variations" 会被 method 的 "model" 抢走。
    """
    t = title.strip()
    if _EXPERIMENT_HINT.search(t):
        return "experiment"
    for kind, pattern in SECTION_RULES:
        if pattern.match(t):
            return kind
    return None


def split_sections_md(pages_md: list[str]) -> list[Section]:
    """用版面模型给出的标题层级切章节。

    规则：
      - 一级标题：能认出章节类型才算章节，否则当论文大标题丢掉
      - 二级标题：一律切新章节，认不出类型记为 other（不丢内容）
      - 三级及以下：当正文里的子标题保留，不切分，避免 3.1/3.2 把方法章撕碎
    """
    sections: list[Section] = []
    current = Section(kind="front", title="(front matter)", page=1, text="", level=0)
    saw_heading = False

    def flush(new: Section) -> None:
        nonlocal current
        if current.text.strip():
            sections.append(current)
        current = new

    for page_no, md in enumerate(pages_md, start=1):
        for level, text in _md_to_lines(md):
            if not text:
                if current.text and not current.text.endswith("\n"):
                    current.text += "\n"
                continue

            if level == 1:
                kind = _classify(text)
                if kind:
                    saw_heading = True
                    flush(Section(kind=kind, title=text, page=page_no, text="", level=1))
                continue  # 论文大标题

            if level == 2:
                saw_heading = True
                kind = _classify(text) or "other"
                flush(Section(kind=kind, title=text, page=page_no, text="", level=2))
                continue

            if level >= 3:
                # 子标题：强特征（References 等）才切，其余留在正文里
                if STRONG_HEADINGS.match(text.strip()):
                    saw_heading = True
                    kind = "references" if re.match(
                        r"^(references|bibliography|参考文献)$", text.strip(), re.I
                    ) else "acknowledg"
                    flush(Section(kind=kind, title=text.strip(), page=page_no, text="", level=3))
                else:
                    current.text += f"\n{text}\n"
                continue

            # 正文行：补漏判的 References / Acknowledgements
            stripped = text.strip()
            if STRONG_HEADINGS.match(stripped):
                saw_heading = True
                kind = "references" if re.match(
                    r"^(references|bibliography|参考文献)$", stripped, re.I
                ) else "acknowledg"
                flush(Section(kind=kind, title=stripped, page=page_no, text="", level=2))
                continue
            # 版面模型常把 "…inspiration. References [1] Jimmy Lei Ba, …" 塞进同一段
            inline = _INLINE_REFS.search(stripped)
            if inline:
                before = stripped[: inline.start()].strip()
                after = stripped[inline.end():].strip()
                saw_heading = True
                if before:
                    current.text += before + "\n"
                flush(
                    Section(kind="references", title=inline.group(1), page=page_no,
                            text=(after + "\n") if after else "", level=2)
                )
                continue
            current.text += text + "\n"

    if current.text.strip():
        sections.append(current)

    # 认不出名字的方法章（如 "3. Deep Residual Learning"）：紧接实验/结论章时按 method 记
    for i, s in enumerate(sections):
        if s.kind != "other":
            continue
        nxt = sections[i + 1].kind if i + 1 < len(sections) else None
        if nxt in ("experiment", "conclusion"):
            s.kind = "method"

    # 版面模型没识别出任何章节 → 退回正则
    if not saw_heading:
        plain = ["\n".join(t for _, t in _md_to_lines(md)) for md in pages_md]
        return split_sections(plain)
    return sections


# ---------------------------------------------------------------- 入口


def _text_layer_ok(doc) -> bool:
    """平均每页不足 100 字符，基本可判定是扫描件。"""
    sample = doc[: min(3, doc.page_count)]
    chars = sum(len(p.get_text("text")) for p in sample)
    return bool(sample) and chars / len(sample) >= 100


def parse_pdf(path: str, backend: str = "auto") -> ParsedPaper:
    """解析一篇 PDF。

    backend:
      "auto"   —— 有 pymupdf4llm 就用 layout，否则用 blocks
      "layout" —— 强制版面模型（慢约 0.7s/页，质量最好）
      "blocks" —— 强制文本块启发式（快，双栏可能串行）
    """
    doc = pymupdf.open(path)
    try:
        page_count = doc.page_count
        if not _text_layer_ok(doc):
            return ParsedPaper(
                path=path, page_count=page_count, needs_ocr=True, backend="none"
            )

        use_layout = backend == "layout" or (backend == "auto" and _HAS_4LLM)
        if use_layout:
            try:
                pages_md = _md_pages(doc)
                sections = split_sections_md(pages_md)
                raw = "\n".join(
                    "\n".join(t for _, t in _md_to_lines(md)) for md in pages_md
                )
                return ParsedPaper(
                    path=path,
                    page_count=page_count,
                    needs_ocr=False,
                    sections=sections,
                    raw_text=raw,
                    backend="layout",
                )
            except Exception:
                # 版面模型缺 ONNX 运行时等情况，回落到 blocks，不让整条链路挂掉
                if backend == "layout":
                    raise
        pages = [_page_text(p) for p in doc]
        return ParsedPaper(
            path=path,
            page_count=page_count,
            needs_ocr=False,
            sections=split_sections(pages),
            raw_text="\n".join(pages),
            backend="blocks",
        )
    finally:
        doc.close()


def split_chunks(text: str, max_chars: int) -> list[str]:
    """按行合并成块，块大小不超过 max_chars。用于长文分块翻译。"""
    chunks, buf = [], ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(buf) + len(line) + 1 > max_chars and buf:
            chunks.append(buf.strip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf.strip())
    return chunks
