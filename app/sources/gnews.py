# -*- coding: utf-8 -*-
"""模块：Google News RSS 直连源（T12 平台/需求侧信号，A 方案）。
职责：按关键词请求 Google News 的 RSS 搜索，解析为 RawItem。
      spec.channel 存关键词；spec.url 为完整 RSS 地址（便于人工核对）。
输入输出：继承 BaseSource，fetch() 返回 list[RawItem]。
关键依赖：httpx、feedparser。
"""
from datetime import datetime
from time import mktime

import feedparser
import httpx

from .base import BaseSource, RawItem


class GoogleNewsSource(BaseSource):
    """Google News 关键词 RSS。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def fetch(self) -> list:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        }
        with httpx.Client(timeout=self.timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            resp = client.get(self.spec.url, headers=headers)
            resp.raise_for_status()
            raw = resp.content

        feed = feedparser.parse(raw)
        if getattr(feed, "bozo", 0) and not getattr(feed, "entries", None):
            raise ValueError("RSS 解析失败（内容不是有效 feed）")

        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=30)  # 优先最近 30 天；全都很旧时保底取前 15 条
        out = []
        for ent in feed.entries[:60]:
            title = (ent.get("title") or "").strip()
            link = (ent.get("link") or "").strip()
            if not title or not link:
                continue
            summary = (ent.get("summary") or "").strip()
            # 去掉摘要里的 HTML 标签（Google News 摘要常带 <a>）
            if "<" in summary:
                import re
                summary = re.sub(r"<[^>]+>", "", summary)
            published = ""
            ts = ent.get("published_parsed") or ent.get("updated_parsed")
            if ts:
                try:
                    published = datetime.fromtimestamp(mktime(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OverflowError):
                    published = ""
            if published:
                try:
                    if datetime.strptime(published, "%Y-%m-%d %H:%M:%S") < cutoff:
                        continue
                except ValueError:
                    pass
            out.append(RawItem(
                title=title, url=link, summary=summary,
                published_at=published, source_id=self.spec.id,
            ))
            if len(out) >= 40:
                break
        # 保底：若时间过滤后为空，退回未过滤的前 15 条（避免该源整轮空转）
        if not out:
            for ent in feed.entries[:15]:
                title = (ent.get("title") or "").strip()
                link = (ent.get("link") or "").strip()
                if not title or not link:
                    continue
                published = ""
                ts = ent.get("published_parsed") or ent.get("updated_parsed")
                if ts:
                    try:
                        published = datetime.fromtimestamp(mktime(ts)).strftime("%Y-%m-%d %H:%M:%S")
                    except (ValueError, OverflowError):
                        published = ""
                out.append(RawItem(title=title, url=link, summary="", published_at=published,
                                   source_id=self.spec.id))
        return out
