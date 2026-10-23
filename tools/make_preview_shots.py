# -*- coding: utf-8 -*-
"""工具：生成 README 用的界面预览图（干净截图 → 压缩为 WebP）。
职责：用 headless Chrome 打开关键页面/交互状态，截原图到临时目录；
      再由 Pillow 压缩到 docs/images/（宽 ≤1600，WebP q88），供 README 展示。
用法：python tools/make_preview_shots.py
关键依赖：websocket-client、Pillow、本机 Chrome、服务 127.0.0.1:9000。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:9000"
OUT_DIR = os.path.join(ROOT, "docs", "images")
RAW_DIR = os.path.join(tempfile.gettempdir(), "hcn-preview-raw")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(RAW_DIR, exist_ok=True)

WAIT_READY = """(() => {
  var cols = !!document.querySelector('.colL');
  var leads = typeof window.__leads !== 'undefined';
  return cols && leads;
})()"""

ENTER_3COL = """(() => {
  var src = document.querySelector('.colR .panel');
  var dz = document.getElementById('drop3');
  var dt = new DataTransfer();
  (src.querySelector('.pt')||src).dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
  src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
  dz.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt}));
  dz.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));
  src.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
  return 1;
})()"""

COLLAPSE_DRAWER = "(() => {var b=document.getElementById('dtog'); if(b) b.click(); return 1;})()"
NIGHT_ON = "(() => {if(window.EARTH && window.EARTH.setNight) window.EARTH.setNight(1); return 1;})()"


def clip_of(selector, pad=8):
    return ("(() => {var e=document.querySelector(%r); if(!e) return null;"
            "var b=e.getBoundingClientRect();"
            "return {x:Math.max(0,Math.round(b.x-%d)), y:Math.max(0,Math.round(b.y-%d)),"
            "width:Math.round(b.width+%d), height:Math.round(b.height+%d)};})()"
            % (selector, pad, pad, pad * 2, pad * 2))


def capture(name, url, width, height, actions=(), clip=None, wait=2.0):
    port = 9400 + abs(hash(name)) % 300
    prof = tempfile.mkdtemp(prefix="prev-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
         "--use-angle=swiftshader", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={prof}",
         f"--window-size={width},{height}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(70):
            try:
                ts = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
                pg = [t for t in ts if t["type"] == "page" and url.split("/")[-1][:10] in t.get("url", "")]
                if not pg:
                    pg = [t for t in ts if t["type"] == "page"]
                if pg:
                    ws = websocket.create_connection(pg[0]["webSocketDebuggerUrl"], timeout=25,
                                                     suppress_origin=True)
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if ws is None:
            print("  [skip]", name, "CDP 失败")
            return None

        def ev(expr, mid=[0]):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid[0]:
                    r = m.get("result", {})
                    if r.get("exceptionDetails"):
                        return None
                    return r.get("result", {}).get("value")

        if "/static/" not in url:
            for _ in range(120):
                time.sleep(0.3)
                if ev(WAIT_READY):
                    break
        time.sleep(wait)
        for act in actions:
            ev(act)
            time.sleep(1.2)

        box = None
        if clip:
            box = ev(clip)
        params = {"format": "png"}
        if box:
            params["clip"] = dict(box, scale=1)
        ws.send(json.dumps({"id": 900, "method": "Page.captureScreenshot", "params": params}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 900:
                data = m.get("result", {}).get("data")
                if not data:
                    print("  [skip]", name, "截图为空")
                    return None
                raw = os.path.join(RAW_DIR, name + ".png")
                with open(raw, "wb") as f:
                    f.write(base64.b64decode(data))
                print("  [raw]", name, os.path.getsize(raw) // 1024, "KB")
                return raw
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.kill()


def compress(raws, max_w=1600, quality=88):
    total = 0
    for name, raw in raws:
        if not raw:
            continue
        img = Image.open(raw).convert("RGB")
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
        out = os.path.join(OUT_DIR, name + ".webp")
        img.save(out, "WEBP", quality=quality, method=6)
        size = os.path.getsize(out)
        total += size
        print(f"  [out] {name}.webp  {img.width}x{img.height}  {size // 1024} KB")
    print(f"合计：{total / 1048576:.2f} MB → {OUT_DIR}")


def main():
    shots = [
        ("dash-2col", BASE + "/", 1920, 1080, (), None, 2.5),
        ("dash-3col-wide", BASE + "/", 1920, 1080, (ENTER_3COL,), None, 2.0),
        ("dash-3col-2560", BASE + "/", 2560, 1440, (ENTER_3COL,), None, 2.0),
        ("dash-night", BASE + "/?night=1", 1920, 1080, (), None, 3.5),
        ("globe-focus", BASE + "/", 1920, 1080, (), clip_of("#g3d"), 2.5),
        ("panel-platform", BASE + "/", 1920, 1080, (), clip_of("#pPlat", 6), 2.5),
        ("sources-signal", BASE + "/", 1920, 1080, (), clip_of("#srcList", 10), 2.5),
        ("drawer-collapsed", BASE + "/", 1920, 1080, (COLLAPSE_DRAWER,), None, 1.5),
        ("settings", BASE + "/static/settings.html", 1400, 1100, (), None, 1.8),
        ("cost", BASE + "/static/cost.html", 1400, 1000, (), None, 1.8),
        ("notes", BASE + "/static/notes.html", 1400, 900, (), None, 1.8),
        ("detail", BASE + "/static/detail.html?id=27221", 1400, 1100, (), None, 2.0),
    ]
    raws = []
    for name, url, w, h, acts, clip, wait in shots:
        print("capture:", name)
        raws.append((name, capture(name, url, w, h, acts, clip, wait)))
    compress(raws)
    return 0


if __name__ == "__main__":
    sys.exit(main())
