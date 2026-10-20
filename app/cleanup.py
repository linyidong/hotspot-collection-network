# -*- coding: utf-8 -*-
"""模块：过期数据清理。职责：按 item_retention_days 删除过期 items（连带其
   analyses/notes/favorites），llm_calls 保留 30 天；返回各表删除行数。
   失败向上抛给调用方（调度器记日志），绝不影响采集。关键依赖：app.db、app.settings_store。"""
from datetime import datetime, timedelta

from . import db, settings_store

LLM_CALLS_RETENTION_DAYS = 30      # 调用日志保留 30 天


def run(conn=None) -> dict:
    """执行一轮清理。返回 {'items','analyses','notes','favorites','llm_calls'} 删除行数。"""
    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        cfg = settings_store.load()
        days = int(cfg.get("item_retention_days") or 90)
        now = datetime.now()
        cutoff_items = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cutoff_calls = (now - timedelta(days=LLM_CALLS_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        # 过期条目：优先按发布时间，缺发布时间按所属批次开始时间兜底
        old = [r["id"] for r in conn.execute(
            "SELECT i.id FROM items i LEFT JOIN batches b ON b.id=i.batch_id "
            "WHERE COALESCE(i.published_at, b.started_at) < ?", (cutoff_items,)).fetchall()]

        n = {"items": 0, "analyses": 0, "notes": 0, "favorites": 0, "llm_calls": 0}
        if old:
            marks = ",".join("?" * len(old))
            n["favorites"] = conn.execute(
                f"DELETE FROM favorites WHERE item_id IN ({marks})", old).rowcount
            n["notes"] = conn.execute(
                f"DELETE FROM notes WHERE item_id IN ({marks})", old).rowcount
            n["analyses"] = conn.execute(
                f"DELETE FROM analyses WHERE item_id IN ({marks})", old).rowcount
            n["items"] = conn.execute(
                f"DELETE FROM items WHERE id IN ({marks})", old).rowcount
        # 孤立解析记录（条目已不在但 analyses 残留）一并清掉
        n["analyses"] += conn.execute(
            "DELETE FROM analyses WHERE item_id NOT IN (SELECT id FROM items)").rowcount
        n["llm_calls"] = conn.execute(
            "DELETE FROM llm_calls WHERE created_at < ?", (cutoff_calls,)).rowcount
        conn.commit()
        return n
    finally:
        if own:
            conn.close()
