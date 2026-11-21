"""本地网页服务：127.0.0.1 上的单页应用后端。

只依赖 Python 标准库（http.server），不引入 Flask/FastAPI：
端侧产品不该为了界面多背一套运行时依赖，也方便演示时"双击就能跑"。

启动：
  E:\\Python312\\python.exe web_server.py
  E:\\Python312\\python.exe web_server.py --port 8080
然后浏览器打开 http://127.0.0.1:8000

接口一览：
  GET  /                       单页界面
  GET  /api/health             服务与模型状态
  GET  /api/terms              术语库
  POST /api/terms              新增/更新术语
  POST /api/terms/delete       删除术语
  GET  /api/terms/export       导出 CSV
  POST /api/terms/candidates   从论文里挖候选术语
  POST /api/upload?name=x.pdf  上传 PDF（原始字节）
  POST /api/run                启动处理任务，返回 task_id
  GET  /api/task/<id>          查询任务进度与结果
  GET  /api/notes              历史笔记
  GET  /api/note/<id>          单条笔记
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import config, extract, llm, note, pdf_parse, terms, translate

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "web", "static")
UPLOADS = os.path.join(ROOT, "data", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

MAX_UPLOAD = 80 * 1024 * 1024  # 80MB，够放一篇带图的论文

# 任务表：进程内内存，够用。重启服务即清空，历史结果在 notes 表里
TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()
LAST_TASK_ID: str | None = None  # 刷新页面后还能接回上一次的结果


# ---------------------------------------------------------------- 任务执行


def _new_task(pdf_path: str, kinds: list[str]) -> str:
    global LAST_TASK_ID
    tid = uuid.uuid4().hex[:12]
    LAST_TASK_ID = tid
    # 上传时为了防重名加了时间戳前缀，展示时要还原成人能读的论文名
    base = re.sub(r"^[0-9a-f]{10,}_", "", os.path.basename(pdf_path))
    title = os.path.splitext(base)[0]
    with TASK_LOCK:
        TASKS[tid] = {
            "id": tid,
            "pdf": pdf_path,
            "title": title,
            "status": "running",
            "stage": "解析",
            "stage_index": 1,
            "stage_total": 4,
            "progress": 0.0,
            "sections": [],
            "elements": None,
            "note_md": "",
            "note_id": None,
            "timings": {},
            "error": "",
            "started": datetime.now().strftime("%H:%M:%S"),
        }
    return tid


def _update(tid: str, **kw) -> None:
    with TASK_LOCK:
        if tid in TASKS:
            TASKS[tid].update(kw)


def _run_task(tid: str, pdf_path: str, kinds: list[str], title: str = "") -> None:
    """后台跑完整链路。每个阶段都回写进度，前端轮询即可。"""
    timings: dict[str, float] = {}
    try:
        # 1) 解析
        t0 = time.perf_counter()
        paper = pdf_parse.parse_pdf(pdf_path, backend=config.PDF_BACKEND)
        timings["解析"] = time.perf_counter() - t0
        if paper.needs_ocr:
            _update(tid, status="error", error="这是扫描件（无文本层），当前版本不支持 OCR")
            return

        sections = [
            {
                "kind": s.kind,
                "title": s.title,
                "page": s.page,
                "chars": s.char_count,
                "text": s.text.strip(),
                "translation": "",
                "term_rate": None,
                "violations": 0,
            }
            for s in paper.sections
        ]
        _update(
            tid,
            sections=sections,
            pages=paper.page_count,
            backend=paper.backend,
            timings=dict(timings),
            stage="翻译",
            stage_index=2,
            progress=0.05,
        )

        # 2) 翻译（按章节逐个推进，前端能边翻边读）
        targets = [i for i, s in enumerate(sections) if s["kind"] in kinds]
        if not targets:
            targets = [i for i, s in enumerate(sections) if s["kind"] in ("abstract",)]
        t0 = time.perf_counter()
        for n, idx in enumerate(targets, start=1):
            sec = sections[idx]
            try:
                r = translate.translate_text(sec["text"])
                sec["translation"] = r["full_text"]
                sec["term_rate"] = r["term_rate"]
                sec["violations"] = sum(len(c["verify"]["violations"]) for c in r["chunks"])
                sec["chunks"] = len(r["chunks"])
                sec["seconds"] = r["total_s"]
                sec["tok_s"] = r["tok_per_s"]
            except Exception as exc:
                sec["translation"] = ""
                sec["error"] = f"{type(exc).__name__}: {exc}"
            _update(tid, sections=sections, progress=0.05 + 0.6 * n / max(len(targets), 1))
        timings["翻译"] = time.perf_counter() - t0
        _update(tid, timings=dict(timings), stage="要素抽取", stage_index=3, progress=0.7)

        # 3) 抽取
        t0 = time.perf_counter()
        el = extract.extract_elements(paper)
        top = extract.rerank_evidence(el)
        timings["抽取"] = time.perf_counter() - t0
        payload = {
            "fields": {k: v for k, v in el.data.items() if k != "evidence"},
            "evidence": el.evidence_checked,
            "top_evidence": top,
            "report": el.report(),
        }
        _update(tid, elements=payload, timings=dict(timings), stage="生成笔记", stage_index=4, progress=0.85)

        # 4) 笔记
        t0 = time.perf_counter()
        rates = [s["term_rate"] for s in sections if s["term_rate"] is not None]
        md = note.build_note(el, title or os.path.basename(pdf_path))
        nid = note.save(
            pdf_path,
            title or os.path.basename(pdf_path),
            md,
            el,
            term_rate=sum(rates) / len(rates) if rates else 1.0,
        )
        timings["笔记"] = time.perf_counter() - t0

        _update(
            tid,
            status="done",
            note_md=md,
            note_id=nid,
            timings=dict(timings),
            progress=1.0,
            stage="完成",
        )
    except Exception as exc:
        _update(tid, status="error", error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ---------------------------------------------------------------- HTTP 层


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperReader/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志，只保留自己的
        pass

    # ---------- 工具 ----------

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        if path in ("/", "/index.html"):
            self._file(os.path.join(STATIC, "index.html"))
            return
        if path == "/api/health":
            self._json(
                {
                    "ok": llm.health(),
                    "model": config.MAIN_MODEL,
                    "backend": config.BACKEND,
                    "base_url": config.BASE_URL,
                    "terms": len(terms.all_terms()),
                    "target_lng": config.TARGET_LNG,
                }
            )
            return
        if path == "/api/terms":
            self._json({"terms": terms.all_terms()})
            return
        if path == "/api/terms/export":
            csv_path = os.path.join(ROOT, "data", "glossary_export.csv")
            terms.export_csv(csv_path)
            self._file(csv_path)
            return
        if path == "/api/notes":
            self._json({"notes": note.list_notes()})
            return
        if path.startswith("/api/note/"):
            nid = int(path.rsplit("/", 1)[-1])
            row = note.load(nid)
            self._json(row or {"error": "not found"}, 200 if row else 404)
            return
        if path.startswith("/api/task/"):
            tid = path.rsplit("/", 1)[-1]
            if tid == "last":
                tid = LAST_TASK_ID or ""
            with TASK_LOCK:
                task = TASKS.get(tid)
                task = json.loads(json.dumps(task)) if task else None
            if not task:
                self._json({"error": "任务不存在"}, 404)
                return
            self._json(task)
            return

        # 静态资源（css/js/图片）
        rel = path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        candidate = os.path.normpath(os.path.join(STATIC, rel))
        if candidate.startswith(STATIC) and os.path.isfile(candidate):
            self._file(candidate)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        if path == "/api/upload":
            name = (q.get("name") or ["paper.pdf"])[0]
            name = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", os.path.basename(name))
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"error": "空文件"}, 400)
                return
            if length > MAX_UPLOAD:
                self._json({"error": "文件超过 80MB"}, 400)
                return
            dest = os.path.join(UPLOADS, f"{int(time.time())}_{name}")
            with open(dest, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            # 同一篇论文重复上传不该堆副本：按内容哈希命名，内容相同就复用
            with open(dest, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()[:10]
            final = os.path.join(UPLOADS, f"{digest}_{name}")
            if os.path.exists(final):
                os.remove(dest)
            else:
                os.replace(dest, final)
            self._json({"path": os.path.relpath(final, ROOT).replace("\\", "/"), "name": name})
            return

        if path == "/api/run":
            data = self._body()
            pdf_path = data.get("path", "")
            abs_path = os.path.normpath(os.path.join(ROOT, pdf_path))
            if not os.path.isfile(abs_path) or not abs_path.lower().endswith(".pdf"):
                self._json({"error": "PDF 路径无效"}, 400)
                return
            kinds = data.get("kinds") or ["abstract", "introduction", "method", "experiment", "conclusion"]
            tid = _new_task(abs_path, kinds)
            title = TASKS[tid]["title"]
            threading.Thread(
                target=_run_task, args=(tid, abs_path, kinds, title), daemon=True
            ).start()
            self._json({"task_id": tid})
            return

        if path == "/api/terms":
            data = self._body()
            if not data.get("source") or not data.get("target"):
                self._json({"error": "source 与 target 必填"}, 400)
                return
            tid = terms.add_term(
                data["source"],
                data["target"],
                domain=data.get("domain") or "general",
                definition=data.get("definition") or "",
                mode=data.get("mode") or "force",
                origin="manual",
                tgt_lng=data.get("tgt_lng") or "",
            )
            self._json({"id": tid})
            return

        if path == "/api/terms/delete":
            data = self._body()
            src = (data.get("source") or "").strip()
            if not src:
                self._json({"error": "source 必填"}, 400)
                return
            n = terms.delete_term(src)
            self._json({"deleted": n})
            return

        if path == "/api/terms/candidates":
            data = self._body()
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "text 必填"}, 400)
                return
            try:
                cands = terms.extract_candidates(text, limit=int(data.get("limit") or 12))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._json({"candidates": cands})
            return

        if path == "/api/terms/accept":
            data = self._body()
            cands = data.get("candidates") or []
            n = terms.accept_candidates(cands, domain=data.get("domain") or "general")
            self._json({"accepted": n})
            return

        self._json({"error": "not found"}, 404)


def main() -> int:
    ap = argparse.ArgumentParser(description="本地网页界面")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机，不要改成 0.0.0.0")
    args = ap.parse_args()

    if not llm.health():
        print(f"[警告] 模型服务 {config.BASE_URL} 没响应，界面能打开但跑不了任务")
        print('       启动：export OLLAMA_MODELS="E:/模型" && "/e/模型/Ollama/ollama.exe" serve')

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"界面已启动：http://{args.host}:{args.port}")
    print(f"模型：{config.MAIN_MODEL}　术语库：{len(terms.all_terms())} 条")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
