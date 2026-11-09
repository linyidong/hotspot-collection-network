# -*- coding: utf-8 -*-
"""模块：本地 RSS 桥源。职责：从本机 china-finance-rss 桥（127.0.0.1:8053）
    拉取国内源（财联社/东财/同花顺/金十/华尔街见闻RSS）的 RSS 并解析为 RawItem。
    超时 20 秒，桥不可达抛中文异常。配置 proxy_url 时在请求 URL 追加 ?proxy=，
    由桥按本源走 http/https 代理抓取；直连时 URL 不变。
    关键依赖：httpx、feedparser、app.sources.base。"""
from datetime import datetime
from urllib.parse import urlencode

import feedparser
import httpx

from .base import BaseSource, RawItem
from .registry import BRIDGE_BASE


class RssBridgeSource(BaseSource):
    """走本地 RSS 桥的单个源。spec.path 是桥上的路由路径。"""

    def __init__(self, spec, bridge_port: int = 8053, timeout: float = 20.0,
                 proxy_url: str = None):
        super().__init__(spec, timeout=timeout)
        self.base = BRIDGE_BASE.format(port=bridge_port)
        self.proxy_url = (proxy_url or "").strip() or None

    def fetch(self) -> list:
        url = self.base + self.spec.path
        if self.proxy_url:
            url = f"{url}?{urlencode({'proxy': self.proxy_url})}"
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
                resp = client.get(url)
            except httpx.HTTPError as e:
                raise RuntimeError(f"本地RSS桥连接失败（{self.base}）：{e}") from e
            if resp.status_code != 200:
                detail = " ".join((resp.text or "").split())[:240]
                raise RuntimeError(
                    f"本地RSS桥返回 HTTP {resp.status_code}（{self.spec.name}）："
                    f"{detail or '上游源当前不可达或响应异常'}")
            xml = resp.text

        feed = feedparser.parse(xml)
        if feed.get("bozo") and not feed.entries:
            exc = feed.get("bozo_exception")
            raise RuntimeError(f"RSS 解析失败：{exc}")

        # 桥在上游失败时会返回 HTTP 200 的「错误 RSS」（guid 以 error_ 开头，
        # 标题带 temporarily unavailable）。绝不能把它当成一条正常新闻，
        # 否则页面会出现「某某 temporarily unavailable」的假条目（静默失败）。
        err_entries = [
            e for e in feed.entries
            if str(e.get("id") or "").startswith("error_")
            or "temporarily unavailable" in (e.get("title") or "").lower()
        ]
        if err_entries:
            desc = (err_entries[0].get("description") or "").strip()
            raise RuntimeError(
                f"桥接源上游失败（{self.spec.name}）："
                f"{desc or '上游暂时不可用，请稍后重试'}")

        out = []
        for entry in feed.entries[:100]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            link = entry.get("link") or ""
            summary = (entry.get("description") or "").strip()
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            out.append(RawItem(
                title=title,
                url=link,
                summary=summary,
                published_at=_fmt_struct(pub),
                source_id=self.spec.id,
            ))
        return out


def _fmt_struct(struct) -> str:
    """feedparser 的 time.struct_time → 'YYYY-MM-DD HH:MM:SS'；缺失返回空串。"""
    if not struct:
        return ""
    try:
        return datetime(*struct[:6]).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
