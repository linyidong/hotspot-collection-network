# -*- coding: utf-8 -*-
"""模块：百度热搜源（T12 二期 · 需求侧信号）。
职责：请求百度热搜公开接口（platform=pc&tab=realtime），取热词 + 描述 + 热度。
输入输出：继承 BaseSource，fetch() 返回 list[RawItem]。
关键依赖：httpx。说明：带 desc 的条目信息量更高，粗筛更容易判断是否有商机。
"""
from datetime import datetime
from urllib.parse import quote

import httpx

from .base import BaseSource, RawItem

API = "https://top.baidu.com/api/board?platform=pc&tab=realtime"
MAX_ITEMS = 30


class BaiduHotSource(BaseSource):
    """百度热搜榜。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def fetch(self) -> list:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://top.baidu.com/board?tab=realtime",
            "Accept": "application/json,text/plain,*/*",
        }
        with httpx.Client(timeout=self.timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            resp = client.get(API, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        cards = ((payload.get("data") or {}).get("cards") or [])
        rows = []
        for card in cards:
            rows.extend(card.get("topContent") or [])
            rows.extend(card.get("content") or [])
        if not rows:
            raise ValueError("百度热搜返回空（接口可能改版）")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seen, out = set(), []
        for row in rows:
            word = (row.get("word") or "").strip()
            if not word or word in seen:
                continue
            seen.add(word)
            desc = (row.get("desc") or "").strip()
            hot = row.get("hotScore") or ""
            url = (row.get("rawUrl") or row.get("url") or "").strip()
            if not url:
                url = "https://www.baidu.com/s?wd=" + quote(word)
            summary = f"百度热搜第 {row.get('index', '?')} 位，热度 {hot}"
            if desc:
                summary += "；" + desc[:120]
            out.append(RawItem(
                title=word, url=url, summary=summary,
                published_at=now, source_id=self.spec.id,
                extra={"heat": hot, "rank": row.get("index"), "platform": "baidu"},
            ))
            if len(out) >= MAX_ITEMS:
                break
        return out
