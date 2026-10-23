# -*- coding: utf-8 -*-
"""模块：平台榜单快照（T12 · C 方案）。
职责：抓取 Amazon Best Sellers 等榜单页，抽取商品名与链接，
      写入 data/platform_snapshot.json；采集层按快照生成条目（日更即可，不跟 3 小时轮次）。
      抓取优先 HTTP 直连 + HTML 解析（httpx，实测 1.6–3.8s/页），失败才用 headless Chrome 兜底；
      单页彻底失败 → 记 last_error 并保留旧数据（stale），绝不影响其它源。
关键依赖：httpx（直连解析）、subprocess（调用本机 Chrome 兜底，Windows/macOS/Linux 路径均已适配）、
          json、html.parser、re。
"""
import html
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

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
    raise FileNotFoundError("找不到 Chrome（平台榜单需要本机 Chrome：Windows 装 Chrome，macOS 放 /Applications，Linux 装 google-chrome）")


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


def fetch_http(url: str, per_page: int = 20) -> list:
    """HTTP 直连 + HTML 解析（httpx）。返回 [{title, url, rank}]；条目 <10 视为失败。

    解析规则（DSH 实测）：`id="gridItemRoot"` 是每条商品块；标题在
    `<div class="_cDEzb_p13n-sc-css-line-clamp...">TITLE</div>`；
    链接形如 `href="/xxx/dp/ASIN"`，需补 `https://www.amazon.com` 前缀；排名按页面顺序 1..N。
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = httpx.get(url, headers=headers,
                     timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=True)
    resp.raise_for_status()
    dom = resp.text
    # 按商品块切分：id="gridItemRoot" 开头的 div
    blocks = re.split(r'<div[^>]*id="gridItemRoot"[^>]*>', dom)[1:]
    items, seen = [], set()
    for blk in blocks:
        m_title = re.search(r'class="(_cDEzb_p13n-sc-css-line-clamp[^"]*)"[^>]*>(.*?)</div>',
                            blk, re.S)
        # 真实链接形如 /商品名/dp/B0BZYCJK89/ref=zg_bs_...?psc=1
        # 只取 /xxx/dp/ASIN 这段（ASIN 9~12 位），去掉 ref 跟踪参数，保证 URL 稳定可点
        m_url = re.search(r'href="(/[^"]*?/dp/([A-Z0-9]{9,12}))', blk)
        if not m_title or not m_url:
            continue
        title = re.sub(r"<[^>]+>", "", m_title.group(2)).strip()
        title = html.unescape(title)
        href = "https://www.amazon.com" + m_url.group(1)
        if not title or len(title) < 6 or href in seen:
            continue
        seen.add(href)
        items.append({"title": title[:120], "url": href, "rank": len(items) + 1})
        if len(items) >= per_page:
            break
    if len(items) < 10:
        raise ValueError(f"HTTP 直连解析不足 10 条（仅 {len(items)} 条），可能被反爬或页面改版：{url}")
    return items


def _fetch_dom(url: str, wait_ms: int = 12000) -> str:
    """用 headless Chrome 抓渲染后的 DOM。"""
    chrome = _chrome_path()
    prof = tempfile.mkdtemp(prefix="plat-")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
           f"--virtual-time-budget={wait_ms}", f"--user-data-dir={prof}",
           "--dump-dom", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=90)
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    dom = (out.stdout or b"").decode("utf-8", "ignore")
    if len(dom) < 2000:          # 失败：换一组更"像浏览器"的参数再试一次
        cmd2 = [c.replace("--headless=new", "--headless")
                for c in cmd if c != "--no-first-run"]
        cmd2.insert(1, "--lang=zh-CN")
        cmd2.insert(2, "--disable-blink-features=AutomationControlled")
        cmd2.insert(3, "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        try:
            out2 = subprocess.run(cmd2, capture_output=True, timeout=120)
            dom = (out2.stdout or b"").decode("utf-8", "ignore")
        except subprocess.SubprocessError:
            pass
    if len(dom) < 2000:
        raise RuntimeError(f"页面内容为空（可能是反爬/改版）：{url}")
    return dom


def refresh(pages: dict, per_page: int = 20) -> dict:
    """刷新榜单快照。pages: {page_key: (label, url)}；返回快照 dict。

    每页优先 HTTP 直连解析（fetch_http），失败再用 headless Chrome（_fetch_dom）兜底，
    都失败则保留「上一份快照」里的数据（标记 stale），避免一次反爬就把全部榜单清空。
    每页记录抓取方式 via（http/chrome/stale）与抓取时间 fetched_at；页间 sleep 1–2s 有礼貌。
    """
    prev = load()
    prev_pages = prev.get("pages") or {}
    snap = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pages": {}, "errors": {}}
    for idx, (key, (label, url)) in enumerate(pages.items()):
        try:
            items = fetch_http(url, per_page=per_page)
            via = "http"
        except Exception:
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
                via = "chrome"
            except Exception as exc:             # 单页失败：保留旧数据，只记错误
                snap["errors"][key] = f"{type(exc).__name__}: {exc}"
                old_page = prev_pages.get(key)
                if old_page and old_page.get("items"):
                    snap["pages"][key] = dict(
                        old_page, stale=True, via="stale",
                        fetched_at=prev.get("ts") or "")
                if idx < len(pages) - 1:
                    time.sleep(random.uniform(1, 2))
                continue
        snap["pages"][key] = {
            "label": label, "url": url, "items": items,
            "via": via,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if idx < len(pages) - 1:
            time.sleep(random.uniform(1, 2))
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
