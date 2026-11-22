"""MinerU 桥接：在 mineru 虚拟环境里跑，把解析结果压成我们自己的 JSON。

为什么用子进程而不是直接 import：
  MinerU 4.x 的依赖（torch / transformers 5.x / onnxruntime-directml）与主环境冲突，
  装进独立 venv，主程序用 subprocess 调用，互不污染。

输出（写到 --out）：
  {
    "pages": 12, "cost_s": 118.3,
    "blocks": [{"page":1,"type":"text","bbox":[x0,y0,x1,y1],"level":0,
                "text":"...","html":"<table>...","caption":"..."}]
  }
bbox 是归一化坐标（0~1），左上原点。

注意：MinerU 内部用 multiprocessing spawn，会重新导入 __main__，
所有逻辑必须写在 `if __name__ == "__main__":` 里。
"""

import argparse
import json
import sys
import time


def _text_of(content) -> str:
    """把 content 里的文本片段拼起来。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            parts.append(item.get("content") or "")
        elif t in ("image_caption", "table_caption", "table_footnote"):
            parts.append(_text_of(item.get("content")))
        elif t in ("image_body", "table_body"):
            continue
    return " ".join(p.strip() for p in parts if p and p.strip())


def _html_of(content) -> str:
    """表格 HTML。MinerU 已经把合并单元格展开成 rowspan/colspan。"""
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "table_body":
            body = item.get("content")
            if isinstance(body, str):
                return body
    return ""


def _caption_of(content) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") in ("image_caption", "table_caption"):
            return _text_of(item.get("content"))
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tier", default="basic", help="flash|basic|standard|advanced")
    ap.add_argument("--pages", default="", help="如 1-5；空表示全文")
    args = ap.parse_args()

    from mineru.parser.mineru_parser import MinerUParser

    t0 = time.perf_counter()
    parser = MinerUParser(tier=args.tier)
    res = parser.parse(args.pdf, page_range=args.pages)
    data = res.to_dict()
    cost = time.perf_counter() - t0

    blocks = []
    for page in data.get("pages") or []:
        pno = int(page.get("page_idx", 0)) + 1  # MinerU 从 0 开始，我们统一从 1 开始
        for b in page.get("blocks") or []:
            btype = b.get("type") or ""
            item = {
                "page": pno,
                "type": btype,
                "bbox": b.get("bbox") or [],
                "level": int(b.get("level") or 0),
                "text": _text_of(b.get("content")),
                "html": _html_of(b.get("content")),
                "caption": _caption_of(b.get("content")),
            }
            if btype == "equation":
                # 公式块：content 里通常直接是 LaTeX
                raw = _text_of(b.get("content"))
                item["text"] = raw
            if not (item["text"] or item["html"]):
                continue
            blocks.append(item)

    payload = {
        "pages": len(data.get("pages") or []),
        "cost_s": round(cost, 1),
        "blocks": blocks,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"blocks={len(blocks)} pages={payload['pages']} cost={cost:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
