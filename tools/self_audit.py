# -*- coding: utf-8 -*-
"""工具：项目全量自检（数据/备份/文档/安全）。
职责：一次跑完体检项，输出可读结论，供闭环检查使用。
用法：python tools/self_audit.py
关键依赖：sqlite3、git、本地 data/ 目录。
"""
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "app.db")
BACKUPS = os.path.join(ROOT, "data", "backups")
DOCS = os.path.join(ROOT, "docs")


def q(conn, sql):
    row = conn.execute(sql).fetchone()
    return row[0] if row else 0


def main():
    print("=== A. 数据健康 ===")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("items 总数      :", q(conn, "select count(*) from items"))
    print("重复标记        :", q(conn, "select count(*) from items where is_duplicate=1"))
    print("线索卡(deep ok) :", q(conn, "select count(*) from analyses where stage='deep' and status='ok'"))
    print("校验拒绝        :", q(conn, "select count(*) from analyses where stage='deep' and status='rejected'"))
    print("今日花费(元)    :", round(q(conn, "select coalesce(sum(cost),0) from llm_calls where created_at >= '2026-10-23 00:00:00'"), 3))
    print("本月花费(元)    :", round(q(conn, "select coalesce(sum(cost),0) from llm_calls where created_at >= '2026-10-01'"), 3))
    print("批次状态分布    :", [tuple(r) for r in conn.execute(
        "select status, count(*) from batches group by status")])
    print("最近 3 批       :", [dict(r) for r in conn.execute(
        "select id, status, started_at, finished_at from batches order by id desc limit 3")])
    dup_ratio = q(conn, "select count(*) from items where is_duplicate=1") / max(1, q(conn, "select count(*) from items"))
    print("重复率          : %.1f%%" % (dup_ratio * 100))
    conn.close()

    print("\n=== B. 备份 ===")
    if os.path.isdir(BACKUPS):
        files = sorted(
            [f for f in os.listdir(BACKUPS) if f.endswith(".db")],
            key=lambda f: os.path.getmtime(os.path.join(BACKUPS, f)), reverse=True)
        print("DB 备份数       :", len(files))
        for f in files[:3]:
            print("   ", f, "%.1f MB" % (os.path.getsize(os.path.join(BACKUPS, f)) / 1048576))
        if files:
            import datetime
            newest = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(BACKUPS, files[0])))
            age_h = (datetime.datetime.now() - newest).total_seconds() / 3600
            print("最新备份距今    : %.1f 小时" % age_h)
            print("结论            :", "正常" if age_h <= 30 else "⚠ 超过 30 小时未备份（可能夜间离线错过 03:30）")
    bundle = os.path.join(BACKUPS, "repo-20261023.bundle")
    if os.path.exists(bundle):
        out = subprocess.run(["git", "-C", ROOT, "bundle", "verify", bundle],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("git bundle      :", "完整" if out.returncode == 0 else "校验失败")

    print("\n=== C. 安全 ===")
    hits = subprocess.run(["git", "-C", ROOT, "grep", "-I", "-n", "-E", r"sk-[A-Za-z0-9]{20,}"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    real = [l for l in hits.stdout.splitlines() if "xxxx" not in l.lower()]
    print("仓库明文 Key    :", "无" if not real else real[:3])
    tracked = subprocess.run(["git", "-C", ROOT, "ls-files", ".env"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(".env 是否跟踪   :", "是（危险）" if tracked.stdout.strip() else "否")
    print(".env 本地存在   :", os.path.exists(os.path.join(ROOT, ".env")))

    print("\n=== D. 文档一致性 ===")
    stale = []
    for name in os.listdir(DOCS):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(DOCS, name), encoding="utf-8", errors="replace").read()
        for pat in ("（8 个源", "（8 源", "（8 张表", "：8 张表", "共 8 源"):
            if pat in text:
                stale.append(f"{name}: {pat}")
    print("可疑过时表述    :", stale[:5] if stale else "无")

    print("\n=== E. 静态资源引用检查（README 图片）===")
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    missing = []
    for m in re.finditer(r"\]\((docs/[^)]+\.(?:webp|png|jpg))\)", readme):
        if not os.path.exists(os.path.join(ROOT, m.group(1))):
            missing.append(m.group(1))
    print("README 缺图     :", missing if missing else "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
