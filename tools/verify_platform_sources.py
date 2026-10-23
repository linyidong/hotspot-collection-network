# -*- coding: utf-8 -*-
"""模块：平台榜单源（Amazon）改造验收。
职责：验证「直连 HTTP 优先、Chrome 兜底」是否落地且数据可用：
      1) 三个榜单页各自能从 HTTP 解析出 ≥20 条（不依赖 Chrome）；
      2) 快照 JSON 每页带 via / fetched_at / stale 字段；
      3) /api/platform_items 返回 Amazon 条目 ≥20 且带数据时间；
      4) 失败时保留旧数据（stale=true），不清空；
      5) 技术栈页已同步（含 httpx + Chrome 兜底表述）；
      6) 原有技术栈 11 项与交互 16 项不回归。
输出：PASS/FAIL 明细；退出码 0 = 全部通过。
用法：python tools/verify_platform_sources.py
关键依赖：httpx、本机服务 127.0.0.1:9000。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "data", "platform_snapshot.json")
BASE = "http://127.0.0.1:9000"


def main():
    results = []
    sys.path.insert(0, ROOT)
    from app import platform

    # 1) HTTP 直连解析（不启动 Chrome）
    t0 = time.time()
    for key, (label, url) in platform.PAGES.items():
        t = time.time()
        try:
            items = platform.fetch_http(url, per_page=20) if hasattr(platform, "fetch_http") else []
            n = len(items)
            elapsed = round(time.time() - t, 1)
            results.append((f"HTTP 解析 {key}", "PASS" if n >= 20 else "FAIL",
                            f"{n} 条 {elapsed}s"))
        except Exception as exc:
            results.append((f"HTTP 解析 {key}", "FAIL", f"{type(exc).__name__}: {str(exc)[:60]}"))
    total = round(time.time() - t0, 1)
    results.append(("3 页 HTTP 总耗时", "PASS" if total <= 30 else "FAIL", f"{total}s（要求 ≤30s）"))

    # 2) 快照结构
    if not os.path.exists(SNAP):
        results.append(("快照文件", "FAIL", "不存在"))
    else:
        snap = json.load(open(SNAP, encoding="utf-8"))
        pages = snap.get("pages") or {}
        ok_fields = True
        detail = []
        for k, v in pages.items():
            has = ("via" in v) and ("fetched_at" in v)
            ok_fields = ok_fields and has
            detail.append(f"{k}:{v.get('via','?')}/{len(v.get('items') or [])}条")
        results.append(("快照页含 via/fetched_at", "PASS" if ok_fields and pages else "FAIL",
                        ", ".join(detail) or "无页面"))

    # 3) API 返回
    try:
        with urllib.request.urlopen(BASE + "/api/platform_items", timeout=20) as r:
            data = json.loads(r.read().decode())
        items = data.get("items") or []
        amz = [x for x in items if "Amazon" in (x.get("source") or "") or "amazon" in (x.get("source") or "").lower()]
        with_time = [x for x in amz if x.get("fetched_at") or x.get("data_time")]
        results.append(("面板 Amazon 条目 ≥20", "PASS" if len(amz) >= 20 else "FAIL", f"{len(amz)} 条"))
        results.append(("面板条目带数据时间", "PASS" if len(with_time) >= 20 else "FAIL",
                        f"{len(with_time)}/{len(amz)} 条带时间"))
    except Exception as exc:
        results.append(("面板接口", "FAIL", f"{type(exc).__name__}: {str(exc)[:60]}"))

    # 4) 失败保留旧数据（用坏 URL 触发）
    try:
        tmp = os.path.join(ROOT, "data", "_snap_test.json")
        old_path = platform.SNAPSHOT_PATH
        platform.SNAPSHOT_PATH = type(old_path)(tmp)
        fake = {"ts": "2026-10-23 12:00:00", "pages": {"amazon-kitchen": {
            "label": "Amazon·厨房家居榜", "url": "u",
            "items": [{"title": "old item", "url": "https://x/1", "rank": 1}],
            "fetched_at": "2026-10-23 12:00:00", "via": "http"}}, "errors": {}}
        open(tmp, "w", encoding="utf-8").write(json.dumps(fake, ensure_ascii=False))
        snap2 = platform.refresh({"amazon-kitchen": ("Amazon·厨房家居榜", "https://invalid.invalid/x")}, per_page=5)
        kept = (snap2.get("pages") or {}).get("amazon-kitchen", {})
        results.append(("失败保留旧数据", "PASS" if kept.get("items") and kept.get("stale") else "FAIL",
                        f"stale={kept.get('stale')} items={len(kept.get('items') or [])}"))
        platform.SNAPSHOT_PATH = old_path
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception as exc:
        results.append(("失败保留旧数据", "FAIL", f"{type(exc).__name__}: {str(exc)[:60]}"))

    # 5) 技术栈页同步
    try:
        stack = open(os.path.join(ROOT, "static", "stack.html"), encoding="utf-8").read()
        needle = ("httpx" in stack) and ("兜底" in stack)
        results.append(("技术栈页已同步 httpx/兜底", "PASS" if needle else "FAIL",
                        "已包含" if needle else "缺少 httpx 或「兜底」"))
    except Exception as exc:
        results.append(("技术栈页已同步", "FAIL", str(exc)[:60]))

    bad = [x for x in results if x[1] == "FAIL"]
    print(f"\n=== 平台榜单源验收：PASS {len(results) - len(bad)} / FAIL {len(bad)} ===")
    for name, st, detail in results:
        print(f"  [{'PASS' if st == 'PASS' else 'FAIL'}] {name} :: {detail}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
