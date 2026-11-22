"""界面冒烟测试：不依赖模型，用假译文跑一遍完整链路，验证渲染。

用途：验证"表格/插图/公式是否回到正文原位"这类版面问题。
真实翻译要模型在线，冒烟测试不该被模型状态卡住。

用法：
  E:/Python312/python.exe tools/verify_ui.py --port 8002
  然后 chrome --headless --dump-dom http://127.0.0.1:8002/
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extract, note, translate  # noqa: E402


def _fake_translate(paras, domain=None, tgt_lng=None, max_retry=1):
    return {
        "paras": [
            {
                "page": p.get("page", 0),
                "source": p.get("text", ""),
                "translation": "【测试译文】" + (p.get("text", "")[:40]),
                "aligned": True,
                "cached": False,
            }
            for p in paras
        ],
        "term_rate": 1.0,
        "violations": 0,
        "chunks": 1,
        "total_s": 0.1,
        "tok_per_s": 1.0,
        "cache_hits": 0,
        "aligned_rate": 1.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8002)
    args = ap.parse_args()

    translate.translate_paragraphs = _fake_translate
    extract.extract_elements = lambda paper, **kw: extract.Elements(
        {
            "research_question": "测试用问题",
            "method": ["测试用方法"],
            "evidence": [],
        }
    )
    extract.rerank_evidence = lambda el: []
    note.build_note = lambda el, title="": "# 测试笔记\n\n这是冒烟测试生成的笔记。"
    note.save = lambda *a, **kw: 1

    import web_server

    sys.argv = ["web_server.py", "--port", str(args.port)]
    return web_server.main()


if __name__ == "__main__":
    sys.exit(main())
