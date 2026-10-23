# -*- coding: utf-8 -*-
"""工具：直接测试 Amazon 榜单 HTTP 直连解析（不走 Chrome）。
用法：python tools/test_amazon_http.py
"""
import sys
import time

sys.path.insert(0, r"D:\Deepseek Harness\Project\Hotspot Collection Network")
sys.stdout.reconfigure(encoding="utf-8")

from app import platform  # noqa: E402

for key, (label, url) in platform.PAGES.items():
    t = time.time()
    try:
        items = platform.fetch_http(url, per_page=20)
        print(f"{key}: {len(items)} 条 {round(time.time() - t, 1)}s")
        print(f"   例: {items[0]['title'][:50]}")
        print(f"   URL: {items[0]['url']}")
    except Exception as exc:
        print(f"{key}: 失败 {type(exc).__name__}: {str(exc)[:90]}")
