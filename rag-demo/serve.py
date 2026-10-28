"""RAG 知识库演示页（零依赖，标准库 HTTP 服务）。

    python serve.py            # http://127.0.0.1:8765
    python serve.py --port 9000

两个接口：
    GET /api/search?q=...&k=6&kind=deep   -> 只检索（离线可用，毫秒级）
    GET /api/answer?q=...&k=6             -> 检索 + 调模型总结（需要 .env 里的 Key）
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ask

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        q = (qs.get("q") or [""])[0].strip()
        k = int((qs.get("k") or ["6"])[0] or 6)
        kind = (qs.get("kind") or [""])[0] or None

        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if u.path == "/api/search":
            if not q:
                return self._json(400, {"ok": False, "error": "缺少 q"})
            t0 = time.time()
            try:
                hits = ask.retrieve(q, k, kind)
            except SystemExit as e:
                return self._json(500, {"ok": False, "error": str(e)})
            return self._json(200, {"ok": True, "q": q, "hits": hits, "ms": round((time.time() - t0) * 1000)})

        if u.path == "/api/answer":
            if not q:
                return self._json(400, {"ok": False, "error": "缺少 q"})
            try:
                hits = ask.retrieve(q, k, kind)
                if not hits:
                    return self._json(200, {"ok": True, "answer": "没有命中资料，不调用模型。", "hits": []})
                ans = ask.call_llm(ask.build_prompt(q, hits), ask.load_env())
                return self._json(200, {"ok": True, "answer": ans, "hits": hits})
            except Exception as e:
                # 模型不可用时，检索结果仍然返回——这就是检索与生成分离的意义
                try:
                    hits = ask.retrieve(q, k, kind)
                except Exception:
                    hits = []
                return self._json(502, {"ok": False, "error": f"模型不可用：{e}", "hits": hits})

        return self._json(404, {"ok": False, "error": "未知路径"})

    def log_message(self, fmt, *args):
        print("[serve]", fmt % args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"知识库演示：http://{a.host}:{a.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
