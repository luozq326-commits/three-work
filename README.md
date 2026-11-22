# PaperReader · 端侧双语学术文献精读助手

读英文论文的三件事——看懂、查术语、做笔记——全在**本机**完成，不联网、不上传。

模型跑在本地（Ollama + 7B），PDF 解析、翻译、要素抽取、笔记生成、跨文献检索都在自己的电脑上算。
适合对数据敏感的场景：未公开的实验数据、企业内部技术报告、审稿中的稿件。

## 能做什么

| 能力 | 说明 |
|---|---|
| PDF 解析 | 双后端：PyMuPDF 版面模型（快，约 12s/篇）或 MinerU（准，约 30s/篇，带 bbox） |
| 图表保留 | 表格按结构渲染（MinerU 档保留合并单元格）、插图与公式按 bbox 裁剪并**回到正文原位**、每页原页快照兜底 |
| 公式 | 精细档裁出公式图 + 显示 LaTeX 源码，点击放大 |
| 段落级对照 | 每段标注原文页码，原文/译文逐段对齐，可按页回查 |
| 扫描件 OCR | RapidOCR，图片页自动识别（约 15~40 秒/页） |
| 术语一致性 | 术语库四步闭环：候选抽取 → 人工确认 → 翻译注入 → 译文校验重译 |
| 结构化要素 | 研究问题 / 方法 / 数据 / 指标 / 关键数字 / 创新点 / 局限，强制附原文证据 |
| AI 总结 | 五段式整篇总结：解决什么 / 怎么做的 / 结果 / 局限 / 该不该读 |
| 证据回查 | 每条引用在**本地**回查原文，模型改写或编造的引用会被标红 |
| 阅读笔记 | 固定七段 + 原文依据附录，可编辑、可写批注、可导出 Markdown |
| 跨文献检索 | 历史笔记向量化，语义检索"哪篇论文提过这个方法" |
| 翻译缓存 | 段落级缓存，同一篇重跑秒出；改术语库或换模型后自动失效 |
| 模型可切换 | 本机 Ollama / DeepSeek / 豆包（火山方舟）/ 自定义 OpenAI 兼容 |
| 手机拍照 | 手机扫码拍照，识别与翻译都回到电脑上跑，图片不出局域网 |

## 快速开始

```bash
# 1. 模型服务（模型路径按自己的改）
export OLLAMA_MODELS="E:/模型"
"/e/模型/Ollama/ollama.exe" serve

# 2. 预热（把首次加载的几秒挪出演示现场）
E:/Python312/python.exe warmup.py

# 3. 启动界面
E:/Python312/python.exe web_server.py
#    手机拍照通道需要局域网访问：
#    E:/Python312/python.exe web_server.py --lan
```

浏览器打开 http://127.0.0.1:8000 。

依赖：Python 3.12、`pymupdf`(2.0)、`pymupdf4llm`、`rapidocr-onnxruntime`、`qrcode`。
Ollama 模型：`qwen2.5-coder:7b`（主力）、`nomic-embed-text`（检索）。

### 可选：MinerU 精细解析

默认走 PyMuPDF（12 秒/篇）。要更准的版面、表格合并单元格、图片 bbox、公式 LaTeX，装 MinerU：

```bash
E:/Python312/python.exe -m venv .venv-mineru
.venv-mineru/Scripts/python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv-mineru/Scripts/python.exe -m pip install "mineru[torch]"
```

- 装在独立 venv：torch / transformers 5.x 与主环境冲突，不能共装
- 模型 835MB，首次运行自动下载；用 `MINERU_HOME` 指到 E 盘（默认 `~/.mineru` 会占 C 盘）
- 推理走 DirectML，直接使用 NVIDIA 显卡，不需要 CUDA
- 界面右上角「解析：快速 / 精细」切换；装了才显示可用

## 命令行用法

```bash
E:/Python312/python.exe demo.py data/papers/resnet.pdf                  # 只翻摘要
E:/Python312/python.exe demo.py data/papers/resnet.pdf --sections all   # 全文
E:/Python312/python.exe eval.py                                         # 多篇评测，输出 out/eval.md
```

## 目录结构

```
app/
  config.py      模型/后端/分块/语言的唯一入口，业务代码不出现模型名
  llm.py         chat / chat_json / embed 适配层，支持 ollama 与 openai 兼容后端
  pdf_parse.py   版面解析 → 章节切分 → 分块；扫描件走 OCR
  terms.py       术语库：SQLite + CSV 导入导出 + 候选抽取 + 译文校验
  translate.py   段落/分块翻译 + 术语注入 + 违例重译 + 缓存
  cache.py       翻译缓存（模型 + 目标语言 + 术语库版本 + 原文哈希）
  extract.py     要素抽取 + evidence 本地回查 + 打分重排
  note.py        七段笔记 + 原文依据 + 批注 + 落库导出
  summarize.py   五段式整篇总结
  settings.py    模型提供方配置（本机 / DeepSeek / 豆包 / 自定义），存 data/settings.json
  search.py      笔记向量化与跨文献检索（无向量模型时降级关键词）
web_server.py    本地网页服务（仅标准库 http.server）
web/static/      index.html（桌面三栏）、mobile.html（手机拍照）
demo.py          端到端命令行
eval.py          多篇论文评测
warmup.py        演示前预热
```

## 实测数据

环境：RTX 4060 Laptop 8GB，`qwen2.5-coder:7b` Q4。

| 场景 | 结果 |
|---|---|
| 单块翻译（1800 字符） | 约 7 秒，稳态 50 tok/s |
| 摘要（1304 字符 / 2 段） | 7.6 秒；缓存命中后 0.01 秒 |
| 全篇（50885 字符 / 36 块） | 320.8 秒（5.3 分钟） |
| 解析 · 快速（12~22 页） | 8~40 秒 |
| 解析 · 精细（MinerU，12 页） | 28~36 秒，产出 15 个表格 HTML + 3 张图 + 2 条公式 |
| 要素抽取 | 5~20 秒，evidence 回查命中率 100%（样本 6 篇） |
| 多篇评测 | 6/6 成功，JSON 合法率 6/6，术语一致率 93.8%，41.5 tok/s |

## 设计取舍

- **不用 Flask/FastAPI**：界面服务只用标准库，端侧产品不该为 UI 多背一套运行时。
- **不用向量数据库**：笔记量级几百到几千小节，纯 Python 点积够用。
- **不复制 GPL/AGPL 项目源码**：只复用公开的接口设计与数据格式，见下表。

## 借鉴来源

| 来源 | 借鉴内容 | 许可 |
|---|---|---|
| BabelDOC | 术语表 source/target/tgt_lng 结构、CSV 交换、"命中才注入"、人工优先 | AGPL-3.0（仅思路） |
| paper-qa | 证据三段式：候选 → 打分 → 只用高分 | Apache-2.0 |
| PyMuPDF | 版面模型（pymupdf4llm）提供阅读顺序与标题层级 | AGPL-3.0（依赖调用） |
| EasyRead | 中英对照 + 原文可溯源 + 笔记导出的信息架构 | MIT |
| MinerU | 版面分析的产品形态参考（未直接使用） | 自定义 |

## 已知限制

- 扫描件 OCR 慢（15~40 秒/页），整篇扫描论文要等几分钟。
- 7B 模型的中文学术表达仍偏生硬，长章节偶有术语违例（BERT 那篇一致率 77.7%）。
- 术语一致率按"译文字面包含"判定，同义改写会被误判为违例。
- 公式给的是原图 + LaTeX 源码，不是可编辑的 MathML。
- 快速解析档没有 bbox，表格与插图只能放在章节末尾（精细档才回原位）。
- 任务状态存在内存里，重启服务后历史任务消失（译文有缓存，重跑很快）。
- 切到云端模型后，论文正文会发送到对应服务商，本地隐私保证不再成立。
- 手机通道需要同一局域网，且 Windows 防火墙要放行端口。

## 状态

课程结课 + 软件创新大赛选题 5。进度与待办见 `进度清单.md`。
