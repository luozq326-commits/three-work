"""链路自检。

用途：
  1. 确认模型服务在线、模型存在
  2. 写入演示术语库
  3. 跑一段真实摘要翻译，量出真实 tok/s 与术语一致率

用法（在项目根目录）：
  E:\\Python312\\python.exe smoke_test.py
"""

import sys
import time

from app import config, llm, settings, terms, translate

SAMPLE = (
    "Transformer models have become the dominant architecture for sequence modeling. "
    "However, their quadratic attention cost limits deployment on long documents. "
    "In this work, we propose a sparse attention mechanism that reduces complexity to linear "
    "time while preserving accuracy on standard benchmark tasks. "
    "Our ablation study shows that the sparsity pattern, rather than the reduced parameter count, "
    "accounts for most of the efficiency gain. "
    "Experiments on three datasets demonstrate a 2.3x speedup with a 0.4 point drop in F1 score. "
    "We conclude that structured sparsity is a practical path toward long-context inference."
)


def main() -> int:
    print("=" * 58)
    print("1) 检查模型服务")
    cur = settings.current()
    if not llm.health():
        print(f"   [失败] {cur.get('base_url') or config.BASE_URL} 无响应")
        print("   先启动服务：\"E:\\模型\\Ollama\\ollama.exe\" serve")
        return 1
    if cur.get("backend") == "ollama":
        models = llm.list_models()
        print(f"   [通过] 在线，可用模型 {len(models)} 个")
        want_model = cur.get("model") or config.MAIN_MODEL
        for m in models:
            mark = "  <- 当前主力" if m == want_model else ""
            print(f"        {m}{mark}")
        if want_model not in models:
            print(f"   [警告] 配置里的 {want_model} 不在列表中，请先 ollama pull")
            return 1
    else:
        print(f"   [通过] 云端服务在线：{llm.current_label()}")

    print("=" * 58)
    print("2) 初始化术语库")
    n = terms.seed_demo()
    total = len(terms.all_terms())
    print(f"   [通过] 写入演示术语 {n} 条，库内共 {total} 条")

    hits = terms.match_terms(SAMPLE)
    print(f"   样例文本命中术语 {len(hits)} 条：" +
          "、".join(f"{h['source']}→{h['target']}" for h in hits))

    print("=" * 58)
    print("3) 真实翻译（这段是全文最慢的一步，请等）")
    t0 = time.perf_counter()
    result = translate.translate_text(SAMPLE, domain=None)
    wall = time.perf_counter() - t0

    print("=" * 58)
    print("译文：")
    print(result["full_text"])
    print("-" * 58)
    print(f"块数        {len(result['chunks'])}")
    print(f"总耗时      {wall:.1f} s")
    print(f"生成速度    {result['tok_per_s']} tok/s   <- 报告里写这个实测值")
    print(f"术语一致率  {result['term_rate'] * 100:.1f}%")
    bad = [v for c in result["chunks"] for v in c["verify"]["violations"]]
    print(f"术语违例    {len(bad)} 处" + (f"：{bad}" if bad else ""))
    print("=" * 58)
    print("自检完成。下一步：拿一篇真实论文 PDF 跑 parse_pdf()。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
