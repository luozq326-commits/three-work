"""端到端演示：PDF → 解析 → 翻译 → 要素抽取 → 笔记 → 导出。

用法（项目根目录）：
  E:\\Python312\\python.exe demo.py data/papers/resnet.pdf
  E:\\Python312\\python.exe demo.py data/papers/resnet.pdf --sections all
  E:\\Python312\\python.exe demo.py data/papers/resnet.pdf --sections abstract,method --no-translate

设计约定：
  - 每阶段单独计时并打印，报告里的数字直接取自这里
  - 任一阶段失败不静默吞掉，打印原因后继续能继续的阶段
  - 默认只翻摘要：全篇翻译是分钟级操作，演示前先确认链路通再放开
"""

import argparse
import os
import sys
import time

from app import config, extract, llm, note, pdf_parse, terms, translate


def _fmt(sec: float) -> str:
    return f"{sec:5.1f}s"


def main() -> int:
    ap = argparse.ArgumentParser(description="端侧双语学术文献精读助手 · 端到端演示")
    ap.add_argument("pdf", help="PDF 路径")
    ap.add_argument("--sections", default="abstract", help="翻译哪些章节，all=全文，逗号分隔")
    ap.add_argument("--no-translate", action="store_true", help="跳过翻译")
    ap.add_argument("--no-note", action="store_true", help="跳过笔记生成")
    ap.add_argument("--candidates", action="store_true", help="顺带跑一次术语候选抽取")
    ap.add_argument("--out", default="out", help="导出目录")
    ap.add_argument("--max-chars", type=int, default=5000, help="抽取上下文字符预算")
    args = ap.parse_args()

    if not os.path.exists(args.pdf):
        print(f"[失败] 找不到文件：{args.pdf}")
        return 1
    if not llm.health():
        print(f"[失败] 模型服务 {config.BASE_URL} 无响应")
        print('  启动：export OLLAMA_MODELS="E:/模型" && "/e/模型/Ollama/ollama.exe" serve')
        return 1

    timings: dict[str, float] = {}
    title = os.path.splitext(os.path.basename(args.pdf))[0]

    print("=" * 60)
    print("1) 解析 PDF")
    t0 = time.perf_counter()
    paper = pdf_parse.parse_pdf(args.pdf, backend=config.PDF_BACKEND)
    timings["解析"] = time.perf_counter() - t0
    if paper.needs_ocr:
        print("   [警告] 无文本层，判定为扫描件，当前版本不支持 OCR")
        return 2
    print(f"   {paper.page_count} 页，后端 {paper.backend}，{_fmt(timings['解析'])}")
    for s in paper.sections:
        print(f"     {s.kind:<12} p{s.page:<3} {s.char_count:>6}  {s.title[:40]}")

    if not terms.all_terms():
        print("   术语库为空，写入演示术语")
        terms.seed_demo()

    translated: list[tuple] = []
    if not args.no_translate:
        print("=" * 60)
        print("2) 翻译")
        if args.sections == "all":
            targets = [s for s in paper.sections if s.kind not in ("references", "acknowledg")]
        else:
            wanted = {k.strip() for k in args.sections.split(",")}
            targets = [s for s in paper.sections if s.kind in wanted]
        if not targets:
            print(f"   [跳过] 没有匹配的章节：{args.sections}")
        t0 = time.perf_counter()
        for s in targets:
            print(f"   [{s.kind}] {s.char_count} 字符")
            try:
                r = translate.translate_text(s.text)
                translated.append((s, r))
                print(
                    f"     块 {len(r['chunks'])}，{_fmt(r['total_s'])}，"
                    f"{r['tok_per_s']} tok/s，术语一致率 {r['term_rate'] * 100:.0f}%"
                )
            except Exception as exc:
                print(f"     [失败] {exc}")
        timings["翻译"] = time.perf_counter() - t0

        # 译文落盘。不存的话这 4 分钟的计算就白跑了，界面也没东西可展示
        if translated:
            os.makedirs(args.out, exist_ok=True)
            zh_path = os.path.join(args.out, f"{title}.zh.md")
            parts = [f"# {title}", "", "> 机器翻译，术语库约束生效；原文为英文，术语一致率见文末。", ""]
            for s, r in translated:
                parts += [f"## {s.title}", "", r["full_text"], ""]
            rate = sum(r["term_rate"] for _, r in translated) / len(translated)
            bad = sum(len(c["verify"]["violations"]) for _, r in translated for c in r["chunks"])
            parts += ["---", "", f"术语一致率 {rate * 100:.1f}%，违例 {bad} 处。"]
            with open(zh_path, "w", encoding="utf-8") as f:
                f.write("\n".join(parts))
            print(f"   译文写入 {zh_path}（{sum(s.char_count for s, _ in translated)} 字符）")

    print("=" * 60)
    print("3) 要素抽取")
    t0 = time.perf_counter()
    try:
        el = extract.extract_elements(paper, max_chars=args.max_chars)
        timings["抽取"] = time.perf_counter() - t0
        rep = el.report()
        print(
            f"   {_fmt(timings['抽取'])}，字段 {len(rep['fields'])} 个，"
            f"证据 {rep['evidence_verified']}/{rep['evidence_total']} 回查通过"
        )
        if el.hit_rate < 1.0:
            print("   [注意] 有证据没能在原文命中，可能是模型改写或编造：")
            for e in el.evidence_checked:
                if not e["verified"]:
                    print(f"     [{e['field']}] {e['quote'][:70]}")
        top = extract.rerank_evidence(el)
        if top:
            print(f"   高分证据 {len(top)} 条：")
            for e in top[:3]:
                print(f"     [{e.get('score', '-')}] {e['field']}: {e['quote'][:60]}")
    except Exception as exc:
        print(f"   [失败] {exc}")
        el = None

    if args.candidates:
        print("=" * 60)
        print("4) 术语候选")
        try:
            cands = terms.extract_candidates(extract.pick_context(paper, 4000), limit=10)
            for c in cands:
                flag = "已在库" if c["in_db"] else "新候选"
                print(f"   [{flag}] {c['source']} → {c['target']}  ({c['reason']})")
            n = terms.accept_candidates(cands)
            print(f"   入库 {n} 条（origin=auto，可在 UI 里确认）")
        except Exception as exc:
            print(f"   [失败] {exc}")

    if el and not args.no_note:
        print("=" * 60)
        print("5) 笔记")
        t0 = time.perf_counter()
        md = note.build_note(el, title)
        timings["笔记"] = time.perf_counter() - t0
        rate = (
            sum(r["term_rate"] for _, r in translated) / len(translated)
            if translated
            else 1.0
        )
        nid = note.save(args.pdf, title, md, el, term_rate=rate)
        os.makedirs(args.out, exist_ok=True)
        path = note.export_markdown(nid, os.path.join(args.out, f"{title}.md"))
        print(f"   {_fmt(timings['笔记'])}，笔记 #{nid} → {path}")

    print("=" * 60)
    print("耗时汇总")
    for k, v in timings.items():
        print(f"   {k:<6} {_fmt(v)}")
    print(f"   合计   {_fmt(sum(timings.values()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
