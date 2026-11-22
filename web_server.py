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
import socket
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from urllib.parse import parse_qs, urlparse

from app import cache, config, extract, llm, note, pdf_parse, search, settings, summarize, terms, translate

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "web", "static")
UPLOADS = os.path.join(ROOT, "data", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

MAX_UPLOAD = 80 * 1024 * 1024  # 80MB，够放一篇带图的论文

# 任务表：进程内内存，够用。重启服务即清空，历史结果在 notes 表里
TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()
LAST_TASK_ID: str | None = None  # 刷新页面后还能接回上一次的结果
# 本地 8GB 显存扛不住并行推理，同一时间只跑一个任务，其余排队
_RUN_SLOTS = threading.Semaphore(1)


def _lan_ip() -> str:
    """本机在局域网里的地址。UDP connect 不会真的发包，只用来让系统选出口网卡。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _decode_cmd(data: bytes) -> str:
    """netsh 的输出在不同系统语言/版本下可能是 UTF-8 或 GBK，两种都试。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def _wifi_info() -> dict:
    """当前连的 WiFi 名称。校园网/公共 WiFi 常开客户端隔离，手机连不上本机。"""
    info = {"ssid": "", "signal": ""}
    try:
        out = _decode_cmd(
            subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True).stdout
        )
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("SSID") and "BSSID" not in s:
                info["ssid"] = s.split(":", 1)[-1].strip()
            elif s.startswith("信号") or s.lower().startswith("signal"):
                info["signal"] = s.split(":", 1)[-1].strip()
    except Exception:
        pass
    return info


def _firewall_state(port: int) -> dict:
    """查 Windows 防火墙是否放行了这个端口。

    手机扫码连不上，九成是这里：WLAN 常被识别为「公用网络」，
    默认 BlockInbound，浏览器能开本机页面但手机连不进来。
    """
    rule = f"PaperReader {port}"
    info = {"rule": False, "profile": "", "blocked": None, "port": port}
    try:
        out = _decode_cmd(
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
                capture_output=True,
            ).stdout
        )
        # 有规则时输出里会带规则名；没有时是一句"没有与指定标准相匹配的规则"
        info["rule"] = rule in out
    except Exception:
        pass
    try:
        out = _decode_cmd(
            subprocess.run(
                ["netsh", "advfirewall", "show", "currentprofile"], capture_output=True
            ).stdout
        )
        for name in ("公用", "专用", "域"):
            if f"{name}配置文件" in out:
                info["profile"] = name
                break
        info["blocked"] = "BlockInbound" in out
    except Exception:
        pass
    return info


# ---------------------------------------------------------------- 任务执行


def _media_url(path: str) -> str:
    """data/media/... → /media/...，给前端用。"""
    try:
        rel = os.path.relpath(path, os.path.join(config.DATA_DIR, "media"))
        return "/media/" + rel.replace("\\", "/")
    except Exception:
        return ""


def _section_media(paper, idx: int) -> dict:
    """把某章节覆盖的页里的插图、整页快照整理成前端能直接用的 URL 列表。"""
    sec = paper.sections[idx]
    nxt = paper.sections[idx + 1].page if idx + 1 < len(paper.sections) else paper.page_count + 1
    # 同一页上常有多个章节，end 至少要比 start 大 1，否则这一节拿不到任何原页
    start = sec.page
    end = max(start + 1, min(nxt, paper.page_count + 1))

    figs, snaps = [], []
    for pno in range(start, end):
        for f in paper.figures.get(pno, []):
            figs.append(
                {
                    "page": pno,
                    "url": _media_url(f),
                    "caption": paper.captions.get(f, ""),
                }
            )
        if pno in paper.page_images:
            snaps.append({"page": pno, "url": _media_url(paper.page_images[pno])})
    return {"figures": figs, "page_images": snaps}


def _translate_remaining(tid: str) -> None:
    """补译当前任务里还没翻译的章节。

    为什么单独做：解析和翻译分开之后，用户可以先用"仅摘要"快速看，
    觉得值得再补译，不必从头重跑（译文有缓存，补译只花没翻过的那部分）。
    """
    if not _RUN_SLOTS.acquire(blocking=False):
        _update(tid, status="queued", stage="排队中")
        _RUN_SLOTS.acquire()
    try:
        with TASK_LOCK:
            t = TASKS.get(tid)
            if not t:
                return
            snapshot = [
                (i, s["kind"], list(s.get("paras") or []))
                for i, s in enumerate(t["sections"])
            ]
        skip = ("references", "acknowledg", "front")
        todo = [
            i
            for i, kind, paras in snapshot
            if kind not in skip and not any(p.get("translation") for p in paras)
        ]
        if not todo:
            _update(tid, status="done", stage="完成", progress=1.0)
            return

        _update(tid, status="running", stage="补译", stage_index=2, progress=0.0)
        for n, idx in enumerate(todo, start=1):
            with TASK_LOCK:
                paras = list((TASKS[tid]["sections"][idx].get("paras") or []))
            src = [{"page": p.get("page"), "text": p.get("source")} for p in paras]
            if not src:
                continue
            try:
                r = translate.translate_paragraphs(src)
                with TASK_LOCK:
                    TASKS[tid]["sections"][idx]["paras"] = r["paras"]
                    TASKS[tid]["sections"][idx]["term_rate"] = r["term_rate"]
                    TASKS[tid]["sections"][idx]["violations"] = r["violations"]
            except Exception as exc:
                with TASK_LOCK:
                    TASKS[tid]["sections"][idx]["error"] = f"{type(exc).__name__}: {exc}"
            _update(tid, progress=round(n / len(todo), 3))
        _update(tid, status="done", stage="完成", progress=1.0)
    finally:
        _RUN_SLOTS.release()


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
            "summary": "",
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


def _run_task(tid: str, pdf_path: str, kinds: list[str], title: str = "", backend: str = "") -> None:
    """后台跑完整链路。每个阶段都回写进度，前端轮询即可。

    只允许一个任务同时跑（本地 8GB 显存扛不住并行），后来的排队等。
    """
    if not _RUN_SLOTS.acquire(blocking=False):
        _update(tid, status="queued", stage="排队中", stage_index=0)
        _RUN_SLOTS.acquire()

    def cancelled() -> bool:
        with TASK_LOCK:
            return bool(TASKS.get(tid, {}).get("cancel"))

    try:
        timings: dict[str, float] = {}
        # 1) 解析
        t0 = time.perf_counter()
        paper = pdf_parse.parse_pdf(pdf_path, backend=backend or config.PDF_BACKEND)
        timings["解析"] = time.perf_counter() - t0
        if paper.needs_ocr and not paper.ocr_used:
            _update(tid, status="error", error="扫描件识别失败，可能是图片质量太低")
            return

        sections = []
        for i, s in enumerate(paper.sections):
            media = _section_media(paper, i)
            # 版面流：正文 / 表格 / 插图 / 公式按原位置混排；图片路径转成 URL
            flow = []
            for item in s.flow:
                it = dict(item)
                if it["type"] in ("figure", "formula", "table") and it.get("path"):
                    it["url"] = _media_url(it.pop("path"))
                flow.append(it)
            sections.append(
                {
                    "kind": s.kind,
                    "title": s.title,
                    "page": s.page,
                    "chars": s.char_count,
                    "text": s.text.strip(),
                    # 先把原文段落填进去：没翻译的章节也该能读英文，而不是一片空白
                    "paras": [
                        {
                            "page": p.get("page", s.page),
                            "source": p.get("text", ""),
                            "translation": "",
                            "aligned": True,
                            "cached": False,
                        }
                        for p in (s.paras or [{"page": s.page, "text": s.text}])
                    ],
                    "tables": s.tables,
                    "flow": flow,
                    "figures": media["figures"],
                    "page_images": media["page_images"],
                    "term_rate": None,
                    "violations": 0,
                    "chunks": 0,
                    "seconds": 0.0,
                    "cache_hits": 0,
                }
            )
        _update(
            tid,
            sections=sections,
            pages=paper.page_count,
            backend=paper.backend,
            ocr_used=paper.ocr_used,
            timings=dict(timings),
            status="running",
            stage="翻译",
            stage_index=2,
            progress=0.05,
        )

        # 2) 翻译：按章节推进，章节内按段落推进，前端能边翻边读
        if "*" in kinds:
            # 全文 = 除参考文献/致谢/题名页外的所有章节，包括版面模型认不出名字的 other
            # 题名页是标题、作者、邮箱，翻了只会出现"Kaiming He → Kaiming He"这种噪音
            skip = ("references", "acknowledg", "front")
            targets = [i for i, s in enumerate(sections) if s["kind"] not in skip]
        else:
            targets = [i for i, s in enumerate(sections) if s["kind"] in kinds]
        if not targets:
            targets = [i for i, s in enumerate(sections) if s["kind"] in ("abstract",)]
        t0 = time.perf_counter()
        done_units = 0
        total_units = sum(len(paper.sections[i].paras) or 1 for i in targets) or 1
        for idx in targets:
            if cancelled():
                _update(tid, status="cancelled", stage="已取消")
                return
            sec = sections[idx]
            src_section = paper.sections[idx]
            try:
                r = translate.translate_paragraphs(src_section.paras or [
                    {"page": src_section.page, "text": src_section.text}
                ])
                sec["paras"] = r["paras"]
                sec["term_rate"] = r["term_rate"]
                sec["violations"] = r["violations"]
                sec["chunks"] = r["chunks"]
                sec["seconds"] = r["total_s"]
                sec["cache_hits"] = r["cache_hits"]
                sec["aligned_rate"] = r["aligned_rate"]
            except Exception as exc:
                sec["error"] = f"{type(exc).__name__}: {exc}"
            done_units += len(src_section.paras) or 1
            _update(
                tid,
                sections=sections,
                progress=0.05 + 0.6 * min(done_units / total_units, 1.0),
            )
        timings["翻译"] = time.perf_counter() - t0
        _update(tid, timings=dict(timings), stage="要素抽取", stage_index=3, progress=0.7)

        if cancelled():
            _update(tid, status="cancelled", stage="已取消")
            return

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
        md = note.build_note(
            el,
            title or os.path.basename(pdf_path),
            context=summarize.build_context(paper.sections, 5000),
        )
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
    finally:
        _RUN_SLOTS.release()


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
        if path in ("/mobile", "/mobile.html"):
            self._file(os.path.join(STATIC, "mobile.html"))
            return
        if path == "/api/qr":
            text = (q.get("text") or [""])[0]
            if not text:
                self._json({"error": "text 必填"}, 400)
                return
            try:
                import io

                import qrcode

                img = qrcode.make(text)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
            except Exception as exc:
                self._json({"error": f"生成二维码失败：{exc}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/lan":
            port = self.server.server_address[1]
            ip = _lan_ip()
            fw = _firewall_state(port)
            self._json(
                {
                    "url": f"http://{ip}:{port}/mobile",
                    "ip": ip,
                    "port": port,
                    "wifi": _wifi_info(),
                    "firewall": fw,
                    # 给用户一键复制：加规则需要管理员权限，程序自己提不了权
                    "fix_cmd": (
                        f'netsh advfirewall firewall add rule name="PaperReader {port}" '
                        f"dir=in action=allow protocol=TCP localport={port}"
                    ),
                }
            )
            return
        if path == "/api/search":
            query = (q.get("q") or [""])[0]
            top_k = int((q.get("k") or ["5"])[0])
            self._json(search.search(query, top_k=top_k))
            return
        if path.startswith("/media/"):
            rel = path[len("/media/"):]
            base = os.path.join(config.DATA_DIR, "media")
            candidate = os.path.normpath(os.path.join(base, rel))
            if candidate.startswith(base) and os.path.isfile(candidate):
                self._file(candidate)
                return
            self._json({"error": "not found"}, 404)
            return
        if path == "/api/settings":
            self._json(settings.public_view())
            return
        if path == "/api/health":
            cfg = settings.current()
            self._json(
                {
                    "ok": llm.health(),
                    "model": cfg.get("model"),
                    "backend": cfg.get("backend"),
                    "provider": cfg.get("name"),
                    "label": cfg.get("label"),
                    "base_url": cfg.get("base_url"),
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
        # 导出笔记：走服务端下载，比前端 blob + a.click() 稳（后者在部分 Chrome 配置下会弹"另存为"且存成空文件）
        m = re.match(r"^/api/note/(\d+)/export$", path)
        if m:
            row = note.load(int(m.group(1)))
            if not row:
                self._json({"error": "not found"}, 404)
                return
            body = row.get("note_md") or ""
            if row.get("user_note"):
                body += "\n\n## 我的批注\n\n" + row["user_note"].strip() + "\n"
            if not body.strip():
                self._json({"error": "这条笔记是空的"}, 404)
                return
            data = body.encode("utf-8")
            fname = re.sub(r'[\\/:*?"<>|]+', "_", (row.get("title") or "note"))[:60] + ".md"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=note.md; filename*=UTF-8''{quote(fname)}",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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

        if path == "/api/settings":
            data = self._body()
            if data.get("name"):
                settings.update(data["name"], **data)
            if data.get("active"):
                settings.set_active(data["active"])
            self._json(settings.public_view())
            return

        if path == "/api/retranslate":
            data = self._body()
            tid = data.get("task_id") or LAST_TASK_ID or ""
            with TASK_LOCK:
                exists = tid in TASKS
            if not exists:
                self._json({"error": "任务不存在"}, 404)
                return
            threading.Thread(target=_translate_remaining, args=(tid,), daemon=True).start()
            self._json({"ok": True, "task_id": tid})
            return

        if path == "/api/summary":
            data = self._body()
            tid = data.get("task_id") or LAST_TASK_ID or ""
            with TASK_LOCK:
                t = json.loads(json.dumps(TASKS.get(tid) or {}))
            if not t:
                self._json({"error": "任务不存在"}, 404)
                return
            from types import SimpleNamespace

            secs = [
                SimpleNamespace(kind=s.get("kind", ""), text=s.get("text") or "")
                for s in t.get("sections", [])
            ]
            try:
                md = summarize.summarize(secs, t.get("title", ""))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            _update(tid, summary=md)
            self._json({"summary": md})
            return

        # 取消任务：worker 在章节之间检查这个标记
        m = re.match(r"^/api/task/([0-9a-zA-Z]+)/cancel$", path)
        if m:
            tid = m.group(1)
            with TASK_LOCK:
                t = TASKS.get(tid)
                if not t:
                    self._json({"error": "任务不存在"}, 404)
                    return
                t["cancel"] = True
                if t["status"] == "queued":
                    t["status"] = "cancelled"
                    t["stage"] = "已取消"
            self._json({"ok": True})
            return

        # 改笔记正文或批注
        m = re.match(r"^/api/note/(\d+)$", path)
        if m:
            nid = int(m.group(1))
            data = self._body()
            ok = note.update(
                nid,
                note_md=data.get("note_md"),
                user_note=data.get("user_note"),
            )
            self._json({"ok": ok})
            return

        if path == "/api/cache/clear":
            self._json({"cleared": cache.clear()})
            return

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
            kinds = data.get("kinds") or ["*"]
            backend = (data.get("backend") or "").strip()
            tid = _new_task(abs_path, kinds)
            title = TASKS[tid]["title"]
            threading.Thread(
                target=_run_task, args=(tid, abs_path, kinds, title, backend), daemon=True
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

        # 图片翻译：上传一张图 → OCR → 翻译，一步到位（替代原来的手机拍照通道）
        if path == "/api/image/translate":
            name = (q.get("name") or ["image.png"])[0]
            name = re.sub(r"[^\w.\-]", "_", os.path.basename(name)) or "image.png"
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"error": "空文件"}, 400)
                return
            if length > MAX_UPLOAD:
                self._json({"error": "图片超过 80MB"}, 400)
                return
            dest = os.path.join(UPLOADS, f"img_{int(time.time())}_{name}")
            with open(dest, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            t0 = time.perf_counter()
            try:
                text = pdf_parse.ocr_image(dest)
            except Exception as exc:
                self._json({"error": f"OCR 失败：{exc}"}, 500)
                return
            if not text.strip():
                self._json({"error": "这张图里没认出文字"}, 200)
                return

            # 按空行切段再翻译：整块送进去，模型容易漏译后半段
            paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            if not paras:
                paras = [text.strip()]
            try:
                r = translate.translate_paragraphs(
                    [{"page": 1, "text": p} for p in paras]
                )
            except Exception as exc:
                self._json(
                    {
                        "error": f"翻译失败：{exc}",
                        "text": text,
                        "seconds": round(time.perf_counter() - t0, 1),
                    },
                    200,
                )
                return

            self._json(
                {
                    "text": text,
                    "translation": "\n\n".join(
                        p.get("translation", "") for p in r["paras"]
                    ),
                    "paras": r["paras"],
                    "chars": len(text),
                    "seconds": round(time.perf_counter() - t0, 1),
                    "term_rate": r.get("term_rate"),
                }
            )
            return

        if path == "/api/mobile/ocr":
            name = (q.get("name") or ["photo.jpg"])[0]
            name = re.sub(r"[^\w.\-]", "_", os.path.basename(name)) or "photo.jpg"
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"error": "空文件"}, 400)
                return
            if length > MAX_UPLOAD:
                self._json({"error": "图片过大"}, 400)
                return
            dest = os.path.join(UPLOADS, f"shot_{int(time.time())}_{name}")
            with open(dest, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            t0 = time.perf_counter()
            try:
                text = pdf_parse.ocr_image(dest)
            except Exception as exc:
                self._json({"error": f"OCR 失败：{exc}"}, 500)
                return
            self._json(
                {
                    "text": text,
                    "chars": len(text),
                    "seconds": round(time.perf_counter() - t0, 1),
                }
            )
            return

        if path == "/api/mobile/translate":
            data = self._body()
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "text 必填"}, 400)
                return
            try:
                r = translate.translate_text(text)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._json(
                {
                    "zh": r["full_text"],
                    "chunks": len(r["chunks"]),
                    "term_rate": r["term_rate"],
                    "seconds": r["total_s"],
                }
            )
            return

        if path == "/api/search/index":
            self._json(search.index_all(force=bool((self._body() or {}).get("force"))))
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
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    ap.add_argument(
        "--lan",
        action="store_true",
        help="同时允许局域网访问（手机拍照通道需要），会监听 0.0.0.0",
    )
    args = ap.parse_args()
    host = "0.0.0.0" if args.lan else args.host

    if not llm.health():
        print(f"[警告] 模型服务 {config.BASE_URL} 没响应，界面能打开但跑不了任务")
        print('       启动：export OLLAMA_MODELS="E:/模型" && "/e/模型/Ollama/ollama.exe" serve')

    # 启动时给笔记建索引：没有索引的话跨文献检索是空的，用户会以为功能坏了
    def _index():
        try:
            stat = search.index_all()
            if stat["indexed"]:
                print(f"检索索引：新增 {stat['indexed']} 条笔记 / {stat['chunks']} 个小节")
        except Exception as exc:
            print(f"[警告] 建检索索引失败：{exc}")

    threading.Thread(target=_index, daemon=True).start()

    srv = ThreadingHTTPServer((host, args.port), Handler)
    print(f"界面已启动：http://127.0.0.1:{args.port}")
    if args.lan:
        print(f"手机拍照：http://{_lan_ip()}:{args.port}/mobile")
    else:
        print("（手机拍照通道需加 --lan 启动）")
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
