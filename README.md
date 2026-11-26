# PaperReader · 端侧双语学术文献精读助手

读英文论文的三件事——看懂、查术语、做笔记——全在**本机**完成，不联网、不上传。

模型跑在本地（Ollama + 7B），PDF 解析、翻译、要素抽取、笔记生成、跨文献检索都在自己的电脑上算。
适合对数据敏感的场景：未公开的实验数据、企业内部技术报告、审稿中的稿件。

## 能做什么

| 能力 | 说明 |
|---|---|
| PDF 解析 | 双后端：PyMuPDF 版面模型（快，约 12s/篇）或 MinerU（准，约 30s/篇，带 bbox） |
| 图表保留 | 表格按结构渲染（MinerU 档保留合并单元格）、插图与公式按 bbox 裁剪并**回到正文原位**、每页原页快照兜底 |
| 公式 | 精细档用本地 KaTeX 渲染（可选中复制，带 MathML），下方保留原图对照 |
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
| 图片翻译 | 选图 / 拖拽 / Ctrl+V 粘贴，本机 OCR（支持双栏重排）后直接出原文+译文对照，历史记录自动落库可回看与删除 |
| 公式解读 | 点正文里的公式，弹出「一句话说明 + 符号表 + 为什么这么写」，只依据上下文不编 |
| 问论文 | 右下角对话框就当前论文提问，正文选中一句话可「加入对话」带着原文问，回答标注页码 |

## 快速开始

需要 Python 3.12（开发环境是 3.12.10）。

```bash
git clone <仓库地址> && cd three
python -m pip install -r requirements.txt

# 1) 配置模型：仓库自带 settings.json（含作者的 Key）；要用自己的就改成模板再填
#    cp data/settings.example.json data/settings.json

# 2) 本机模型（可选，不配云端才需要）
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
ollama serve

# 3) 启动
#    Windows 可直接双击根目录的「启动PaperReader.bat」（自动防重起 + 打开浏览器）
#    演示前（使用本机 Ollama 模型时）可先跑一次 python warmup.py，省掉约 4 秒首次模型加载
python web_server.py            # 只本机访问
python web_server.py --lan      # 允许局域网访问
```

浏览器打开 http://127.0.0.1:8000 。

### 配置说明

- `data/settings.json` 存本机配置，**按作者要求随仓库提交，含 API Key**
- 借用仓库前请注意：用的是作者自己的额度，别拿去做批量任务
- 想用自己的 Key：复制 `data/settings.example.json` 覆盖，或在界面右上角「模型」里填
- 没有配置文件时默认走本机 Ollama，不会崩
- 界面右上角「模型」可切换：本机 / DeepSeek / 豆包（火山方舟）/ 自定义 OpenAI 兼容
- 用豆包要注意：默认会开「深度思考」，程序已自动关闭（`thinking: disabled`），否则翻译两句话能烧 5000 token

### 可选：MinerU 精细解析

默认走 PyMuPDF（12 秒/篇）。要更准的版面、表格合并单元格、图片 bbox、公式 LaTeX，装 MinerU：

```bash
python -m venv .venv-mineru
.venv-mineru/Scripts/python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv-mineru/Scripts/python.exe -m pip install "mineru[torch]"
```

- 装在独立 venv：torch / transformers 5.x 与主环境冲突，不能共装
- 模型 835MB，首次运行自动下载；默认放在 `E:/模型/mineru`（可用 `PAPERREADER_MODELS` 改），
  不要放仓库目录——单个 onnx 591MB，GitHub 会拒收
- 推理走 DirectML，直接使用 NVIDIA 显卡，不需要 CUDA
- 界面右上角「解析：快速 / 精细」切换；装了才显示可用

## 命令行用法

```bash
python demo.py data/papers/resnet.pdf                  # 只翻摘要
python demo.py data/papers/resnet.pdf --sections all   # 全文
python eval.py                                         # 多篇评测，输出 out/eval.md
python security_test.py                                # 20 项安全与健壮性回归（需先启动服务）
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
security_test.py 20 项安全回归（同源、路径穿越、请求体边界、Key 清理等）
warmup.py        演示前预热
```

## 实测数据

环境：RTX 4060 Laptop 8GB，`qwen2.5-coder:7b` Q4。

| 场景 | 结果 |
|---|---|
| 单块翻译（1800 字符） | 约 7 秒，稳态 50 tok/s |
| 摘要（1304 字符 / 2 段） | 7.6 秒；缓存命中后 0.01 秒 |
| 全篇（50885 字符 / 36 块） | 320.8 秒（5.3 分钟） |
| 解析 · 快速（12~22 页） | 8~24 秒，图表按 bbox 回原位（表格以原图呈现） |
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
- 公式给的是 KaTeX 渲染 + 原图对照；**快速档没有 LaTeX**，只能显示公式原图。
- 快速档的表格是原页裁图，不能复制单元格文字；要结构化表格请用精细档。
- 任务状态存在内存里，重启服务后历史任务消失；但处理完成的论文会自动保存精读快照到 `data/notes.db`（`paper_snapshot` 表），再次上传同一篇或在「历史笔记」点「打开阅读」可直接秒开，也可以随时点「重新解析翻译」按最新术语重跑。
- 切到云端模型后，论文正文会发送到对应服务商，本地隐私保证不再成立。
- 手机通道需要同一局域网，且 Windows 防火墙要放行端口。

## 状态

课程结课 + 软件创新大赛选题 5。进度与待办见 `进度清单.md`。
