# -*- coding: utf-8 -*-
"""模块：新浪财经 7×24 直连源。职责：直连 zhibo.sina.com.cn 的 7x24 直播接口，
    解析 result.data.feed.list[]（rich_text / create_time / docurl）为 RawItem。
    国内源，可走直连；超时 20 秒。关键依赖：httpx、app.sources.base。"""
from datetime import datetime

import httpx

from .base import BaseSource, RawItem

SINA_API = "https://zhibo.sina.com.cn/api/zhibo/feed"


class SinaSource(BaseSource):
    """新浪财经 7×24 快讯。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def fetch(self) -> list:
        params = {"page": 1, "page_size": 50, "zhibo_id": 152,
                  "tag_id": 0, "dire": "f", "dpc": 1}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://finance.sina.com.cn/7x24/",
        }
        with httpx.Client(timeout=self.timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            resp = client.get(SINA_API, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        rows = (((payload.get("result") or {}).get("data") or {})
                .get("feed") or {}).get("list") or []
        out = []
        for it in rows:
            text = (it.get("rich_text") or "").strip()
            if not text:
                continue
            title = text[:120]
            out.append(RawItem(
                title=title,
                url=it.get("docurl") or "https://finance.sina.com.cn/7x24/",
                summary=text,
                published_at=it.get("create_time") or "",
                source_id=self.spec.id,
            ))
        return out
