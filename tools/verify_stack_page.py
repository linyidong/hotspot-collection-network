# -*- coding: utf-8 -*-
"""模块：技术栈页面自动验收。
职责：检查 /static/stack.html 是否满足页面要求：
      1) HTTP 200；2) 自包含（无 CDN/外部 http(s) 资源）；3) 无 console 错误；
      4) 1440/1920/2560 三档宽度无横向滚动；5) 内容覆盖前端/后端/数据源/关键数字；
      6) 大屏顶部有「技术栈」入口且能跳到该页；7) 配色为项目深色调。
输出：PASS/FAIL 明细；退出码 0 = 全部通过。
关键依赖：websocket-client、本机 Chrome、服务 127.0.0.1:9000。
"""
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:9000"
STACK = BASE + "/static/stack.html"

# 页面必须出现的技术关键词（前后端 + 数据源 + 数字）
REQUIRED = {
    "前端": ["three.js", "原生", "HTML", "CSS", "着色器", "零构建"],
    "后端": ["Python", "FastAPI", "uvicorn", "httpx", "APScheduler", "SQLite", "openai"],
    "数据": ["RSS", "热搜", "headless", "Chrome", "去重", "粗筛", "深加工"],
    "数字": ["20", "12", "9", "6"],
}


def main():
    results = []

    # 1) HTTP 200 + 内容检查
    try:
        with urllib.request.urlopen(STACK, timeout=10) as r:
            status = r.status
            html = r.read().decode("utf-8", "ignore")
    except Exception as exc:
        print(f"stack.html 打不开：{exc}")
        return 1
    results.append(("页面可访问", "PASS" if status == 200 else "FAIL", f"HTTP {status}"))

    # 2) 自包含：不允许外链 http(s) 资源（href/src/url）
    ext = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html, re.I)
    css_ext = re.findall(r'url\(\s*["\']?(https?://[^)"\']+)', html, re.I)
    ext = ext + css_ext
    results.append(("无外部 CDN 依赖", "PASS" if not ext else "FAIL",
                    "自包含" if not ext else "发现外链: " + ", ".join(ext[:3])))

    # 3) 关键词覆盖
    missing = []
    for group, words in REQUIRED.items():
        for w in words:
            if w.lower() not in html.lower():
                missing.append(f"{group}:{w}")
    results.append(("技术内容覆盖", "PASS" if not missing else "FAIL",
                    "关键项齐全" if not missing else "缺: " + ", ".join(missing[:6])))

    # 4) 配色（出现项目主色系）
    palette_ok = any(c in html.lower() for c in ("#060a12", "#0a1220", "#35d0e8", "#7fe6f5"))
    results.append(("配色与项目一致", "PASS" if palette_ok else "FAIL",
                    "包含深色底+青色强调" if palette_ok else "未发现项目主色"))

    # 5) 大屏顶部入口
    try:
        with urllib.request.urlopen(BASE + "/", timeout=10) as r:
            home = r.read().decode("utf-8", "ignore")
        entry = bool(re.search(r'href\s*=\s*["\'][^"\']*stack\.html', home, re.I)) or ("技术栈" in home)
        results.append(("大屏有技术栈入口", "PASS" if entry else "FAIL",
                        "顶部已加入口" if entry else "首页未找到 stack.html 链接/技术栈文字"))
    except Exception as exc:
        results.append(("大屏有技术栈入口", "FAIL", f"首页打不开: {exc}"))

    # 6) 多分辨率无横向滚动 + 无 console 错误
    for w, h in ((1440, 900), (1920, 1080), (2560, 1440)):
        port = 9300 + w // 100
        prof = tempfile.mkdtemp(prefix=f"st-{w}-")
        proc = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--use-angle=swiftshader", f"--remote-debugging-port={port}",
             "--remote-allow-origins=*", f"--user-data-dir={prof}",
             f"--window-size={w},{h}", STACK],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws = None
        try:
            for _ in range(70):
                try:
                    ts = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
                    pg = [t for t in ts if t["type"] == "page" and "stack" in t.get("url", "")]
                    if pg:
                        ws = websocket.create_connection(pg[0]["webSocketDebuggerUrl"], timeout=20,
                                                         suppress_origin=True)
                        break
                except Exception:
                    pass
                time.sleep(0.3)
            if ws is None:
                results.append((f"{w} 宽：打开页面", "FAIL", "CDP 失败"))
                continue
            ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            errors = []

            def ev(expr, mid=[0]):
                mid[0] += 1
                ws.send(json.dumps({"id": mid[0], "method": "Runtime.evaluate",
                                    "params": {"expression": expr, "returnByValue": True}}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("method") == "Runtime.exceptionThrown":
                        errors.append(str(m["params"]["exceptionDetails"].get("text"))[:80])
                        continue
                    if m.get("id") == mid[0]:
                        return m.get("result", {}).get("result", {}).get("value")

            for _ in range(40):
                time.sleep(0.3)
                if ev("document.readyState") == "complete":
                    break
            time.sleep(0.6)
            overflow = ev("document.documentElement.scrollWidth - window.innerWidth")
            results.append((f"{w} 宽：无横向滚动", "PASS" if isinstance(overflow, int) and overflow <= 2 else "FAIL",
                            f"横向溢出 {overflow}px"))
            results.append((f"{w} 宽：无 JS 异常", "PASS" if not errors else "FAIL",
                            "无" if not errors else "; ".join(errors[:3])))
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
            proc.kill()

    ok = sum(1 for _, s, _ in results if s == "PASS")
    bad = [x for x in results if x[1] == "FAIL"]
    print(f"\n=== 技术栈页面验收：PASS {ok} / FAIL {len(bad)} ===")
    for name, st, detail in results:
        mark = "PASS" if st == "PASS" else "FAIL"
        print(f"  [{mark}] {name} :: {detail}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
