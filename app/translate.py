"""翻译流程：分块 → 术语注入 → 调用模型 → 术语校验 → 违规则重译该块。"""

from . import config, llm, terms
from .pdf_parse import split_chunks
from .prompts import TRANSLATE_SYSTEM


def translate_chunk(
    text: str,
    domain: str | None = None,
    max_retry: int = 1,
    tgt_lng: str | None = None,
) -> dict:
    """翻译单个文本块，返回 {text, hits, verify, retries, meta}。"""
    tgt_lng = tgt_lng or config.TARGET_LNG
    hits = terms.match_terms(text, domain, tgt_lng)
    block = terms.constraint_block(hits)
    messages = [
        {"role": "system", "content": TRANSLATE_SYSTEM.format(term_block=block)},
        {"role": "user", "content": text},
    ]

    translated, meta = llm.chat(messages, return_meta=True)
    result = terms.verify_translation(text, translated, domain, tgt_lng)

    retries = 0
    while not result["consistent"] and retries < max_retry:
        retries += 1
        bad = "、".join(f"{v['source']}→{v['target']}" for v in result["violations"])
        messages = messages + [
            {"role": "assistant", "content": translated},
            {
                "role": "user",
                "content": (
                    f"译文里这些术语没有按指定译法出现：{bad}。"
                    "请修正后重新输出完整译文，仍然只输出译文本身。"
                ),
            },
        ]
        translated, meta = llm.chat(messages, return_meta=True)
        result = terms.verify_translation(text, translated, domain, tgt_lng)

    return {
        "text": translated,
        "hits": hits,
        "verify": result,
        "retries": retries,
        "meta": meta,
    }


def translate_text(text: str, domain: str | None = None, tgt_lng: str | None = None) -> dict:
    """翻译整段文本（自动分块）。

    返回 {chunks: [...], full_text, term_rate, total_s, tok_per_s}
    """
    chunks = split_chunks(text, config.CHUNK_CHARS)
    if not chunks:
        return {"chunks": [], "full_text": "", "term_rate": 1.0, "total_s": 0.0, "tok_per_s": 0.0}

    out, rates, total_s, tokens = [], [], 0.0, 0
    for i, chunk in enumerate(chunks, start=1):
        item = translate_chunk(chunk, domain, tgt_lng=tgt_lng)
        out.append(item)
        rates.append(item["verify"]["rate"])
        total_s += item["meta"]["wall_s"]
        tokens += item["meta"]["eval_count"]
        flag = "" if item["verify"]["consistent"] else f"  [术语违例 {len(item['verify']['violations'])}]"
        print(f"  块 {i}/{len(chunks)}  {item['meta']['wall_s']:>5.1f}s  "
              f"{item['meta']['tok_per_s']:>4.1f} tok/s{flag}")

    return {
        "chunks": out,
        "full_text": "\n\n".join(c["text"] for c in out),
        "term_rate": round(sum(rates) / len(rates), 3) if rates else 1.0,
        "total_s": round(total_s, 2),
        "tok_per_s": round(tokens / total_s, 1) if total_s else 0.0,
    }
