# -*- coding: utf-8 -*-
"""模块：调度审计报告（T-003）。
职责：调用 app.scheduling_audit 核对固定时刻（08/11/14/17/20/23）是否都有自动批次，
      输出 markdown 报告到 docs/调度审计报告.md；关机/睡眠时段记为「离线跳过」不计入考核。
用法：
    python tools/scheduler_audit.py                  # 默认审计最近 24 小时
    python tools/scheduler_audit.py --hours 48
    python tools/scheduler_audit.py --since "2026-10-22 17:00" --hours 23
关键依赖：app.scheduling_audit、app.db。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.scheduling_audit import audit  # noqa: E402

DB = os.path.join(ROOT, "data", "app.db")
REPORT = os.path.join(ROOT, "docs", "调度审计报告.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="审计最近多少小时（默认 24）")
    ap.add_argument("--since", type=str, default="", help="起始时间 'YYYY-MM-DD HH:MM'")
    args = ap.parse_args()

    now = datetime.now()
    start = (datetime.strptime(args.since, "%Y-%m-%d %H:%M")
             if args.since else now - timedelta(hours=args.hours))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    res = audit(conn, start, now)
    detail = {}
    for r in conn.execute("SELECT id, stats_json FROM batches WHERE started_at >= ?",
                          (start.strftime("%Y-%m-%d %H:%M:%S"),)):
        try:
            detail[r["id"]] = json.loads(r["stats_json"] or "{}")
        except (ValueError, TypeError):
            detail[r["id"]] = {}
    conn.close()

    s = res["summary"]
    if s["online"] == 0:
        print("窗口内没有在线时段（观察尚未开始），不算通过。")
        return 2

    rows = []
    for x in res["slots"]:
        st = detail.get(x["batch_id"]) or {}
        tail = (f"{st.get('sources_ok', '-')}/{st.get('deep_ok', '-')}/¥{st.get('cost', 0):.3f}"
                if x["batch_id"] else "—")
        rows.append(f"| {x['time'][5:16]} | {x['result']} | "
                    f"{('#' + str(x['batch_id'])) if x['batch_id'] else '—'} | "
                    f"{x['started'][11:] if x['started'] else '—'} | {tail} |")

    md = ["# 调度审计报告（T-003）", "",
          f"- 审计窗口：{start:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M}",
          f"- 在线时段：{', '.join(f'{a[5:16]}~{b[5:16]}' for a, b in res['intervals'][:6]) or '—'}",
          "- 考核口径：仅统计「服务在线」时刻；关机/睡眠记为「离线跳过」，不算漏轮", "",
          f"**结论：{'通过' if res['passed'] else '未通过'}**"
          f"（准时 {s['ok']} / 失败 {s['fail']} / 漏轮 {s['miss']} / 离线跳过 {s['offline']}，"
          f"应跑 {s['slots']} 个时刻，在线 {s['online']}）", "",
          "| 应跑时刻 | 结果 | 批次 | 实际开始 | 源/线索/花费 |", "|---|---|---|---|---|"]
    md += rows if rows else ["| — | 窗口内没有应跑时刻 | — | — | — |"]
    md += ["", "> 离线跳过 = 那一刻服务没运行（关机/睡眠），不算代码缺陷；",
           "> 漏轮 = 服务在线却没跑，属于要排查的问题。", ""]
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md) + "\n")

    print("\n".join(md[4:9]))
    print(f"报告：{REPORT}")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
