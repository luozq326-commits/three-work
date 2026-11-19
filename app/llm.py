"""模型调用适配层。

上层只依赖 chat() / chat_json()，不关心后端是 Ollama 还是 OpenAI 兼容服务。
换模型 = 改 config.py 里的 MAIN_MODEL 一行。
"""

import json
import time
import urllib.error
import urllib.request

from . import config


class LLMError(RuntimeError):
    """模型调用失败，上层需要显式处理，不允许静默吞掉。"""


def _post(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LLMError(f"无法连接模型服务 {url}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回非 JSON：{exc}") from exc


def health() -> bool:
    """检查后端是否可用。演示前必须先跑这个。"""
    try:
        with urllib.request.urlopen(f"{config.BASE_URL}/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def list_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{config.BASE_URL}/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return sorted(m["name"] for m in data.get("models", []))
    except Exception:
        return []


def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float | None = None,
    num_ctx: int | None = None,
    timeout: int | None = None,
    return_meta: bool = False,
):
    """统一对话接口。

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    return_meta=True 时返回 (文本, meta)，meta 含耗时与 token 统计，用于评测。
    """
    model = model or config.MAIN_MODEL
    temperature = config.TEMPERATURE if temperature is None else temperature
    num_ctx = config.NUM_CTX if num_ctx is None else num_ctx
    timeout = config.TIMEOUT_S if timeout is None else timeout

    started = time.perf_counter()

    if config.BACKEND == "ollama":
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        data = _post(f"{config.BASE_URL}/api/chat", payload, timeout)
        text = data.get("message", {}).get("content", "")
        eval_count = data.get("eval_count", 0)
        eval_ns = data.get("eval_duration", 0)
        tok_per_s = eval_count / (eval_ns / 1e9) if eval_ns else 0.0
        meta = {
            "model": model,
            "eval_count": eval_count,
            "tok_per_s": round(tok_per_s, 1),
            "wall_s": round(time.perf_counter() - started, 2),
        }
    elif config.BACKEND == "openai":
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        data = _post(f"{config.BASE_URL}/v1/chat/completions", payload, timeout)
        text = data["choices"][0]["message"]["content"]
        meta = {
            "model": model,
            "eval_count": data.get("usage", {}).get("completion_tokens", 0),
            "tok_per_s": 0.0,
            "wall_s": round(time.perf_counter() - started, 2),
        }
    else:
        raise LLMError(f"未知后端：{config.BACKEND}")

    if not text.strip():
        raise LLMError(f"模型 {model} 返回空内容")

    return (text, meta) if return_meta else text


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def chat_json(messages: list[dict], retries: int = 1, **kwargs) -> dict:
    """要求模型输出 JSON，失败自动重试。

    小模型经常带 markdown 围栏或多余说明，这里做一次清洗 + 一次重试。
    """
    last_err = None
    for attempt in range(retries + 1):
        raw = chat(messages, **kwargs)
        cleaned = _strip_fence(raw)
        # 兜底：截取第一个 { 到最后一个 }
        if not cleaned.startswith("{"):
            lo, hi = cleaned.find("{"), cleaned.rfind("}")
            if lo != -1 and hi > lo:
                cleaned = cleaned[lo : hi + 1]
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_err = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "你上次的输出不是合法 JSON。请只输出 JSON，不要任何解释、不要 markdown 围栏。"},
            ]
    raise LLMError(f"JSON 解析连续失败：{last_err}")
