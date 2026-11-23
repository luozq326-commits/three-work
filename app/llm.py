"""模型调用适配层。

上层只依赖 chat() / chat_json() / embed()，不关心后端是本机 Ollama 还是云端 OpenAI 兼容服务。
当前用哪个提供方由 app/settings.py 决定（界面可切换），换模型不需要改业务代码。
"""

import json
import time
import urllib.error
import urllib.request

from . import config, settings


class LLMError(RuntimeError):
    """模型调用失败，上层需要显式处理，不允许静默吞掉。"""


def _cfg() -> dict:
    """当前生效的提供方：backend / base_url / model / api_key。"""
    p = settings.current()
    return {
        "backend": p.get("backend") or "ollama",
        "base_url": (p.get("base_url") or config.BASE_URL).rstrip("/"),
        "model": p.get("model") or config.MAIN_MODEL,
        "api_key": p.get("api_key") or "",
        "name": p.get("name") or "local",
        # 别漏字段：漏了 disable_thinking，豆包就会一直开着深度思考烧 token
        "disable_thinking": bool(p.get("disable_thinking")),
    }


def current_label() -> str:
    """给启动提示用的「提供方 · 模型」描述。"""
    c = _cfg()
    return f"{c['name']} · {c['model']}"


def model_name() -> str:
    """当前模型标识，缓存键要用它，切模型后旧缓存自然失效。"""
    c = _cfg()
    return f"{c['name']}:{c['model']}"


# 有些环境（公司网络、沙箱）会设 http_proxy 环境变量，urllib 会照单全收。
# 代理常常不放行模型服务域名，表现就是"连不上"或卡几十秒。这里一律直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url: str, payload: dict, timeout: int, api_key: str = "") -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise LLMError(f"模型接口返回 {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"无法连接模型服务 {url}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回非 JSON：{exc}") from exc


def health() -> bool:
    """检查当前提供方是否可用。演示前先跑这个。"""
    c = _cfg()
    try:
        if c["backend"] == "ollama":
            with _OPENER.open(f"{c['base_url']}/api/tags", timeout=5) as r:
                return r.status == 200
        req = urllib.request.Request(
            f"{c['base_url']}/models",
            headers={"Authorization": f"Bearer {c['api_key']}"} if c["api_key"] else {},
        )
        with _OPENER.open(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def list_models() -> list[str]:
    """只有本机 Ollama 能列模型；云端接口不一定支持，返回空即可。"""
    c = _cfg()
    if c["backend"] != "ollama":
        return []
    try:
        with urllib.request.urlopen(f"{c['base_url']}/api/tags", timeout=5) as r:
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
    max_tokens: int | None = None,
):
    """统一对话接口。

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    return_meta=True 时返回 (文本, meta)，meta 含耗时与 token 统计，用于评测。
    """
    c = _cfg()
    model = model or c["model"]
    temperature = config.TEMPERATURE if temperature is None else temperature
    num_ctx = config.NUM_CTX if num_ctx is None else num_ctx
    timeout = config.TIMEOUT_S if timeout is None else timeout

    started = time.perf_counter()

    if c["backend"] == "ollama":
        options = {"temperature": temperature, "num_ctx": num_ctx}
        # 不设上限时模型偶尔会一路写下去，单次调用能拖到几分钟
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        data = _post(f"{c['base_url']}/api/chat", payload, timeout, c["api_key"])
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
    elif c["backend"] == "openai":
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        # 豆包 2.x 的「深度思考」对翻译这类任务纯属浪费：两句话能产 5000 思考 token
        if c.get("disable_thinking"):
            payload["thinking"] = {"type": "disabled"}
        data = _post(f"{c['base_url']}/chat/completions", payload, timeout, c["api_key"])
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content", "") or ""
        meta = {
            "model": model,
            "eval_count": (data.get("usage") or {}).get("completion_tokens", 0),
            "tok_per_s": 0.0,
            "wall_s": round(time.perf_counter() - started, 2),
        }
    else:
        raise LLMError(f"未知后端：{c['backend']}")

    if not text.strip():
        raise LLMError(f"模型 {model} 返回空内容")

    return (text, meta) if return_meta else text


def embed(texts: list[str], model: str | None = None, timeout: int = 60) -> list[list[float]]:
    """文本向量。用于跨文献笔记检索。

    向量模型不可用时抛 LLMError，调用方负责降级到关键词检索。
    """
    c = _cfg()
    model = model or config.EMBED_MODEL
    out: list[list[float]] = []
    for t in texts:
        if c["backend"] == "ollama":
            data = _post(
                f"{c['base_url']}/api/embeddings",
                {"model": model, "prompt": t[:4000]},
                timeout,
                c["api_key"],
            )
            vec = data.get("embedding") or []
        else:
            data = _post(
                f"{c['base_url']}/embeddings",
                {"model": model, "input": t[:4000]},
                timeout,
                c["api_key"],
            )
            vec = (data.get("data") or [{}])[0].get("embedding") or []
        if not vec:
            raise LLMError(f"向量模型 {model} 返回空向量")
        out.append(vec)
    return out


def embed_ready(model: str | None = None) -> bool:
    """向量模型是否可用（避免每次检索都等超时）。只有本机 Ollama 这条路可靠。"""
    c = _cfg()
    if c["backend"] != "ollama":
        return False
    model = model or config.EMBED_MODEL
    try:
        return any(m == model or m.startswith(model + ":") for m in list_models())
    except Exception:
        return False


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
                {
                    "role": "user",
                    "content": "你上次的输出不是合法 JSON。请只输出 JSON，不要任何解释、不要 markdown 围栏。",
                },
            ]
    raise LLMError(f"JSON 解析连续失败：{last_err}")
