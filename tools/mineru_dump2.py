"""调试：打印 MinerU 各类型块的结构，确认 table / image / formula 的字段。

注意：MinerU 内部用 multiprocessing spawn 起渲染进程，会重新导入 __main__，
所以**所有逻辑必须放在 `if __name__ == "__main__":` 里面**，否则子进程会递归重跑。
"""

import json
import sys


def main() -> int:
    from mineru.parser.mineru_parser import MinerUParser

    pdf = sys.argv[1]
    pages = sys.argv[2] if len(sys.argv) > 2 else "1-4"
    res = MinerUParser(tier="basic").parse(pdf, page_range=pages)
    d = res.to_dict()

    types: dict[str, list] = {}
    for p in d["pages"]:
        for b in p.get("blocks") or []:
            types.setdefault(b["type"], []).append((p["page_idx"], b))

    for t, items in sorted(types.items()):
        print("类型 %-22s 数量 %d" % (t, len(items)))

    print("---- 样例 ----")
    for t in ("table", "image", "interline_equation", "text"):
        if t in types:
            _, b = types[t][0]
            print("[%s] %s" % (t, json.dumps(b, ensure_ascii=False)[:500]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
