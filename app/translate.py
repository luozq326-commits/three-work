"""翻译流程：分段/分块 → 术语注入 → 调用模型 → 术语校验 → 违规则重译。

两个入口：
- ``translate_text(text)``：整段文本按字符数切块翻译，返回连续译文。用于手机拍照、评测。
- ``translate_paragraphs(paras)``：按段落翻译并保留页码，用于正文阅读区。
  每段单独成条，才能做"点译文跳到原文第几页"。

两条路径都走缓存：同一段英文 + 同一模型 + 同一版术语库，直接复用译文。
"""

import re
import time

from . import cache, config, llm, terms
from .pdf_parse import split_chunks
from .prompts import TRANSLATE_NUMBERED_SYSTEM, TRANSLATE_SYSTEM


def translate_chunk(
    text: str,
    domain: str | None = None,
    max_retry: int = 1,
    tgt_lng: str | None = None,
    use_cache: bool = True,
) -> dict:
    """翻译单个文本块，返回 {text, hits, verify, retries, meta, cached}。"""
    tgt_lng = tgt_lng or config.TARGET_LNG
    key = cache.make_key(text) if use_cache else ""
    if use_cache:
        hit = cache.get(key)
        if hit:
            verify = terms.verify_translation(text, hit["target"], domain, tgt_lng)
            return {
                "text": hit["target"],
                "hits": terms.match_terms(text, domain, tgt_lng),
                "verify": verify,
                "retries": 0,
                "meta": {"wall_s": 0.0, "tok_per_s": 0.0, "eval_count": 0, "cached": True},
                "cached": True,
            }

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

    if use_cache and translated.strip():
        cache.put(key, text, translated, {"model": config.MAIN_MODEL, "retries": retries})

    return {
        "text": translated,
        "hits": hits,
        "verify": result,
        "retries": retries,
        "meta": meta,
        "cached": False,
    }


def translate_text(text: str, domain: str | None = None, tgt_lng: str | None = None) -> dict:
    """翻译整段文本（自动分块）。

    返回 {chunks: [...], full_text, term_rate, total_s, tok_per_s, cache_hits}
    """
    chunks = split_chunks(text, config.CHUNK_CHARS)
    if not chunks:
        return {
            "chunks": [],
            "full_text": "",
            "term_rate": 1.0,
            "total_s": 0.0,
            "tok_per_s": 0.0,
            "cache_hits": 0,
        }

    out, rates, total_s, tokens, hits = [], [], 0.0, 0, 0
    for i, chunk in enumerate(chunks, start=1):
        item = translate_chunk(chunk, domain, tgt_lng=tgt_lng)
        out.append(item)
        rates.append(item["verify"]["rate"])
        total_s += item["meta"]["wall_s"]
        tokens += item["meta"]["eval_count"]
        hits += 1 if item.get("cached") else 0
        flag = "" if item["verify"]["consistent"] else f"  [术语违例 {len(item['verify']['violations'])}]"
        mark = " [缓存]" if item.get("cached") else ""
        print(f"  块 {i}/{len(chunks)}  {item['meta']['wall_s']:>5.1f}s  "
              f"{item['meta']['tok_per_s']:>4.1f} tok/s{flag}{mark}")

    return {
        "chunks": out,
        "full_text": "\n\n".join(c["text"] for c in out),
        "term_rate": round(sum(rates) / len(rates), 3) if rates else 1.0,
        "total_s": round(total_s, 2),
        "tok_per_s": round(tokens / total_s, 1) if total_s else 0.0,
        "cache_hits": hits,
    }


# ---------------------------------------------------------------- 段落级


def _parse_numbered(raw: str, n: int) -> dict[int, str]:
    """解析 "[1] 译文\\n[2] 译文" 形式的输出。模型不听话时返回空字典。"""
    parts = re.split(r"^\s*\[(\d+)\]\s*", raw, flags=re.MULTILINE)
    got: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        try:
            idx = int(parts[i])
        except ValueError:
            continue
        body = parts[i + 1].strip()
        if body and 1 <= idx <= n:
            got[idx] = body
    return got


def _batch_paras(pending: list[tuple[int, str]], max_chars: int) -> list[list[tuple[int, str]]]:
    """把待译段落按字符预算分组，保持原有顺序。"""
    batches, cur, size = [], [], 0
    for idx, text in pending:
        if cur and size + len(text) > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append((idx, text))
        size += len(text) + 8
    if cur:
        batches.append(cur)
    return batches


def translate_paragraphs(
    paras: list[dict],
    domain: str | None = None,
    tgt_lng: str | None = None,
    max_retry: int = 1,
) -> dict:
    """按段落翻译，返回带页码的对照结果。

    paras: [{"page": 3, "text": "..."}]
    返回：{"paras": [{page, source, translation, aligned, cached}],
           "term_rate", "violations", "chunks", "total_s", "tok_per_s", "cache_hits"}
    """
    tgt_lng = tgt_lng or config.TARGET_LNG
    out: list[dict | None] = [None] * len(paras)
    pending: list[tuple[int, str]] = []
    cache_hits = 0

    for i, p in enumerate(paras):
        src = (p.get("text") or "").strip()
        page = p.get("page", 0)
        if not src:
            out[i] = {"page": page, "source": "", "translation": "", "aligned": True, "cached": False}
            continue
        hit = cache.get(cache.make_key(src))
        if hit:
            out[i] = {
                "page": page,
                "source": src,
                "translation": hit["target"],
                "aligned": True,
                "cached": True,
            }
            cache_hits += 1
        else:
            pending.append((i, src))

    total_s = 0.0
    tokens = 0
    chunks = 0
    violations = 0
    rates: list[float] = []
    aligned_ok = 0
    aligned_total = 0

    for batch in _batch_paras(pending, config.CHUNK_CHARS):
        joined = "\n\n".join(f"[{n}] {t}" for n, (_, t) in enumerate(batch, start=1))
        hits = terms.match_terms(joined, domain, tgt_lng)
        block = terms.constraint_block(hits)
        messages = [
            {"role": "system", "content": TRANSLATE_NUMBERED_SYSTEM.format(term_block=block)},
            {"role": "user", "content": joined},
        ]
        t0 = time.perf_counter()
        raw, meta = llm.chat(messages, return_meta=True)
        parsed = _parse_numbered(raw, len(batch))

        # 模型没按编号输出：整批当一块，标记未对齐，界面退化成整段显示
        if len(parsed) != len(batch):
            whole = raw.strip()
            for n, (i, src) in enumerate(batch, start=1):
                out[i] = {
                    "page": paras[i].get("page", 0),
                    "source": src,
                    "translation": whole if n == 1 else "",
                    "aligned": False,
                    "cached": False,
                }
                aligned_total += 1
        else:
            for n, (i, src) in enumerate(batch, start=1):
                zh = parsed[n]
                out[i] = {
                    "page": paras[i].get("page", 0),
                    "source": src,
                    "translation": zh,
                    "aligned": True,
                    "cached": False,
                }
                aligned_ok += 1
                aligned_total += 1
                cache.put(cache.make_key(src), src, zh, {"model": config.MAIN_MODEL})

        total_s += time.perf_counter() - t0
        tokens += meta.get("eval_count", 0)
        chunks += 1

        # 逐段校验术语：按段判定比按块判定更准
        for n, (i, src) in enumerate(batch, start=1):
            item = out[i]
            if not item or not item["translation"]:
                continue
            v = terms.verify_translation(src, item["translation"], domain, tgt_lng)
            rates.append(v["rate"])
            violations += len(v["violations"])

    result_paras = [
        p
        if p is not None
        else {"page": 0, "source": "", "translation": "", "aligned": True, "cached": False}
        for p in out
    ]
    return {
        "paras": result_paras,
        "term_rate": round(sum(rates) / len(rates), 3) if rates else 1.0,
        "violations": violations,
        "chunks": chunks,
        "total_s": round(total_s, 2),
        "tok_per_s": round(tokens / total_s, 1) if total_s else 0.0,
        "cache_hits": cache_hits,
        "aligned_rate": round(aligned_ok / aligned_total, 3) if aligned_total else 1.0,
    }
