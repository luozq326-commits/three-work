"""全局配置。

换模型只改这个文件，业务代码里不允许出现模型名字符串。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "notes.db"

# ---------------- 后端与地址 ----------------
# BACKEND: "ollama" 走 /api/chat；"openai" 走 /v1/chat/completions
# 换成 vLLM / LM Studio / llama.cpp server 时，把 BACKEND 改成 openai 即可
BACKEND = "ollama"
BASE_URL = "http://127.0.0.1:11434"

# ---------------- 模型 ----------------
# 开发期：本地已有的代码模型，先跑通链路
# 上线前：替换为 qwen2.5:7b-instruct（通用指令模型，中文学术表达明显更好）
MAIN_MODEL = "qwen2.5-coder:7b"

# 降级模型：显存不足或超时时使用。当前留空表示不降级。
# 正式环境建议 qwen2.5:3b-instruct
FALLBACK_MODEL = None

# 向量模型：用于笔记跨文献检索，暂未接入
EMBED_MODEL = "nomic-embed-text"

# ---------------- 推理参数 ----------------
TEMPERATURE = 0.2
NUM_CTX = 8192
TIMEOUT_S = 180

# 单次送入模型的最大字符数，超出则分块
CHUNK_CHARS = 1800

# ---------------- 目标语言 ----------------
# 术语表里 tgt_lng 为空的条目对任意目标语言生效，非空的按此值过滤（同 BabelDOC 语义）
TARGET_LNG = "zh-CN"

# ---------------- 解析 ----------------
# auto: 有 pymupdf4llm 就用版面模型 | layout: 强制 | blocks: 强制启发式
PDF_BACKEND = "auto"
