# -*- coding: utf-8 -*-
"""模块：平台榜单快照（T12 · C 方案）。
职责：用本机 headless Chrome 打开 Amazon Best Sellers 等榜单页，抽取商品名与链接，
      写入 data/platform_snapshot.json；采集层按快照生成条目（日更即可，不跟 3 小时轮次）。
      Chrome 打开失败 / 页面改版 → 记 last_error，绝不影响其它源。
关键依赖：subprocess（调用本机 chrome.exe）、json、html.parser。
"""
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from .config import ROOT

SNAPSHOT_PATH = ROOT / "data" / "platform_snapshot.json"
# 要抓的平台榜单页（key 必须与 registry 里 snapshot 源的 channel 一致）
PAGES = {
    "amazon-kitchen": ("Amazon·厨房家居榜", "https://www.amazon.com/gp/bestsellers/kitchen/"),
    "amazon-electronics": ("Amazon·电子配件榜", "https://www.amazon.com/gp/bestsellers/electronics/"),
    "amazon-beauty": ("Amazon·美妆个护榜", "https://www.amazon.com/gp/bestsellers/beauty/"),
}

CHROME_CANDIDATES = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def _chrome_path() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    raise FileNotFoundError("找不到 chrome.exe（平台榜单需要本机 Chrome）")


class _LinkText(HTMLParser):
    """收集 <a href> 内含 /dp/ 或 /item/ 的链接及其文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cur = None
        self.depth = 0
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            if self.cur is not None:
                self.depth += 1
            return
        href = dict(attrs).get("href") or ""
        if "/dp/" in href or "/item/" in href:
            self.cur = {"href": href, "text": []}
            self.depth = 0
        elif self.cur is not None:
            self.depth += 1

    def handle_data(self, data):
        if self.cur is not None:
            t = data.strip()
            if t:
                self.cur["text"].append(t)

    def handle_endtag(self, tag):
        if tag == "a" and self.cur is not None:
            text = re.sub(r"\s+", " ", " ".join(self.cur["text"])).strip()
            if len(text) >= 6:
                self.items.append((text, self.cur["href"]))
            self.cur = None
            self.depth = 0


def _fetch_dom(url: str, wait_ms: int = 12000) -> str:
    """用 headless Chrome 抓渲染后的 DOM。"""
    chrome = _chrome_path()
    prof = tempfile.mkdtemp(prefix="plat-")
    cmd = [chrome, "--headless", "--disable-gpu", "--no-first-run",
           f"--virtual-time-budget={wait_ms}", f"--user-data-dir={prof}",
           "--dump-dom", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=90)
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    dom = (out.stdout or b"").decode("utf-8", "ignore")
    if len(dom) < 2000:
        raise RuntimeError(f"页面内容为空（可能是反爬/改版）：{url}")
    return dom


def refresh(pages: dict, per_page: int = 20) -> dict:
    """刷新榜单快照。pages: {page_key: (label, url)}；返回快照 dict。"""
    snap = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pages": {}, "errors": {}}
    for key, (label, url) in pages.items():
        try:
            dom = _fetch_dom(url)
            parser = _LinkText()
            parser.feed(dom)
            seen, items = set(), []
            for text, href in parser.items:
                if href.startswith("/"):
                    href = "https://www.amazon.com" + href
                if href in seen:
                    continue
                seen.add(href)
                items.append({"title": html.unescape(text)[:120], "url": href,
                              "rank": len(items) + 1})
                if len(items) >= per_page:
                    break
            if not items:
                raise RuntimeError("未解析到商品（页面可能改版）")
            snap["pages"][key] = {"label": label, "url": url, "items": items}
        except Exception as exc:                 # 单页失败不影响其它页
            snap["errors"][key] = f"{type(exc).__name__}: {exc}"
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return snap


def load() -> dict:
    """读取快照；不存在返回空结构。"""
    if not SNAPSHOT_PATH.exists():
        return {"ts": None, "pages": {}, "errors": {}}
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"ts": None, "pages": {}, "errors": {}}


def is_fresh(max_hours: int = 24) -> bool:
    """快照是否在 max_hours 内。"""
    snap = load()
    ts = snap.get("ts")
    if not ts:
        return False
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() < max_hours * 3600
