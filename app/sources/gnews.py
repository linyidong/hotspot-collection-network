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


# Google 不可达时短路（秒级时间戳）：避免每个源都白等 8~16 秒连接超时
_GOOGLE_DOWN_UNTIL = 0.0


class GoogleNewsSource(BaseSource):
    """Google News 关键词 RSS。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)
        self.proxy_url = proxy_url

    def _fetch_360(self, headers: dict) -> list:
        """备用源 1：360 新闻搜索（HTML）。Google 不可达时国内仍可用，返回真实链接。"""
        import re
        from html import unescape
        from urllib.parse import quote
        from datetime import timedelta
        import time as _time
        base = self.spec.channel or ""
        queries = QUERY_ALTS.get(base, [base])
        if base not in queries:
            queries = [base] + queries
        _timeout = httpx.Timeout(self.timeout, connect=8.0)
        hdrs = dict(headers)
        hdrs["Accept"] = "text/html,application/xhtml+xml"
        last_err = None
        with httpx.Client(timeout=_timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            for q in queries[:2]:
                url = "https://news.so.com/ns?q=" + quote(q) + "&pn=1"
                try:
                    resp = client.get(url, headers=hdrs)
                    resp.raise_for_status()
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    _time.sleep(1.0)
                    continue
                html = resp.text
                out = []
                for m in re.finditer(r'<li[^>]*class="[^"]*res-list[^"]*"[^>]*>(.*?)</li>',
                                     html, re.S):
                    block = m.group(1)
                    href = re.search(r'<a[^>]*href="(https?://[^"]+)"', block)
                    t = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
                    if not href or not t:
                        continue
                    title = unescape(re.sub(r"<[^>]+>|<!--.*?-->", "", t.group(1),
                                            flags=re.S))
                    title = re.sub(r"\s+", " ", title).strip()
                    if not title or len(title) < 6:
                        continue
                    sm = re.search(r'<p[^>]*class="[^"]*summary[^"]*"[^>]*>(.*?)</p>',
                                   block, re.S)
                    summary = ""
                    if sm:
                        summary = unescape(re.sub(r"<[^>]+>", "", sm.group(1),
                                                  flags=re.S)).strip()
                    published = ""
                    tm = re.search(r'class="[^"]*time[^"]*"[^>]*>\s*([^<]{1,20})\s*<',
                                   block)
                    if tm:
                        raw_t = tm.group(1).strip()
                        now = datetime.now()
                        hm = re.match(r"(\d+)\s*小时前", raw_t)
                        mm = re.match(r"(\d+)\s*分钟前", raw_t)
                        dm = re.match(r"(\d+)\s*天前", raw_t)
                        try:
                            if hm:
                                published = (now - timedelta(hours=int(hm.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
                            elif mm:
                                published = (now - timedelta(minutes=int(mm.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
                            elif dm:
                                published = (now - timedelta(days=int(dm.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, OverflowError):
                            published = ""
                    out.append(RawItem(title=title, url=href.group(1), summary=summary,
                                       published_at=published, source_id=self.spec.id,
                                       extra={"via": "360"}))
                if len(out) >= 3:
                    return out[:40]
                _time.sleep(1.0)
        raise ValueError(f"360 新闻无结果（{last_err or '解析不到条目'}）")

    def _fetch_sogou(self, headers: dict) -> list:
        """备用源 1：搜狗新闻搜索结果（HTML）。Google 不可达时国内仍可用。"""
        import re
        from html import unescape
        from urllib.parse import quote
        import time as _time
        base = self.spec.channel or ""
        queries = QUERY_ALTS.get(base, [base])
        if base not in queries:
            queries = [base] + queries
        _timeout = httpx.Timeout(self.timeout, connect=5.0)
        hdrs = dict(headers)
        hdrs["Accept"] = "text/html,application/xhtml+xml"
        last_err = None
        with httpx.Client(timeout=_timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            for q in queries[:2]:
                url = ("https://news.sogou.com/news?query=" + quote(q)
                       + "&mode=1&sort=0&page=1")
                try:
                    resp = client.get(url, headers=hdrs)
                    resp.raise_for_status()
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    _time.sleep(1.0)
                    continue
                html = resp.text
                blocks = re.findall(
                    r'<h3[^>]*class="vr-title"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    html, re.S)
                out = []
                for href, raw_title in blocks:
                    title = unescape(re.sub(r"<[^>]+>|<!--.*?-->", "", raw_title,
                                            flags=re.S)).strip()
                    title = re.sub(r"\s+", " ", title)
                    if not title or len(title) < 6:
                        continue
                    link = href
                    if link.startswith("/"):
                        link = "https://news.sogou.com" + link
                    if not link.startswith("http"):
                        continue
                    out.append(RawItem(title=title, url=link, summary="",
                                       published_at="", source_id=self.spec.id,
                                       extra={"via": "sogou"}))
                if len(out) >= 3:
                    return out[:40]
                _time.sleep(1.0)
        raise ValueError(f"搜狗新闻无结果（{last_err or '解析不到条目'}）")

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
        import time as _tnow
        global _GOOGLE_DOWN_UNTIL
        skip_google = _tnow.time() < _GOOGLE_DOWN_UNTIL
        raw = None
        last_err = "Google 已被判定不可达（10 分钟内直连备用源）" if skip_google else None
        # connect 单独短超时：坏网络下最多 8s/次，避免把整轮采集拖到 90s+
        _timeout = httpx.Timeout(self.timeout, connect=5.0)
        with httpx.Client(timeout=_timeout, proxy=self.proxy_url,
                          follow_redirects=True) as client:
            for attempt in range(0 if skip_google else 2):
                try:
                    resp = client.get(self.spec.url, headers=headers)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {resp.status_code}（Google 限流/上游错误）"
                        _time.sleep(2.0)
                        continue
                    resp.raise_for_status()
                    raw = resp.content
                    break
                except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    _GOOGLE_DOWN_UNTIL = _tnow.time() + 600   # 10 分钟内不再试 Google
                    break
                except httpx.HTTPError as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    _time.sleep(1.0)
        if raw is None:
            # Google 不可达/限流时，自动降级：先搜狗新闻（国内可用），再必应 RSS
            try:
                return self._fetch_360(headers)
            except Exception as exc1:
                try:
                    return self._fetch_bing(headers)
                except Exception as exc2:
                    raise ValueError(
                        f"Google 不可用（{last_err}），360 失败（{exc1}），"
                        f"必应失败（{exc2}）")

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
