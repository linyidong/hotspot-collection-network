# -*- coding: utf-8 -*-
"""模块：微博热搜直连源（T12 平台/需求侧信号，A 方案）。
职责：请求 weibo.com 的热搜 JSON 接口（需 Referer），取前 N 条话题作为"需求侧信号"。
      spec.channel 可放条数上限（默认 50）。
输入输出：继承 BaseSource，fetch() 返回 list[RawItem]。
关键依赖：httpx。
说明：热搜是"话题热度"，不是新闻正文；粗筛会判断其是否具备经济/商品价值。
"""
from datetime import datetime
from urllib.parse import quote

import httpx

from .base import BaseSource, RawItem

API = "https://weibo.com/ajax/side/hotSearch"


class WeiboHotSource(BaseSource):
    """微博热搜榜。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def fetch(self) -> list:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://weibo.com/",
            "Accept": "application/json,text/plain,*/*",
        }
        with httpx.Client(timeout=self.timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            resp = client.get(API, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        if payload.get("ok") != 1:
            raise ValueError(f"微博接口返回异常：ok={payload.get('ok')}")

        rows = ((payload.get("data") or {}).get("realtime") or [])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for row in rows:
            word = (row.get("word") or "").strip()
            if not word:
                continue
            num = row.get("num") or 0
            out.append(RawItem(
                title=word,
                url="https://s.weibo.com/weibo?q=" + quote("#" + word + "#"),
                summary=f"微博热搜第 {row.get('rank', '?')} 位，热度 {num}",
                published_at=now,
                source_id=self.spec.id,
                extra={"heat": num, "rank": row.get("rank")},
            ))
            if len(out) >= 50:
                break
        return out
