# -*- coding: utf-8 -*-
"""模块：抖音热点榜源（T12 二期 · 需求侧信号）。
职责：请求抖音公开热榜接口（需 UA + Referer），取前 N 条话题作为「大众注意力」信号。
输入输出：继承 BaseSource，fetch() 返回 list[RawItem]。
关键依赖：httpx。说明：热点是话题级信号，是否具备经济价值交给粗筛判断。
"""
from datetime import datetime
from urllib.parse import quote

import httpx

from .base import BaseSource, RawItem

API = "https://www.douyin.com/aweme/v1/web/hot/search/list/"
MAX_ITEMS = 30


class DouyinHotSource(BaseSource):
    """抖音热点榜。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def fetch(self) -> list:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
            "Accept": "application/json,text/plain,*/*",
        }
        with httpx.Client(timeout=self.timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            resp = client.get(API, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        words = ((payload.get("data") or {}).get("word_list") or [])
        if not words:
            raise ValueError("抖音热榜返回空（接口可能改版或需要登录）")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for row in words:
            word = (row.get("word") or "").strip()
            if not word:
                continue
            hot = row.get("hot_value") or 0
            pos = row.get("position") or len(out) + 1
            out.append(RawItem(
                title=word,
                url="https://www.douyin.com/search/" + quote(word),
                summary=f"抖音热榜第 {pos} 位，热度 {hot}",
                published_at=now,
                source_id=self.spec.id,
                extra={"heat": hot, "rank": pos, "platform": "douyin"},
            ))
            if len(out) >= MAX_ITEMS:
                break
        return out
