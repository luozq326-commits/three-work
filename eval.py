"""评测：同一套协议跑多篇论文，产出可写进报告的指标表。

协议（写报告时要照抄这一段，否则数字没有可比性）：
  - 解析：全篇，记录后端、章节数、耗时、是否判定扫描件
  - 翻译：只翻 abstract + method 两章（信息密度最高、长度可控），
          记录块数、术语一致率、违例数、稳态 tok/s、耗时
  - 抽取：全篇上下文，记录 JSON 是否合法、字段数、evidence 回查命中率
  - 笔记：生成但只记录耗时与成功与否

用法：
  E:\\Python312\\python.exe eval.py
  E:\\Python312\\python.exe eval.py --papers data/papers --out out
  E:\\Python312\\python.exe eval.py --full     # 翻全文（慢，分钟级/篇）
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime

from app import config, extract, llm, note, pdf_parse, terms, translate

TRANSLATE_KINDS = ("abstract", "method")


def eval_one(path: str, full: bool = False) -> dict:
    """跑一篇论文，返回一行结果。任何阶段失败都记录下来，不中断整批。"""
    row = {
        "paper": os.path.basename(path),
        "pages": 0,
        "backend": "",
        "sections": 0,
        "parse_s": 0.0,
        "chars": 0,
        "chunks": 0,
        "term_rate": None,
        "violations": 0,
        "tok_s": 0.0,
        "trans_s": 0.0,
        "json_ok": None,
        "fields": 0,
        "ev_total": 0,
        "ev_ok": 0,
        "ev_rate": None,
        "extract_s": 0.0,
        "note_s": 0.0,
        "error": "",
    }

    try:
        t0 = time.perf_counter()
        paper = pdf_parse.parse_pdf(path, backend=config.PDF_BACKEND)
        row["parse_s"] = time.perf_counter() - t0
        row["pages"] = paper.page_count
        row["backend"] = paper.backend
        row["sections"] = len(paper.sections)
        if paper.needs_ocr:
            row["error"] = "扫描件，无文本层"
            return row

        targets = (
            [s for s in paper.sections if s.kind not in ("references", "acknowledg")]
            if full
            else [s for s in paper.sections if s.kind in TRANSLATE_KINDS]
        )
        if not targets:
            row["error"] = "未识别出 abstract/method 章节"
        else:
            row["chars"] = sum(s.char_count for s in targets)
            t0 = time.perf_counter()
            rates, chunks, viol, tokens = [], 0, 0, 0
            for s in targets:
                r = translate.translate_text(s.text)
                chunks += len(r["chunks"])
                rates.append(r["term_rate"])
                viol += sum(len(c["verify"]["violations"]) for c in r["chunks"])
                tokens += sum(c["meta"]["eval_count"] for c in r["chunks"])
            row["trans_s"] = time.perf_counter() - t0
            row["chunks"] = chunks
            row["term_rate"] = round(sum(rates) / len(rates), 3)
            row["violations"] = viol
            row["tok_s"] = round(tokens / row["trans_s"], 1) if row["trans_s"] else 0.0

        t0 = time.perf_counter()
        el = extract.extract_elements(paper)
        row["extract_s"] = time.perf_counter() - t0
        row["json_ok"] = True  # chat_json 抛异常会走 except，走到这里就是合法 JSON
        row["fields"] = len(el.report()["fields"])
        row["ev_total"] = len(el.evidence_checked)
        row["ev_ok"] = len(el.verified_evidence())
        row["ev_rate"] = round(el.hit_rate, 3)

        t0 = time.perf_counter()
        md = note.build_note(el, os.path.splitext(row["paper"])[0])
        note.save(path, row["paper"], md, el, term_rate=row["term_rate"] or 1.0)
        row["note_s"] = time.perf_counter() - t0
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    return row


def to_markdown(rows: list[dict], stamp: str, full: bool) -> str:
    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    lines = [
        "# 评测结果",
        "",
        f"生成时间：{stamp}",
        f"模型：`{config.MAIN_MODEL}`　后端：`{config.BACKEND}`　解析：`{config.PDF_BACKEND}`",
        f"翻译范围：{'全文（不含参考文献/致谢）' if full else 'abstract + method'}",
        "",
        "| 论文 | 页数 | 后端 | 章节 | 解析 | 翻译字符 | 块数 | 术语一致率 | 违例 | tok/s | 翻译 | JSON | 字段 | evidence | 命中率 | 抽取 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {paper} | {pages} | {backend} | {sections} | {parse_s:.1f}s | {chars} | {chunks} | "
            "{rate} | {viol} | {tok} | {trans:.1f}s | {js} | {fields} | {ev_ok}/{ev_total} | {evr} | "
            "{ext:.1f}s | {err} |".format(
                paper=r["paper"],
                pages=r["pages"],
                backend=r["backend"],
                sections=r["sections"],
                parse_s=r["parse_s"],
                chars=r["chars"],
                chunks=r["chunks"],
                rate=pct(r["term_rate"]),
                viol=r["violations"],
                tok=r["tok_s"],
                trans=r["trans_s"],
                js="✓" if r["json_ok"] else ("✗" if r["json_ok"] is False else "—"),
                fields=r["fields"],
                ev_ok=r["ev_ok"],
                ev_total=r["ev_total"],
                evr=pct(r["ev_rate"]),
                ext=r["extract_s"],
                err=r["error"] or "",
            )
        )

    ok = [r for r in rows if not r["error"]]
    lines += ["", "## 汇总", ""]
    if ok:
        avg = lambda k: sum(r[k] for r in ok) / len(ok)
        lines += [
            f"- 成功 {len(ok)}/{len(rows)} 篇",
            f"- 平均术语一致率 **{pct(avg('term_rate'))}**",
            f"- 平均 evidence 命中率 **{pct(avg('ev_rate'))}**",
            f"- JSON 合法率 **{sum(1 for r in ok if r['json_ok'])}/{len(ok)}**",
            f"- 平均解析 {avg('parse_s'):.1f}s　平均翻译 {avg('trans_s'):.1f}s　平均抽取 {avg('extract_s'):.1f}s",
            f"- 平均稳态 **{avg('tok_s'):.1f} tok/s**",
        ]
    else:
        lines.append("- 无成功样本")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="多篇论文评测")
    ap.add_argument("--papers", default="data/papers")
    ap.add_argument("--out", default="out")
    ap.add_argument("--full", action="store_true", help="翻译全文（慢）")
    ap.add_argument("--only", default="", help="只跑文件名包含该子串的论文")
    args = ap.parse_args()

    if not llm.health():
        print(f"[失败] 模型服务 {config.BASE_URL} 无响应，先跑 warmup.py")
        return 1

    files = sorted(
        os.path.join(args.papers, f)
        for f in os.listdir(args.papers)
        if f.lower().endswith(".pdf")
    )
    if args.only:
        files = [f for f in files if args.only in os.path.basename(f)]
    if not files:
        print(f"[失败] {args.papers} 下没有 PDF")
        return 1

    if not terms.all_terms():
        print("术语库为空，写入演示术语")
        terms.seed_demo()

    rows = []
    for i, path in enumerate(files, start=1):
        print(f"\n[{i}/{len(files)}] {os.path.basename(path)}")
        row = eval_one(path, full=args.full)
        rows.append(row)
        print(
            f"   解析 {row['parse_s']:.1f}s · {row['sections']} 章 · {row['backend']}"
            f" | 翻译 {row['chars']} 字符/{row['chunks']} 块 {row['trans_s']:.1f}s"
            f" 一致率 {row['term_rate'] if row['term_rate'] is None else round(row['term_rate'] * 100, 1)}%"
            f" | 抽取 {row['extract_s']:.1f}s 证据 {row['ev_ok']}/{row['ev_total']}"
            + (f" | {row['error']}" if row["error"] else "")
        )

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = to_markdown(rows, stamp, args.full)
    md_path = os.path.join(args.out, "eval.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    json_path = os.path.join(args.out, "eval.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 60)
    print(md)
    print(f"结果写入 {md_path} / {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
