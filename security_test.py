"""安全与健壮性回归测试（20 项）。

跑法（服务要先启动）：
    E:/Python312/python.exe security_test.py
    E:/Python312/python.exe security_test.py --base http://127.0.0.1:8000

设计原则：
  - 只读为主，会写配置/术语的两项自带备份与还原
  - 每项独立，一项失败不影响后续
  - 最后打印通过率，全过返回码 0
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "通过" if ok else "失败"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))


def req(path: str, method: str = "GET", body: bytes | None = None,
        headers: dict | None = None, timeout: int = 15) -> tuple[int, str]:
    """发一个请求，返回 (状态码, 响应文本)。4xx/5xx 不抛异常。"""
    url = BASE + path
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with OPENER.open(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")
    except Exception as exc:
        return 0, str(exc)


def post_json(path: str, payload: dict, extra_headers: dict | None = None) -> tuple[int, str]:
    h = {"Content-Type": "application/json"}
    if extra_headers:
        h.update(extra_headers)
    return req(path, "POST", json.dumps(payload).encode("utf-8"), h)


# ---------------------------------------------------------------- HTTP 层

def t01_health() -> None:
    code, text = req("/api/health")
    ok = code == 200 and '"ok"' in text
    record("健康接口可访问且返回状态", ok, f"HTTP {code}")


def t02_origin_blocked() -> None:
    code, text = post_json("/api/run", {"path": "data/papers/resnet.pdf"},
                           {"Origin": "http://evil.example.com"})
    ok = code == 403
    record("外部 Origin 的跨站请求被拒", ok, f"HTTP {code}")


def t03_origin_allowed() -> None:
    code, _ = post_json("/api/run", {"path": "nope.pdf"},
                        {"Origin": BASE})
    ok = code == 400  # 同源放行，只是路径无效
    record("同源请求正常放行（未被误杀）", ok, f"HTTP {code}")


def t04_content_type() -> None:
    code, text = req("/api/run", "POST", b'{"path":"x.pdf"}',
                     {"Content-Type": "text/plain"})
    ok = code == 415 and "application/json" in text
    record("非 JSON 请求体被拒", ok, f"HTTP {code}")


def t05_body_too_large() -> None:
    big = b'{"x":"' + b"a" * (3 * 1024 * 1024) + b'"}'
    code, text = req("/api/run", "POST", big, {"Content-Type": "application/json"})
    ok = code == 413
    record("超大请求体被拒（>2MB）", ok, f"HTTP {code} {text[:40]}")


def t06_path_traversal_run() -> None:
    code, text = post_json("/api/run", {"path": "../../../Windows/win.ini"})
    ok = code == 400 and "路径无效" in text
    record("/api/run 路径穿越被拦", ok, f"HTTP {code}")


def t07_non_pdf_run() -> None:
    code, text = post_json("/api/run", {"path": "README.md"})
    ok = code == 400
    record("/api/run 拒绝非 PDF 文件", ok, f"HTTP {code}")


def t08_media_traversal() -> None:
    code, _ = req("/media/../../data/settings.json")
    ok = code in (400, 403, 404)
    record("/media 路径穿越被拦", ok, f"HTTP {code}")


def t09_upload_fake_pdf() -> None:
    code, text = req("/api/upload?name=fake.pdf", "POST", b"NOTAPDF" * 100,
                     {"Content-Type": "application/octet-stream"})
    ok = code == 400 and "PDF" in text
    record("伪造 PDF（文件头不符）被拒", ok, f"HTTP {code}")


def t10_upload_empty() -> None:
    code, text = req("/api/upload?name=e.pdf", "POST", b"",
                     {"Content-Type": "application/octet-stream"})
    ok = code == 400
    record("空文件上传被拒", ok, f"HTTP {code}")


def t11_note_export_404() -> None:
    code, _ = req("/api/note/99999999/export")
    ok = code == 404
    record("导出不存在的笔记返回 404", ok, f"HTTP {code}")


def t12_task_404() -> None:
    code, _ = req("/api/task/notexist123")
    ok = code == 404
    record("查询不存在的任务返回 404", ok, f"HTTP {code}")


def t13_cancel_404() -> None:
    code, _ = req("/api/task/notexist123/cancel", "POST", b"{}",
                  {"Content-Type": "application/json"})
    ok = code == 404
    record("取消不存在的任务返回 404", ok, f"HTTP {code}")


def t14_unknown_route() -> None:
    code, _ = req("/api/definitely-not-here")
    ok = code == 404
    record("未知接口返回 404", ok, f"HTTP {code}")


def t15_static_traversal() -> None:
    code, _ = req("/static/../../web_server.py")
    ok = code in (400, 403, 404)
    record("/static 目录穿越被拦", ok, f"HTTP {code}")


def t16_settings_save_no_crash() -> None:
    """保存模型配置：历史上这里有过 name 参数冲突导致必崩。"""
    code, text = post_json("/api/settings", {
        "name": "custom", "label": "自定义", "backend": "openai",
        "base_url": "https://example.com/v1", "model": "test-model",
    })
    ok = code == 200 and '"error"' not in text
    record("保存模型配置不崩溃（参数冲突已修）", ok, f"HTTP {code}")


def t17_key_cleared_on_host_change() -> None:
    """改 base_url 的域名时要自动清空旧 Key，避免把 Key 发给陌生地址。"""
    path = os.path.join(ROOT, "data", "settings.json")
    backup = path + ".testbak"
    shutil.copyfile(path, backup)
    try:
        post_json("/api/settings", {
            "name": "custom", "backend": "openai",
            "base_url": "https://host-a.example/v1", "model": "m1",
            "api_key": "sk-test-aaaaaaaa",
        })
        post_json("/api/settings", {
            "name": "custom", "base_url": "https://host-b.example/v1",
        })
        data = json.load(open(path, encoding="utf-8"))
        key = data["providers"]["custom"].get("api_key") or ""
        ok = key == ""
        record("换域名自动清空旧 API Key", ok, f"key={'空' if not key else '仍存在'}")
    finally:
        shutil.copyfile(backup, path)
        os.remove(backup)


def t18_health_reports_capability() -> None:
    code, text = req("/api/health")
    ok = code == 200 and '"mineru"' in text
    record("健康接口上报精细解析可用性", ok, "")


# ---------------------------------------------------------------- 库层

def t19_cache_key_follows_model() -> None:
    from app import cache, llm

    src = "security-test-sentence"
    key1 = cache.make_key(src)
    original = llm.model_name
    try:
        llm.model_name = lambda: "security-test-model-b"
        key2 = cache.make_key(src)
    finally:
        llm.model_name = original
    ok = key1 != key2
    record("缓存键随模型变化（换模型不串译文）", ok, "")


def t20_terms_variant_cascade() -> None:
    from app import terms

    src, tgt = "security-test-term", "安全测试术语"
    tid = terms.add_term(src, tgt, variants=["security-test-variant"])
    found = [t for t in terms.all_terms() if t["source"] == src]
    has_variant = bool(found and found[0].get("variants"))
    terms.delete_term(src)
    after = [t for t in terms.all_terms() if t["source"] == src]
    ok = has_variant and not after
    record("术语删除联动清理变体", ok, f"新增 id={tid}")


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description="安全与健壮性回归测试")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print("=" * 56)
    print(f"安全与健壮性回归测试 · {BASE}")
    print("=" * 56)

    code, _ = req("/api/health", timeout=5)
    if code == 0:
        print(f"[错误] 连不上 {BASE}，请先启动 web_server.py")
        return 2

    http_tests = [
        t01_health, t02_origin_blocked, t03_origin_allowed, t04_content_type,
        t05_body_too_large, t06_path_traversal_run, t07_non_pdf_run,
        t08_media_traversal, t09_upload_fake_pdf, t10_upload_empty,
        t11_note_export_404, t12_task_404, t13_cancel_404, t14_unknown_route,
        t15_static_traversal, t16_settings_save_no_crash,
        t17_key_cleared_on_host_change, t18_health_reports_capability,
    ]
    print("\n一、接口与访问控制")
    for fn in http_tests:
        try:
            fn()
        except Exception as exc:
            record(fn.__name__, False, f"异常：{type(exc).__name__}: {exc}")

    print("\n二、库层联动")
    for fn in (t19_cache_key_follows_model, t20_terms_variant_cascade):
        try:
            fn()
        except Exception as exc:
            record(fn.__name__, False, f"异常：{type(exc).__name__}: {exc}")

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "-" * 56)
    print(f"结果：{passed}/{total} 通过（{passed * 100 // max(total, 1)}%）")
    if passed != total:
        print("未通过：")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}（{detail}）")
    print("-" * 56)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
