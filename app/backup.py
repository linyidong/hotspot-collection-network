# -*- coding: utf-8 -*-
"""模块：数据库每日备份（T11 交付要求：每日一份、保留 7 份、可恢复）。
职责：
  1. 用 SQLite 在线备份 API 把 data/app.db 复制到 data/backups/app-YYYYmmdd-HHMMSS.db
     （不走文件复制，避免 WAL 未落盘导致备份损坏）。
  2. 备份后做 PRAGMA quick_check，校验不通过则删除该备份并抛异常。
  3. 清理超出保留份数的旧备份（默认 7 份）。
输出：dict(ok, path, size, kept, pruned)；失败抛异常，由调用方决定是否只记日志。
关键依赖：sqlite3、pathlib、datetime、app.config。
"""

from datetime import datetime
from pathlib import Path

from .config import ROOT, settings

BACKUP_DIR = ROOT / "data" / "backups"
KEEP = 7


def _db_path() -> Path:
    return Path(settings.db_path)


def run(keep: int = KEEP) -> dict:
    """执行一次备份 + 校验 + 清理，返回结果摘要。"""
    src = _db_path()
    if not src.exists():
        raise FileNotFoundError(f"数据库不存在：{src}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"app-{stamp}.db"

    # 在线备份（WAL 安全）
    import sqlite3
    s_conn = sqlite3.connect(str(src))
    d_conn = sqlite3.connect(str(dst))
    try:
        with d_conn:
            s_conn.backup(d_conn)
    finally:
        s_conn.close()
        d_conn.close()

    # 校验
    chk = sqlite3.connect(str(dst))
    try:
        ok = chk.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        chk.close()
    if not ok:
        dst.unlink(missing_ok=True)
        raise RuntimeError("备份文件校验失败（PRAGMA quick_check != ok），已删除该文件")

    # 只保留最近 keep 份
    files = sorted(BACKUP_DIR.glob("app-*.db"), key=lambda p: p.name, reverse=True)
    pruned = 0
    for old in files[keep:]:
        try:
            old.unlink()
            pruned += 1
        except OSError:
            pass

    return {
        "ok": True,
        "path": str(dst),
        "size": dst.stat().st_size,
        "kept": min(len(files), keep),
        "pruned": pruned,
    }


def last_backup() -> Path | None:
    """最近一份备份（没有则返回 None）。"""
    if not BACKUP_DIR.exists():
        return None
    files = sorted(BACKUP_DIR.glob("app-*.db"), key=lambda p: p.name, reverse=True)
    return files[0] if files else None
