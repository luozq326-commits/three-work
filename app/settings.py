"""模型提供方设置：本机 Ollama 或任意 OpenAI 兼容的云端接口。

为什么单独一层：端侧是卖点，但演示时 7B 模型的中文表达不够好，
要能一键切到 DeepSeek / 豆包，而不是改代码。密钥只存在本机 data/settings.json。
"""

import json
from pathlib import Path

from . import config

PATH = Path(config.DATA_DIR) / "settings.json"

DEFAULTS = {
    "active": "local",
    "providers": {
        "local": {
            "label": "本机 Ollama",
            "backend": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": config.MAIN_MODEL,
            "api_key": "",
        },
        "deepseek": {
            "label": "DeepSeek",
            "backend": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": "",
        },
        "doubao": {
            "label": "豆包（火山方舟）",
            "backend": "openai",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "model": "doubao-seed-1-6-250615",
            "api_key": "",
        },
        "custom": {
            "label": "自定义 OpenAI 兼容",
            "backend": "openai",
            "base_url": "",
            "model": "",
            "api_key": "",
        },
    },
}


def load() -> dict:
    """读设置，缺的字段用默认值补齐（老配置文件也能用）。"""
    data = {}
    if PATH.exists():
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    out = {"active": data.get("active") or DEFAULTS["active"], "providers": {}}
    for name, default in DEFAULTS["providers"].items():
        merged = dict(default)
        merged.update((data.get("providers") or {}).get(name) or {})
        out["providers"][name] = merged
    if out["active"] not in out["providers"]:
        out["active"] = "local"
    return out


def save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def current() -> dict:
    """当前生效的提供方配置，附带 name。"""
    data = load()
    cfg = dict(data["providers"][data["active"]])
    cfg["name"] = data["active"]
    return cfg


def update(name: str, **kw) -> dict:
    data = load()
    if name not in data["providers"]:
        data["providers"][name] = dict(DEFAULTS["providers"]["custom"], label=name)
    for k in ("label", "backend", "base_url", "model", "api_key"):
        if kw.get(k) is not None:
            data["providers"][name][k] = str(kw[k]).strip()
    save(data)
    return data["providers"][name]


def set_active(name: str) -> bool:
    data = load()
    if name not in data["providers"]:
        return False
    data["active"] = name
    save(data)
    return True


def public_view() -> dict:
    """给前端看的视图：密钥只回传是否已填，不回传明文。"""
    data = load()
    return {
        "active": data["active"],
        "providers": {
            name: {
                "label": p["label"],
                "backend": p["backend"],
                "base_url": p["base_url"],
                "model": p["model"],
                "has_key": bool(p.get("api_key")),
            }
            for name, p in data["providers"].items()
        },
    }
