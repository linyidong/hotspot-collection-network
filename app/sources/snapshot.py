# -*- coding: utf-8 -*-
"""模块：平台榜单快照源（T12 · C 方案）。
职责：从 data/platform_snapshot.json 读取「Amazon Best Sellers」等榜单条目，
      按 spec.channel 指定的页面取数据，转成 RawItem。
      快照由 app.platform.refresh() 每天刷新一次（不在 3 小时轮次里跑浏览器）。
关键依赖：app.platform.load。
"""
from .base import BaseSource, RawItem


class SnapshotSource(BaseSource):
    """读取本地榜单快照的源。"""

    def __init__(self, spec, proxy_url: str = None, timeout: float = 20.0):
        super().__init__(spec, timeout=timeout)

    def fetch(self) -> list:
        from ..platform import load           # 延迟导入，避免循环依赖

        snap = load()
        key = self.spec.channel or self.spec.path
        page = (snap.get("pages") or {}).get(key)
        if not page:
            err = (snap.get("errors") or {}).get(key)
            raise RuntimeError(
                "平台榜单快照缺失或该页抓取失败"
                + (f"（{err}）" if err else "")
                + "；先执行：python run.py --platform-snapshot")
        ts = snap.get("ts") or ""
        label = page.get("label") or key
        out = []
        for it in page.get("items") or []:
            title = (it.get("title") or "").strip()
            url = (it.get("url") or "").strip()
            if not title or not url:
                continue
            out.append(RawItem(
                title=title, url=url,
                summary=f"{label} 第 {it.get('rank', '?')} 名（榜单快照 {ts}）",
                published_at=ts, source_id=self.spec.id,
            ))
        return out
