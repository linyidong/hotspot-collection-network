# -*- coding: utf-8 -*-
"""模块：华尔街见闻直连源。职责：直连 api-one.wallstcn.com 抓取 3 个频道
    （global-channel / a-stock-channel / forex-channel），解析 data.items[] 为 RawItem。
    超时 20 秒（任务书要求）。关键依赖：httpx、app.sources.base、app.sources.registry。"""
from datetime import datetime

import httpx

from .base import BaseSource, RawItem
from .registry import WSCN_API_BASE


class WallStcnSource(BaseSource):
    """直连华尔街见闻的单个频道。spec.path 即频道名。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def fetch(self) -> list:
        params = {"channel": self.spec.channel, "client": "pc", "limit": 50}
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://wallstreetcn.com/live",
        }
        with httpx.Client(timeout=self.timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            resp = client.get(WSCN_API_BASE, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        items = (payload.get("data") or {}).get("items") or []
        out = []
        for it in items:
            content = it.get("content_text") or it.get("content") or ""
            title = (it.get("title") or content)[:200]
            if not title:
                continue
            uri = it.get("uri") or ""
            url = uri if uri.startswith("http") else "https://wallstreetcn.com" + uri
            pub_ts = it.get("display_time") or 0
            out.append(RawItem(
                title=title,
                url=url,
                summary=content,
                published_at=_fmt_ts(pub_ts),
                source_id=self.spec.id,
            ))
        return out


def _fmt_ts(ts) -> str:
    """Unix 秒 → 本地时间 'YYYY-MM-DD HH:MM:SS'；无效返回空串。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return ""
