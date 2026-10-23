# -*- coding: utf-8 -*-
"""工具：从数据库恢复平台榜单快照（Amazon 反爬导致刷新失败时的兜底）。
职责：读取最近一批里 Amazon 各榜的条目，重写 data/platform_snapshot.json，
      标记 stale=true 与来源批次，让 3 个榜单源恢复可采（数据为上一批的榜单）。
用法：python tools/rebuild_snapshot.py
关键依赖：sqlite3、app.platform 的快照路径。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.platform import SNAPSHOT_PATH  # noqa: E402

DB = os.path.join(ROOT, "data", "app.db")
# 源名 → 快照页 key / 展示名
PAGE_OF = {
    "Amazon·厨房家居榜": ("amazon-kitchen", "https://www.amazon.com/gp/bestsellers/kitchen/"),
    "Amazon·电子配件榜": ("amazon-electronics", "https://www.amazon.com/gp/bestsellers/electronics/"),
    "Amazon·美妆个护榜": ("amazon-beauty", "https://www.amazon.com/gp/bestsellers/beauty/"),
}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    batch = conn.execute(
        "SELECT MAX(i.batch_id) b FROM items i JOIN sources s ON s.id=i.source_id "
        "WHERE s.name LIKE 'Amazon%'").fetchone()["b"]
    if not batch:
        print("数据库里没有 Amazon 榜单条目，无法恢复")
        return 1
    pages, total = {}, 0
    for name, (key, url) in PAGE_OF.items():
        rows = conn.execute(
            "SELECT i.title, i.url FROM items i JOIN sources s ON s.id=i.source_id "
            "WHERE s.name=? AND i.batch_id=? ORDER BY i.id LIMIT 20", (name, batch)).fetchall()
        if not rows:
            continue
        items = []
        for idx, r in enumerate(rows, 1):
            title = (r["title"] or "").strip()
            link = (r["url"] or "").strip()
            if not title or not link:
                continue
            items.append({"title": title, "url": link, "rank": idx})
        if items:
            pages[key] = {"label": name, "url": url, "items": items,
                          "stale": True, "fetched_at": f"batch #{batch}"}
            total += len(items)
    conn.close()
    if not pages:
        print("没有可恢复的条目")
        return 1
    snap = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pages": pages,
            "errors": {}, "recovered_from": f"batch #{batch}"}
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已从批次 #{batch} 恢复 {len(pages)} 个榜单 / {total} 条 → {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
