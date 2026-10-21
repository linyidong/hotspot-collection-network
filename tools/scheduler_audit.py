# -*- coding: utf-8 -*-
"""模块：调度审计（T-003「连续观察，6 轮无漏轮」）。
职责：检查过去 N 天内，每日固定时刻（默认 08/11/14/17/20/23）是否都有
      `trigger=自动` 的批次；统计漏轮、迟到、失败，并输出 markdown 报告。
用法：
    python tools/scheduler_audit.py                 # 审计最近 24 小时
    python tools/scheduler_audit.py --hours 48
    python tools/scheduler_audit.py --since "2026-10-21 16:00"
关键依赖：sqlite3、argparse、datetime。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "app.db")
REPORT = os.path.join(ROOT, "docs", "调度审计报告.md")
SLOTS = [8, 11, 14, 17, 20, 23]
TOLERANCE_MIN = 45          # 到点后 45 分钟内跑起来算"准时"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="审计最近多少小时（默认 24）")
    ap.add_argument("--since", type=str, default="", help="起始时间 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--slots", type=str, default="8,11,14,17,20,23")
    args = ap.parse_args()

    slots = [int(x) for x in args.slots.split(",") if x.strip()]
    now = datetime.now()
    if args.since:
        start = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
    else:
        start = now - timedelta(hours=args.hours)

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, trigger, status, started_at, finished_at, stats_json FROM batches "
        "WHERE started_at >= ? ORDER BY started_at", (start.strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
    c.close()

    autos = []
    for r in rows:
        try:
            dt = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        autos.append((dt, r))

    # 枚举窗口内每个应跑时刻
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    slots_all = []
    while day <= now:
        for h in slots:
            t = day.replace(hour=h)
            if start <= t <= now:
                slots_all.append(t)
        day += timedelta(days=1)

    lines, ok_n, late_n, miss_n, fail_n = [], 0, 0, 0, 0
    for t in slots_all:
        hit = None
        for dt, r in autos:
            if r["trigger"] != "自动":
                continue
            delta = (dt - t).total_seconds() / 60
            if 0 <= delta <= TOLERANCE_MIN:
                hit = (dt, r, delta)
                break
        if not hit:
            miss_n += 1
            lines.append(f"| {t:%m-%d %H:%M} | ❌ 漏轮 | — | — | — |")
            continue
        dt, r, delta = hit
        status = r["status"]
        if status in ("done", "degraded"):
            if delta <= 5:
                ok_n += 1
                tag = "✅ 准时"
            else:
                late_n += 1
                tag = f"🟡 迟到 {int(delta)} 分"
        else:
            fail_n += 1
            tag = f"❌ 失败（{status}）"
        try:
            st = json.loads(r["stats_json"] or "{}")
        except (ValueError, TypeError):
            st = {}
        lines.append(f"| {t:%m-%d %H:%M} | {tag} | #{r['id']} | {r['started_at'][11:]} | "
                     f"{st.get('sources_ok', '-')}/{st.get('deep_ok', '-')}/¥{st.get('cost', 0):.3f} |")

    total = len(slots_all)
    if total == 0:
        print("窗口内没有应跑时刻（观察尚未开始），不算通过。")
        return 2
    passed = miss_n == 0
    md = [f"# 调度审计报告（T-003）", "",
          f"- 审计窗口：{start:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M}",
          f"- 固定时刻：{', '.join(f'{h:02d}:00' for h in slots)}（迟到容忍 {TOLERANCE_MIN} 分钟）", "",
          f"**结论：{'通过' if passed else '未通过'}**（准时 {ok_n} / 迟到 {late_n} / 漏轮 {miss_n} / 失败 {fail_n}，共 {total} 个时刻）", "",
          "| 应跑时刻 | 结果 | 批次 | 实际开始 | 源/线索/花费 |", "|---|---|---|---|---|"]
    md += lines if lines else ["| — | 窗口内没有应跑时刻 | — | — | — |"]
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md) + "\n")

    print("\n".join(md[3:9]))
    print(f"报告：{REPORT}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
