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


# 必应降级时，某些关键词可能返回空 → 用同义变体依次尝试
QUERY_ALTS = {
    "跨境电商": ["跨境电商 出海", "跨境 电商", "跨境贸易"],
    "选品 爆款": ["选品", "爆款 选品", "热销选品"],
    "关税 出海": ["关税 出海", "关税 出口"],
    "TikTok Shop Shopee Temu": ["TikTok Shop Shopee Temu", "跨境电商平台 政策"],
    "海外仓 物流": ["海外仓 物流", "海外仓"],
}


class GoogleNewsSource(BaseSource):
    """Google News 关键词 RSS。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def _fetch_bing(self, headers: dict) -> list:
        """备用源：必应新闻 RSS（同一关键词）。"""
        import re
        from urllib.parse import quote
        import time as _time
        base = self.spec.channel or ""
        queries = QUERY_ALTS.get(base, [base])
        if base not in queries:
            queries = [base] + queries
        feed = None
        _btimeout = httpx.Timeout(self.timeout, connect=8.0)
        with httpx.Client(timeout=_btimeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            for q in queries[:2]:
                url = ("https://www.bing.com/news/search?q=" + quote(q)
                       + "&format=RSS&setlang=zh-cn")
                for attempt in range(2):
                    try:
                        resp = client.get(url, headers=headers)
                        break
                    except Exception:
                        resp = None
                        _time.sleep(1.0 + attempt)
                if resp is None or resp.status_code != 200:
                    continue
                f = feedparser.parse(resp.content)
                if f.entries:
                    feed = f
                    break
                _time.sleep(0.6)
        if feed is None:
            raise ValueError("必应新闻 RSS 无条目（已尝试多个关键词变体）")
        out = []
        for ent in feed.entries[:40]:
            title = (ent.get("title") or "").strip()
            link = (ent.get("link") or "").strip()
            if not title or not link:
                continue
            summary = re.sub(r"<[^>]+>", "", ent.get("summary") or "").strip()
            published = ""
            ts = ent.get("published_parsed") or ent.get("updated_parsed")
            if ts:
                try:
                    published = datetime.fromtimestamp(mktime(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OverflowError):
                    published = ""
            out.append(RawItem(title=title, url=link, summary=summary,
                               published_at=published, source_id=self.spec.id,
                               extra={"via": "bing"}))
        if not out:
            raise ValueError("必应新闻 RSS 无条目")
        return out

    def fetch(self) -> list:
        import random
        import time as _time0
        _time0.sleep(random.uniform(0, 1.5))     # 错峰：5 个源不要同时打同一站点
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        }
        import time as _time
        raw = None
        last_err = None
        # connect 单独短超时：坏网络下最多 8s/次，避免把整轮采集拖到 90s+
        _timeout = httpx.Timeout(self.timeout, connect=8.0)
        with httpx.Client(timeout=_timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            for attempt in range(2):
                try:
                    resp = client.get(self.spec.url, headers=headers)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {resp.status_code}（Google 限流/上游错误）"
                        _time.sleep(2.0)
                        continue
                    resp.raise_for_status()
                    raw = resp.content
                    break
                except httpx.HTTPError as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    _time.sleep(1.5)
        if raw is None:
            # Google 整体 503（IP 级限流）时，自动降级到必应新闻 RSS
            try:
                return self._fetch_bing(headers)
            except Exception as exc:
                raise ValueError(
                    f"Google News 重试 3 次仍失败（{last_err}），必应备用源也失败（{exc}）")

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
