"""演示前预热：把模型加载进显存，把首次调用的 8 秒开销挪出演示现场。

用法（项目根目录，演示前跑一次）：
  E:\\Python312\\python.exe warmup.py
  E:\\Python312\\python.exe warmup.py --rounds 2

做三件事：
  1. 检查服务与模型是否就绪（缺什么直接说缺什么）
  2. 发一条极短请求，触发模型加载进显存
  3. 跑一小段真实学术英文翻译，确认术语注入与 tok/s 正常

退出码 0 表示可以开始演示。
"""

import argparse
import sys
import time

from app import config, llm, terms, translate

# 预热用的片段：真实学术句式，含 2 个术语库里的词，长度足够测出稳态速度
WARM_TEXT = (
    "We propose a residual learning framework to ease the training of networks that are "
    "substantially deeper than those used previously. Our ablation study shows that the "
    "identity shortcuts, rather than the extra parameters, account for most of the gain."
)


def main() -> int:
    ap = argparse.ArgumentParser(description="模型预热")
    ap.add_argument("--rounds", type=int, default=1, help="重复跑几轮（第 2 轮起是稳态速度）")
    args = ap.parse_args()

    print("=" * 56)
    print("1) 检查服务")
    if not llm.health():
        print(f"   [失败] {config.BASE_URL} 无响应")
        print('   启动：export OLLAMA_MODELS="E:/模型" && "/e/模型/Ollama/ollama.exe" serve')
        return 1
    models = llm.list_models()
    if config.MAIN_MODEL not in models:
        print(f"   [失败] 模型 {config.MAIN_MODEL} 不在本地，先 ollama pull")
        print(f"   本地已有：{', '.join(models) or '（空）'}")
        return 1
    print(f"   [通过] 在线，主力模型 {config.MAIN_MODEL} 存在")

    print("=" * 56)
    print("2) 加载模型进显存")
    t0 = time.perf_counter()
    try:
        llm.chat([{"role": "user", "content": "回复 ok"}], timeout=300)
    except Exception as exc:
        print(f"   [失败] {exc}")
        return 1
    print(f"   [通过] 首次调用 {time.perf_counter() - t0:.1f}s（含加载，这段不该出现在演示里）")

    if not terms.all_terms():
        print("   [提示] 术语库为空，写入演示术语")
        terms.seed_demo()

    print("=" * 56)
    print("3) 跑真实翻译片段")
    for i in range(1, args.rounds + 1):
        try:
            r = translate.translate_text(WARM_TEXT)
        except Exception as exc:
            print(f"   [失败] {exc}")
            return 1
        print(
            f"   第 {i} 轮：{r['total_s']:>5.1f}s  {r['tok_per_s']:>4.1f} tok/s  "
            f"术语一致率 {r['term_rate'] * 100:.0f}%"
        )

    print("=" * 56)
    print("预热完成，可以开始演示。")
    # 英文学术文本约 4.5 字符/token（实测 1304 字符≈280 token），不能拿字符数直接除 tok/s
    est = config.CHUNK_CHARS / 4.5 / max(r["tok_per_s"], 1)
    print(f"   预估单块（{config.CHUNK_CHARS} 字符≈{config.CHUNK_CHARS // 4.5:.0f} token）"
          f"耗时约 {est:.0f}s，按实际内容浮动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
