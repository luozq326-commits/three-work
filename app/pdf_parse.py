"""PDF 解析：文本抽取 → 版面重排 → 章节切分 → 语义单元。

两个解析后端：

- ``layout``（默认）：借用 PyMuPDF 2.0 自带的文档版面分析（ONNX，能力对标 MinerU），
  由它给出阅读顺序、段落合并、标题层级和表格结构，输出 markdown 后再转回纯文本。
  标题层级来自版面模型，不用正则猜，误判率远低于纯启发式。
- ``blocks``：纯 PyMuPDF 文本块 + 中线分栏启发式，作为 pymupdf4llm 不可用时的兜底。

扫描版（无文本层）返回 needs_ocr=True。
"""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import config

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
    paras: list[dict] = field(default_factory=list)  # [{page, text}]，段落级对齐用
    tables: list[dict] = field(default_factory=list)  # [{page, rows: [[cell...]]}]
    # 版面顺序的内容流：text / table / figure / formula 混排，用于把图表插回正文原位
    # text 项只存 paras 下标（{"type":"text","para":0}），翻译结果按 paras 回填
    flow: list[dict] = field(default_factory=list)

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
    ocr_used: bool = False
    # 页码 → 图片路径列表（论文里的插图）；页码 → 整页快照（矢量图、图表混排靠它兜底）
    figures: dict[int, list[str]] = field(default_factory=dict)
    page_images: dict[int, str] = field(default_factory=dict)
    captions: dict[str, str] = field(default_factory=dict)  # 图片路径 → 图注

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


def _md_items(md: str) -> list[tuple[str, int, object]]:
    """markdown → [("h"|"p", 层级, 文本) | ("table", 0, [[单元格...]])]。

    表格单独成 token：它要按原样渲染成表格，不能拍平成一行字再去翻译。
    图内文字块（示意图标注）整块丢掉——那是图里的字，翻出来只会污染正文。
    """
    items: list[tuple[str, int, object]] = []
    in_picture = False
    table_buf: list[list[str]] = []

    def flush_table() -> None:
        nonlocal table_buf
        if len(table_buf) >= 2:
            items.append(("table", 0, table_buf))
        elif table_buf:
            items.append(("p", 0, " ".join(" ".join(r) for r in table_buf)))
        table_buf = []

    for raw in md.split("\n"):
        line = raw.rstrip()
        marker = _PICTURE_BLOCK.search(line)
        if marker:
            flush_table()
            in_picture = marker.group(1) == "Start"
            continue
        if in_picture:
            continue

        stripped = line.strip()
        if stripped.startswith("|"):
            if _MD_TABLE_RULE.match(stripped):
                continue
            table_buf.append([c.strip() for c in stripped.strip("|").split("|")])
            continue
        flush_table()

        if not stripped:
            items.append(("p", 0, ""))
            continue

        level = 0
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            level = min(hashes, 3)
            stripped = stripped[hashes:].strip()

        text = _MD_EMPHASIS.sub("", stripped).strip()
        if text:
            items.append(("h" if level else "p", level, text))
    flush_table()
    return items


def _md_to_lines(md: str) -> list[tuple[int, str]]:
    """markdown → [(标题层级, 纯文本)]，表格拍平成一行。用于 raw_text 与兜底切分。"""
    out: list[tuple[int, str]] = []
    for kind, level, payload in _md_items(md):
        if kind == "table":
            rows = payload
            out.append((0, " ".join(" ".join(r) for r in rows)))
        else:
            out.append((level, payload))
    return out


def _md_chunks(doc) -> list:
    """整篇转 markdown，按页返回原始 chunk（含 page_boxes，带每个版面块的 class 与 bbox）。"""
    pymupdf4llm.use_layout(True)
    chunks = pymupdf4llm.to_markdown(doc, page_chunks=True)
    return [c if isinstance(c, dict) else {"text": str(c)} for c in chunks]


def _md_pages(doc) -> list[str]:
    """整篇转 markdown 后按页返回。一次转换比逐页转换快一个数量级。"""
    chunks = _md_chunks(doc)
    pages = [c.get("text", "") for c in chunks]
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
    r"\b(ablations?|variations?|analys[ie]s|comparisons?|evaluations?|results?|experiments?)\b",
    re.IGNORECASE,
)
# 方法章常常起个没关键词的名字（"2 CONVNET CONFIGURATIONS"），靠这几个词兜住
_METHOD_HINT = re.compile(
    r"\b(configurations?|frameworks?|architectures?|networks?|methods?|methodology|approach|"
    r"implementation|training|models?|design)\b",
    re.IGNORECASE,
)


def _classify(title: str) -> str | None:
    """标题 → 章节类型。认不出返回 None。

    判定顺序：参考文献 → 实验/分析关键词（任意位置）→ 锚定规则 → 方法关键词（任意位置）。
    实验必须早于方法，否则 "6.2 Model Variations" 会被 method 的 "model" 抢走。
    """
    t = title.strip()
    if re.match(r"^(references|bibliography|参考文献)$", t, re.IGNORECASE):
        return "references"
    if _EXPERIMENT_HINT.search(t):
        return "experiment"
    for kind, pattern in SECTION_RULES:
        if pattern.match(t):
            return kind
    if _METHOD_HINT.search(t):
        return "method"
    return None


def _clean_inline_md(t: str) -> str:
    """去掉段落里的行内 HTML 标记。

    pymupdf4llm 会把脚注、上标转成 <sup>1</sup> 之类的标签，
    直接送进翻译会在中文里漏出 "<sup>1</sup>"，读起来像乱码。
    """
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]{1,20}>", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def split_sections_md(pages_md: list[str]) -> list[Section]:
    """用版面模型给出的标题层级切章节，同时攒出带页码的段落。

    规则：
      - 一级标题：能认出章节类型才算章节，否则当论文大标题丢掉
      - 二级标题：一律切新章节，认不出类型记为 other（不丢内容）
      - 三级及以下：当正文里的子标题保留，不切分，避免 3.1/3.2 把方法章撕碎

    段落按空行切，记录段落起始页——UI 要能"点译文跳到原文第几页"。
    """
    sections: list[Section] = []
    current = Section(kind="front", title="(front matter)", page=1, text="", level=0)
    saw_heading = False
    buf: list[str] = []
    buf_page = 1

    def flush_par() -> None:
        nonlocal buf
        if buf:
            t = _clean_inline_md("\n".join(buf).strip())
            if t:
                current.paras.append({"page": buf_page, "text": t})
            buf = []

    def add_line(text: str, page_no: int) -> None:
        nonlocal buf_page
        if not buf:
            buf_page = page_no
        buf.append(text)

    def flush_sec(new: Section) -> None:
        nonlocal current
        flush_par()
        if current.text.strip():
            sections.append(current)
        current = new

    for page_no, md in enumerate(pages_md, start=1):
        for kind, level, payload in _md_items(md):
            if kind == "table":
                # 表格只记录结构，不进正文：翻译表格会把数字和表头搅乱
                current.tables.append({"page": page_no, "rows": payload})
                continue
            text = payload
            if not text:
                flush_par()
                if current.text and not current.text.endswith("\n"):
                    current.text += "\n"
                continue

            if level == 1:
                kind = _classify(text)
                if kind:
                    saw_heading = True
                    flush_sec(Section(kind=kind, title=text, page=page_no, text="", level=1))
                continue  # 论文大标题

            if level == 2:
                saw_heading = True
                kind = _classify(text) or "other"
                flush_sec(Section(kind=kind, title=text, page=page_no, text="", level=2))
                continue

            if level >= 3:
                # 子标题：强特征（References 等）才切，其余留在正文里
                if STRONG_HEADINGS.match(text.strip()):
                    saw_heading = True
                    kind = "references" if re.match(
                        r"^(references|bibliography|参考文献)$", text.strip(), re.I
                    ) else "acknowledg"
                    flush_sec(Section(kind=kind, title=text.strip(), page=page_no, text="", level=3))
                else:
                    add_line(text, page_no)
                    current.text += f"\n{text}\n"
                continue

            # 正文行：补漏判的 References / Acknowledgements
            stripped = text.strip()
            if STRONG_HEADINGS.match(stripped):
                saw_heading = True
                kind = "references" if re.match(
                    r"^(references|bibliography|参考文献)$", stripped, re.I
                ) else "acknowledg"
                flush_sec(Section(kind=kind, title=stripped, page=page_no, text="", level=2))
                continue
            # 版面模型常把 "…inspiration. References [1] Jimmy Lei Ba, …" 塞进同一段
            inline = _INLINE_REFS.search(stripped)
            if inline:
                before = stripped[: inline.start()].strip()
                after = stripped[inline.end():].strip()
                saw_heading = True
                if before:
                    add_line(before, page_no)
                    current.text += before + "\n"
                flush_sec(
                    Section(kind="references", title=inline.group(1), page=page_no, text="", level=2)
                )
                if after:
                    add_line(after, page_no)
                    current.text += after + "\n"
                    flush_par()
                continue
            add_line(text, page_no)
            current.text += text + "\n"

    flush_par()
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


def ocr_available() -> bool:
    """有没有可用的 OCR 引擎。兼容 rapidocr_onnxruntime (1.x) 与 rapidocr (3.x)。"""
    for mod in ("rapidocr_onnxruntime", "rapidocr"):
        try:
            __import__(mod)
            return True
        except ImportError:
            continue
    return False


_OCR_ENGINE = None


# ---------------------------------------------------------------- MinerU 后端
#
# MinerU 4.x 装在独立 venv（torch/transformers 与主环境冲突），用子进程调用。
# 它给出：阅读顺序、段落/标题层级、表格 HTML（含合并单元格）、公式 LaTeX、图片 bbox。
# 代价是慢（12 页约 36s），所以做成可选后端，默认仍走 PyMuPDF。

MINERU_VENV = config.ROOT / ".venv-mineru"
MINERU_BRIDGE = config.ROOT / "tools" / "mineru_bridge.py"
# 模型放 E 盘，别塞 C 盘用户目录（MinerU 默认 ~/.mineru，834MB）
# 也不能放仓库里：单个 onnx 591MB，GitHub 直接拒收
MODEL_ROOT = os.environ.get("PAPERREADER_MODELS") or "E:/模型"
MINERU_HOME = os.path.join(MODEL_ROOT, "mineru")
MINERU_ENV = {
    "MINERU_MODEL_SOURCE": "modelscope",
    "MINERU_HOME": MINERU_HOME,
    "MODELSCOPE_CACHE": os.path.join(MINERU_HOME, "hub"),
    "HF_HOME": os.path.join(MODEL_ROOT, "hf"),
}


def mineru_available() -> bool:
    py = MINERU_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return py.exists() and MINERU_BRIDGE.exists()


def _mineru_blocks(pdf_path: str) -> dict:
    """调子进程跑 MinerU，返回 {pages, cost_s, blocks}。"""
    py = MINERU_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    out = config.DATA_DIR / "media" / "_mineru" / f"{_pdf_digest(pdf_path)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(MINERU_ENV)
    proc = subprocess.run(
        [str(py), str(MINERU_BRIDGE), str(pdf_path), "--out", str(out)],
        capture_output=True,
        text=True,
        env=env,
        timeout=1800,
    )
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"MinerU 解析失败：{(proc.stderr or proc.stdout)[-400:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def _html_rows(html: str) -> list[list[str]]:
    """表格 HTML → 二维文本，供界面在拿不到富文本时降级渲染。"""
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        rows.append([re.sub(r"<[^>]+>", "", c).strip() for c in cells])
    return rows


def _title_hashes(text: str, level: int) -> str:
    """章节标题 → markdown 井号（只用于 paragraph_title）。

    MinerU 给的 level 不稳定（3.1 这种子标题也常标成 2），所以优先看编号深度：
    一级章节给 "##"（切新章节），子标题给 "###"（留在正文里，不切碎）。
    注意不能给 "#"——"#" 在我们这边是论文大标题，认不出类型会被丢掉。
    """
    m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+\S", text.strip())
    if m:
        return "##" if len(m.group(1).split(".")) <= 1 else "###"
    # 认得出类型的标题（Abstract / References / 无编号的 Conclusion）一律切章节，
    # 否则 MinerU 给的 level 会把它们压成子标题，摘要就并进题名页了
    if _classify(text):
        return "##"
    return "##" if int(level or 2) <= 1 else "###"


def _sections_from_mineru(blocks: list[dict]) -> list[Section]:
    """MinerU 块 → Section 列表，同时保留版面顺序的 flow。

    和 md 路径的区别：这里不走"markdown → 正则切章"，而是直接按块建章节，
    这样表格、插图、公式都能带着 bbox 留在原来的位置（flow），而不是堆到章节末尾。
    """
    sections: list[Section] = []
    current = Section(kind="front", title="(front matter)", page=1, text="", level=0)
    in_refs = False
    in_abstract = False

    def close() -> None:
        nonlocal current
        if current.paras or current.tables or current.flow:
            sections.append(current)

    def add_para(text: str, page: int, bbox: list) -> None:
        current.paras.append({"page": page, "text": text, "bbox": bbox})
        current.flow.append({"type": "text", "para": len(current.paras) - 1})
        current.text += text + "\n"

    for b in blocks:
        pno = b["page"]
        btype = b["type"]
        text = (b.get("text") or "").strip()
        bbox = b.get("bbox") or []

        if btype in ("page_number", "page_footnote", "aside_text", "doc_title"):
            continue  # 页码/脚注/页边文字/论文大标题都不进正文

        if btype == "ref_text":
            if not in_refs:
                close()
                current = Section(kind="references", title="References", page=pno, text="", level=2)
                in_refs = True
            if text:
                add_para(text, pno, bbox)
            continue

        if btype == "paragraph_title":
            if _title_hashes(text, b.get("level") or 2) == "##":
                close()
                current = Section(
                    kind=_classify(text) or "other", title=text, page=pno, text="", level=2
                )
            else:
                add_para(text, pno, bbox)  # 子标题留在正文里，不切碎章节
            continue

        if btype == "text":
            # MinerU 常把 Abstract 当普通正文，这里补一个章节，否则摘要并进题名页
            if not in_abstract and re.match(r"^abstract\b[:.\s]", text, re.IGNORECASE):
                in_abstract = True
                close()
                current = Section(kind="abstract", title="Abstract", page=pno, text="", level=2)
                text = re.sub(r"^abstract\b[:.\s]*", "", text, flags=re.IGNORECASE).strip()
                if not text:
                    continue
            if text:
                add_para(text, pno, bbox)
            continue

        if btype == "table":
            html = b.get("html") or ""
            rows = _html_rows(html)
            current.tables.append({"page": pno, "rows": rows, "html": html})
            current.flow.append(
                {"type": "table", "page": pno, "bbox": bbox, "html": html, "rows": rows}
            )
            continue

        if btype in ("image", "chart"):
            current.flow.append(
                {"type": "figure", "page": pno, "bbox": bbox, "caption": b.get("caption") or ""}
            )
            continue

        if btype == "equation":
            # 公式只存 LaTeX 与位置，不送翻译（送进去容易被模型改写）
            current.flow.append({"type": "formula", "page": pno, "bbox": bbox, "latex": text})
            continue

    close()

    # 认不出名字的方法章（如 "3. Deep Residual Learning"）：紧接实验/结论时按 method 记
    for i, s in enumerate(sections):
        if s.kind == "other" and i + 1 < len(sections) and sections[i + 1].kind in (
            "experiment",
            "conclusion",
        ):
            s.kind = "method"
    return sections


def _crop_bbox(doc, base, page_no: int, bbox: list, name: str, dpi: int = 170,
               min_w: float = 40, min_h: float = 24) -> str:
    """按归一化 bbox（0~1）从原页裁一块图出来，返回路径；失败返回空串。"""
    if not (1 <= page_no <= doc.page_count) or len(bbox) != 4:
        return ""
    page = doc[page_no - 1]
    x0, y0, x1, y1 = bbox
    rect = pymupdf.Rect(
        x0 * page.rect.width, y0 * page.rect.height,
        x1 * page.rect.width, y1 * page.rect.height,
    )
    if rect.width < min_w or rect.height < min_h:
        return ""
    fp = base / name
    try:
        page.get_pixmap(clip=rect, dpi=dpi).save(fp)
        return str(fp)
    except Exception:
        return ""


def _crop_rect_abs(doc, base, page_no: int, bbox: list, name: str, dpi: int = 170,
                   min_w: float = 40, min_h: float = 24) -> str:
    """按绝对坐标（PDF 点）裁剪。pymupdf4llm 的 page_boxes 用的是绝对坐标。"""
    if not (1 <= page_no <= doc.page_count) or len(bbox) != 4:
        return ""
    page = doc[page_no - 1]
    rect = pymupdf.Rect(*bbox)
    if rect.width < min_w or rect.height < min_h:
        return ""
    fp = base / name
    try:
        page.get_pixmap(clip=rect, dpi=dpi).save(fp)
        return str(fp)
    except Exception:
        return ""


# 快速档：pymupdf4llm 的版面块分类 → 我们的 flow 类型
_TEXT_BOX_CLASSES = {"text", "title", "section-header", "list-item", "caption", "footnote"}
_MEDIA_BOX_CLASSES = {"picture": "figure", "table": "table", "formula": "formula"}


def _norm_text(s: str) -> str:
    """归一化：只留小写字母数字，用于段落与版面块的前缀匹配。"""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _layout_flow(paper: ParsedPaper, doc, chunks: list, sections: list[Section]) -> None:
    """快速档也把表格/插图/公式放回原位。

    段落文字来自 markdown（质量好），位置来自 page_boxes（有 bbox）：
    先用 page.get_textbox 反查每个 box 的文字，再按归一化前缀把 box 匹配回段落。
    匹配不上就留空 bbox，排序时落到该页末尾——宁可位置不准，也不要错位。
    """
    base = config.DATA_DIR / "media" / _pdf_digest(paper.path)
    base.mkdir(parents=True, exist_ok=True)

    boxes_by_page: dict[int, list] = {}
    texts_by_page: dict[int, list[str]] = {}
    for i, c in enumerate(chunks, start=1):
        boxes = c.get("page_boxes") or []
        boxes_by_page[i] = boxes
        page = doc[i - 1] if i <= doc.page_count else None
        texts: list[str] = []
        for b in boxes:
            bb = b.get("bbox") or []
            t = ""
            if page is not None and len(bb) == 4 and b.get("class") in _TEXT_BOX_CLASSES:
                try:
                    t = _norm_text(page.get_textbox(pymupdf.Rect(bb)))[:120]
                except Exception:
                    t = ""
            texts.append(t)
        texts_by_page[i] = texts

    # 1) 给每个段落找 bbox
    for s in sections:
        for p in s.paras:
            pno = p.get("page") or s.page
            target = _norm_text(p.get("text"))[:120]
            if not target:
                continue
            best, score = None, 0.0
            for b, cand in zip(boxes_by_page.get(pno) or [], texts_by_page.get(pno) or []):
                if not cand:
                    continue
                n = 0
                for x, y in zip(target, cand):
                    if x != y:
                        break
                    n += 1
                sc = n / max(len(target), 1)
                if sc > score:
                    best, score = b.get("bbox"), sc
            if score >= 0.5:
                p["bbox"] = best

    # 2) 收集所有媒体块，按 (页, y) 归属到"起始位置在它之前的最后一个章节"
    #    不能按页区间分配给每个章节：同一页上常有多个章节，会重复展示同一张图
    media: list[dict] = []
    for pno, boxes in boxes_by_page.items():
        for b in boxes:
            t = _MEDIA_BOX_CLASSES.get(b.get("class"))
            bb = b.get("bbox") or []
            if not t or len(bb) != 4:
                continue
            # 公式框又扁又窄，用更小的尺寸门槛和更高的 dpi，否则会被当成噪点丢掉
            kw = (
                {"dpi": 220, "min_w": 20, "min_h": 8}
                if t == "formula"
                else {}
            )
            fp = _crop_rect_abs(doc, base, pno, bb, f"{t}_p{pno}_{len(media)}.png", **kw)
            if not fp:
                continue  # 裁不出图（太小/越界）就不展示，避免空白占位
            media.append({"type": t, "page": pno, "bbox": bb, "path": fp})

    starts: list[tuple[tuple[int, float], int]] = []
    for idx, s in enumerate(sections):
        first_y = None
        for p in s.paras:
            bb = p.get("bbox") or []
            if len(bb) == 4:
                first_y = bb[1]
                break
        starts.append(((s.page, first_y if first_y is not None else 0.0), idx))

    buckets: dict[int, list[dict]] = {i: [] for i in range(len(sections))}
    for m in media:
        pos = (m["page"], m["bbox"][1])
        owner = None
        for start, idx in starts:
            if start <= pos:
                owner = idx
            else:
                break
        if owner is not None:
            buckets[owner].append(m)

    # 3) 每章按 (页, y, x) 排序
    for idx, s in enumerate(sections):
        items = [
            {"type": "text", "para": i, "page": p.get("page"), "bbox": p.get("bbox") or []}
            for i, p in enumerate(s.paras)
        ]
        items.extend(buckets[idx])

        def _key(it: dict) -> tuple:
            bb = it.get("bbox") or []
            return (
                it.get("page") or 0,
                bb[1] if len(bb) == 4 else 1e9,
                bb[0] if len(bb) == 4 else 0,
            )

        items.sort(key=_key)
        s.flow = items


def parse_with_mineru(path: str) -> ParsedPaper:
    """MinerU 解析：结构最准，但慢（约 30s/篇）。"""
    data = _mineru_blocks(path)
    sections = _sections_from_mineru(data["blocks"])
    paper = ParsedPaper(
        path=path,
        page_count=data.get("pages") or 0,
        needs_ocr=False,
        sections=sections,
        raw_text="\n".join(s.text for s in sections),
        backend="mineru",
    )

    doc = pymupdf.open(path)
    try:
        _attach_media(paper, doc, path)
        base = config.DATA_DIR / "media" / _pdf_digest(path)
        for s in sections:
            for i, item in enumerate(s.flow):
                if item["type"] == "figure":
                    fp = _crop_bbox(doc, base, item["page"], item["bbox"], f"fig_p{item['page']}_{i}.png")
                    if not fp:
                        continue
                    item["path"] = fp
                    paper.figures.setdefault(item["page"], []).append(fp)
                    if item.get("caption"):
                        paper.captions[fp] = item["caption"]
                elif item["type"] == "formula":
                    # 公式单独裁一张小图：LaTeX 渲染依赖字体，直接显示原图最忠实
                    fp = _crop_bbox(doc, base, item["page"], item["bbox"],
                                    f"eq_p{item['page']}_{i}.png", dpi=220, min_w=20, min_h=8)
                    if fp:
                        item["path"] = fp
    finally:
        doc.close()
    return paper



def _section_end_page(sections: list[Section], sec: Section) -> int:
    idx = sections.index(sec)
    return sections[idx + 1].page if idx + 1 < len(sections) else 10**6


def _pdf_digest(pdf_path: str) -> str:
    with open(pdf_path, "rb") as f:
        return hashlib.md5(f.read(4 * 1024 * 1024)).hexdigest()[:12]


def _format_ocr_items(items: list[tuple[float, float, float, float, str]]) -> str:
    """把带 bbox 的 OCR 文本行重排为自然段落：自动检测双栏（先左栏后右栏）、合并段内断行与连字符。"""
    if not items:
        return ""
    min_x = min(it[0] for it in items)
    max_x = max(it[2] for it in items)
    width = max(max_x - min_x, 1.0)
    mid_x = (min_x + max_x) / 2.0

    # 双栏判定：左右两栏各有 >=4 行，且跨过中线的长行占比很低
    left_only = [it for it in items if it[2] <= mid_x + width * 0.04]
    right_only = [it for it in items if it[0] >= mid_x - width * 0.04]
    crossing = [
        it for it in items if it[0] < mid_x - width * 0.04 and it[2] > mid_x + width * 0.04
    ]
    if len(left_only) >= 4 and len(right_only) >= 4 and len(crossing) <= max(2, len(items) // 8):
        # 顶部跨栏标题归入最前，其余按左栏 -> 右栏排
        top_y = min(min(it[1] for it in left_only), min(it[1] for it in right_only))
        top_cross = sorted([it for it in crossing if it[1] <= top_y + 10], key=lambda x: (x[1], x[0]))
        other_cross = [it for it in crossing if it[1] > top_y + 10]
        columns = [
            top_cross,
            sorted(left_only, key=lambda x: (x[1], x[0])),
            sorted(right_only + other_cross, key=lambda x: (x[1], x[0])),
        ]
    else:
        columns = [sorted(items, key=lambda x: (x[1], x[0]))]

    paras: list[str] = []
    for col in columns:
        if not col:
            continue
        heights = sorted(max(it[3] - it[1], 1.0) for it in col)
        med_h = heights[len(heights) // 2]
        col_left = min(it[0] for it in col)
        cur = ""
        prev: tuple[float, float, float, float, str] | None = None
        for it in col:
            x0, y0, x1, y1, txt = it
            txt = txt.strip()
            if not txt:
                continue
            if prev is None:
                cur = txt
                prev = it
                continue
            gap_y = y0 - prev[3]
            is_heading = bool(re.match(r"^\d+(?:\.\d+)*\.?\s+\S", txt)) and len(txt) < 80
            prev_heading = bool(re.match(r"^\d+(?:\.\d+)*\.?\s+\S", prev[4])) and len(prev[4]) < 80
            indented = (x0 - col_left) > med_h * 0.9 and prev[4].rstrip().endswith((".", "?", "!", "。", "：", ":"))
            if gap_y > med_h * 0.75 or is_heading or prev_heading or indented:
                if cur:
                    paras.append(cur)
                cur = txt
            else:
                if cur.endswith("-") and len(cur) >= 2 and cur[-2].isalpha() and txt[0].isalpha():
                    cur = cur[:-1] + txt
                elif cur and ord(cur[-1]) > 127 and ord(txt[0]) > 127:
                    cur += txt
                else:
                    cur += " " + txt
            prev = it
        if cur:
            paras.append(cur)
    return "\n\n".join(paras)


def ocr_image(path: str, lang: str = "ch") -> str:
    """对单张图片做 OCR，返回按阅读顺序分段后的纯文本。图片翻译与手机拍照通道走这条路。

    兼容 rapidocr_onnxruntime (1.x) 与 rapidocr (3.x)，支持双栏重排与段内合并。
    引擎加载要几秒，进程内复用同一个实例，否则每次拍照都要等模型初始化。
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            from rapidocr import RapidOCR
        _OCR_ENGINE = RapidOCR()

    out = _OCR_ENGINE(path)
    items: list[tuple[float, float, float, float, str]] = []
    # rapidocr 3.x 返回 RapidOCROutput 对象（含 .boxes 与 .txts）
    txts = getattr(out, "txts", None)
    boxes = getattr(out, "boxes", None)
    if txts is not None:
        if boxes is not None and len(boxes) == len(txts):
            for box, txt in zip(boxes, txts):
                s = str(txt).strip()
                if not s:
                    continue
                xs = [float(pt[0]) for pt in box]
                ys = [float(pt[1]) for pt in box]
                items.append((min(xs), min(ys), max(xs), max(ys), s))
            if items:
                return _format_ocr_items(items)
        return "\n".join(str(t).strip() for t in txts if str(t).strip())

    # rapidocr_onnxruntime 1.x 返回 (result, elapse) 元组，result 每项：[框坐标, 文本, 置信度]
    result = out[0] if isinstance(out, tuple) else out
    if not result:
        return ""
    for item in result:
        if len(item) < 2 or not str(item[1]).strip():
            continue
        box, s = item[0], str(item[1]).strip()
        try:
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            items.append((min(xs), min(ys), max(xs), max(ys), s))
        except Exception:
            continue
    if items:
        return _format_ocr_items(items)
    return "\n".join(item[1] for item in result if len(item) > 1 and str(item[1]).strip())


def _fill_paras_fallback(sections: list[Section]) -> None:
    """blocks 后端拿不到逐行页码，按空行切段、页码用章节起始页兜底。"""
    for s in sections:
        if s.paras:
            continue
        chunks = [p.strip() for p in re.split(r"\n\s*\n", s.text) if p.strip()]
        if len(chunks) <= 1:
            chunks = [p.strip() for p in s.text.split("\n") if p.strip()]
        s.paras = [{"page": s.page, "text": p} for p in chunks]


def _attach_media(paper: ParsedPaper, doc, pdf_path: str, dpi: int = 110) -> ParsedPaper:
    """把插图和整页快照落到 data/media/<pdf摘要>/ 下，并把路径挂到 ParsedPaper 上。

    两层保险：
      - 内嵌位图：真图片，能单独展示
      - 整页快照：矢量图、流程图、图表混排都靠它兜底（PDF 里的图很多不是位图）
    """
    try:
        with open(pdf_path, "rb") as f:
            digest = hashlib.md5(f.read(4 * 1024 * 1024)).hexdigest()[:12]
        base = config.DATA_DIR / "media" / digest
        base.mkdir(parents=True, exist_ok=True)

        seen_xref: set[int] = set()
        for pno, page in enumerate(doc, start=1):
            snap = base / f"page_{pno}.png"
            if not snap.exists():
                try:
                    page.get_pixmap(dpi=dpi).save(snap)
                except Exception:
                    pass
            if snap.exists():
                paper.page_images[pno] = str(snap)

            for idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                if xref in seen_xref:
                    continue
                seen_xref.add(xref)
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.width < 150 or pix.height < 120:
                        continue  # 太小基本是 logo、图标、公式碎片
                    if pix.n > 3:
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    fp = base / f"fig_p{pno}_{idx}.png"
                    pix.save(fp)
                    paper.figures.setdefault(pno, []).append(str(fp))
                except Exception:
                    continue
    except Exception:
        # 出图失败不该让整篇解析失败：正文还能读
        pass
    return paper


def parse_pdf(path: str, backend: str = "auto", ocr: bool = True) -> ParsedPaper:
    """解析一篇 PDF。

    backend:
      "auto"   —— 有 pymupdf4llm 就用 layout，否则用 blocks
      "layout" —— 强制版面模型（慢约 0.9s/页，质量最好）
      "blocks" —— 强制文本块启发式（快，双栏可能串行）
    ocr:
      True  —— 无文本层时自动走 OCR（约 30~60s/页，慢但能读扫描件）
      False —— 只标记 needs_ocr，不 OCR
    """
    if backend == "mineru":
        if not mineru_available():
            raise RuntimeError(
                "MinerU 未安装。需要 .venv-mineru 与 tools/mineru_bridge.py，"
                "或用 backend='auto' 走 PyMuPDF"
            )
        return parse_with_mineru(path)

    doc = pymupdf.open(path)
    try:
        page_count = doc.page_count
        if not _text_layer_ok(doc):
            if not (ocr and _HAS_4LLM and ocr_available()):
                return ParsedPaper(
                    path=path, page_count=page_count, needs_ocr=True, backend="none"
                )
            # 扫描件：版面模型会对图片页调用 RapidOCR
            pages_md = _md_pages(doc)
            raw = "\n".join("\n".join(t for _, t in _md_to_lines(md)) for md in pages_md)
            if not raw.strip():
                return ParsedPaper(
                    path=path, page_count=page_count, needs_ocr=True, backend="none"
                )
            paper = ParsedPaper(
                path=path,
                page_count=page_count,
                needs_ocr=True,
                sections=split_sections_md(pages_md),
                raw_text=raw,
                backend="ocr",
                ocr_used=True,
            )
            return _attach_media(paper, doc, path)

        use_layout = backend == "layout" or (backend == "auto" and _HAS_4LLM)
        if use_layout:
            try:
                chunks = _md_chunks(doc)
                pages_md = [c.get("text", "") for c in chunks]
                if len(pages_md) != doc.page_count:
                    pages_md = ["\n".join(pages_md)]
                sections = split_sections_md(pages_md)
                raw = "\n".join(
                    "\n".join(t for _, t in _md_to_lines(md)) for md in pages_md
                )
                paper = ParsedPaper(
                    path=path,
                    page_count=page_count,
                    needs_ocr=False,
                    sections=sections,
                    raw_text=raw,
                    backend="layout",
                )
                _attach_media(paper, doc, path)
                # 图表回原位：拿不到位置不该让整篇失败，退回"图表堆章节末尾"
                try:
                    _layout_flow(paper, doc, chunks, sections)
                except Exception:
                    pass
                return paper
            except Exception:
                # 版面模型缺 ONNX 运行时等情况，回落到 blocks，不让整条链路挂掉
                if backend == "layout":
                    raise
        pages = [_page_text(p) for p in doc]
        blocks_sections = split_sections(pages)
        _fill_paras_fallback(blocks_sections)
        paper = ParsedPaper(
            path=path,
            page_count=page_count,
            needs_ocr=False,
            sections=blocks_sections,
            raw_text="\n".join(pages),
            backend="blocks",
        )
        return _attach_media(paper, doc, path)
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
