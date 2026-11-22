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
            t = "\n".join(buf).strip()
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
    """有没有可用的 OCR 引擎。RapidOCR 是纯 pip 依赖，不需要装 Tesseract。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except ImportError:
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
MINERU_HOME = str(config.ROOT.parent / "模型" / "mineru")
MINERU_ENV = {
    "MINERU_MODEL_SOURCE": "modelscope",
    "MINERU_HOME": MINERU_HOME,
    "MODELSCOPE_CACHE": str(config.ROOT.parent / "模型" / "mineru" / "hub"),
    "HF_HOME": str(config.ROOT.parent / "模型" / "hf"),
}


def mineru_available() -> bool:
    py = MINERU_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return py.exists() and MINERU_BRIDGE.exists()


def _mineru_blocks(pdf_path: str) -> dict:
    """调子进程跑 MinerU，返回 {pages, cost_s, blocks}。"""
    py = MINERU_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    out = config.DATA_DIR / "media" / "_mineru" / f"{Path(pdf_path).stem}.json"
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


def _blocks_to_markdown(blocks: list[dict]) -> tuple[list[str], list[dict], list[dict]]:
    """MinerU 块 → 每页 markdown（复用现有切章逻辑）+ 表格 + 图片。"""
    pages: dict[int, list[str]] = {}
    tables: list[dict] = []
    figures: list[dict] = []
    in_refs = False
    in_abstract = False

    for b in blocks:
        pno = b["page"]
        btype = b["type"]
        text = (b.get("text") or "").strip()

        if btype in ("page_number", "page_footnote", "aside_text"):
            continue  # 页码、脚注、页边文字：进正文只会污染

        if btype == "table":
            tables.append({"page": pno, "html": b.get("html") or "", "rows": _html_rows(b.get("html") or "")})
            continue

        if btype in ("image", "chart"):
            figures.append({"page": pno, "bbox": b.get("bbox") or [], "caption": b.get("caption") or ""})
            continue

        if btype == "equation":
            continue  # 公式单独存放，不送翻译（送进去容易被改写）

        if btype == "ref_text":
            if not in_refs:
                in_refs = True
                pages.setdefault(pno, []).append("## References")
            pages.setdefault(pno, []).append(text)
            pages.setdefault(pno, []).append("")  # 每条参考文献独立成段
            continue

        if btype == "doc_title":
            pages.setdefault(pno, []).append(f"# {text}")  # 论文大标题，切章时会被丢掉
            continue

        if btype == "paragraph_title":
            pages.setdefault(pno, []).append(f"{_title_hashes(text, b.get('level') or 2)} {text}")
            continue

        if btype == "text":
            # MinerU 常把 Abstract 当普通正文（它没有编号，也不算标题），
            # 这里补一个标题，否则摘要会被并进题名页，而摘要恰恰是最该翻的部分
            if not in_abstract and re.match(r"^abstract\b[:.\s]", text, re.IGNORECASE):
                in_abstract = True
                pages.setdefault(pno, []).append("## Abstract")
                text = re.sub(r"^abstract\b[:.\s]*", "", text, flags=re.IGNORECASE).strip()
                if not text:
                    continue

        pages.setdefault(pno, []).append(text)
        pages.setdefault(pno, []).append("")  # MinerU 的每个 text 块本身就是一段

    total = (max(pages) if pages else 0)
    md_pages = ["\n".join(pages.get(i, [])) for i in range(1, total + 1)]
    return md_pages, tables, figures


def parse_with_mineru(path: str) -> ParsedPaper:
    """MinerU 解析：结构最准，但慢。"""
    data = _mineru_blocks(path)
    md_pages, tables, figures = _blocks_to_markdown(data["blocks"])
    sections = split_sections_md(md_pages)
    raw = "\n".join("\n".join(t for _, t in _md_to_lines(md)) for md in md_pages)

    paper = ParsedPaper(
        path=path,
        page_count=data.get("pages") or len(md_pages),
        needs_ocr=False,
        sections=sections,
        raw_text=raw,
        backend="mineru",
    )

    # 表格按页挂到章节上
    for t in tables:
        for s in sections:
            if s.page <= t["page"] <= _section_end_page(sections, s):
                s.tables.append({"page": t["page"], "rows": t["rows"], "html": t["html"]})
                break

    # 图片：用 bbox 从原页裁出来，比整页截图精确得多
    doc = pymupdf.open(path)
    try:
        _attach_media(paper, doc, path)
        base = config.DATA_DIR / "media" / _pdf_digest(path)
        for i, f in enumerate(figures):
            pno = f["page"]
            if not (1 <= pno <= doc.page_count) or len(f["bbox"]) != 4:
                continue
            page = doc[pno - 1]
            x0, y0, x1, y1 = f["bbox"]
            rect = pymupdf.Rect(
                x0 * page.rect.width, y0 * page.rect.height,
                x1 * page.rect.width, y1 * page.rect.height,
            )
            if rect.width < 40 or rect.height < 30:
                continue
            fp = base / f"mineru_p{pno}_{i}.png"
            try:
                page.get_pixmap(clip=rect, dpi=170).save(fp)
                paper.figures.setdefault(pno, []).append(str(fp))
                if f.get("caption"):
                    paper.captions[str(fp)] = f["caption"]
            except Exception:
                continue
    finally:
        doc.close()
    return paper


def _section_end_page(sections: list[Section], sec: Section) -> int:
    idx = sections.index(sec)
    return sections[idx + 1].page if idx + 1 < len(sections) else 10**6


def _pdf_digest(pdf_path: str) -> str:
    with open(pdf_path, "rb") as f:
        return hashlib.md5(f.read(4 * 1024 * 1024)).hexdigest()[:12]


def ocr_image(path: str, lang: str = "ch") -> str:
    """对单张图片做 OCR，返回纯文本。手机拍照通道走这条路。

    引擎加载要几秒，进程内复用同一个实例，否则每次拍照都要等模型初始化。
    """
    global _OCR_ENGINE
    from rapidocr_onnxruntime import RapidOCR

    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    result, _ = _OCR_ENGINE(path)
    if not result:
        return ""
    # result 每项：[框坐标, 文本, 置信度]
    return "\n".join(item[1] for item in result if len(item) > 1 and item[1].strip())


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
                pages_md = _md_pages(doc)
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
                return _attach_media(paper, doc, path)
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
