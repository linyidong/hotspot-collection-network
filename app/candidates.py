# -*- coding: utf-8 -*-
"""模块：候选池（F-005 / F-103）。职责：保存每轮超出深加工限额的粗筛合格条目，
    上限 200（超出按 potential 从低到高淘汰），并提供手动提级读取/移除。
    输入：items.id / batch_id / potential / category / opportunity_type；
    输出：池条目列表与增删计数。关键依赖：app.db。
    注意：候选池条目从不删除 item 与 analyses，只删除池记录。"""

from datetime import datetime

from . import db

CAP = 200


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_many(conn, batch_id: int, entries: list, cap: int = CAP) -> dict:
    """加入候选池（item_id 幂等），随后按上限淘汰低潜力条目。
    entries: [{item_id,potential,category,opportunity_type}]。
    返回 {inserted, evicted, total}；不 commit（由调用方统一提交）。"""
    inserted = 0
    ts = _now()
    for e in entries or []:
        cur = conn.execute(
            "INSERT OR IGNORE INTO candidate_pool"
            "(item_id,batch_id,potential,category,opportunity_type,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (e["item_id"], batch_id, int(e.get("potential") or 0),
             (e.get("category") or "")[:80], (e.get("opportunity_type") or ""),
             ts))
        inserted += cur.rowcount
    total = conn.execute("SELECT COUNT(*) c FROM candidate_pool").fetchone()["c"]
    evicted = 0
    if total > cap:
        over = total - cap
        rows = conn.execute(
            "SELECT item_id FROM candidate_pool "
            "ORDER BY potential ASC, created_at ASC, item_id ASC LIMIT ?",
            (over,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM candidate_pool WHERE item_id=?", (r["item_id"],))
        evicted = len(rows)
        total -= evicted
    return {"inserted": inserted, "evicted": evicted, "total": total}


def list_items(limit: int = 50, offset: int = 0, q: str = "") -> dict:
    """按 potential 降序返回候选池条目（支持分页/标题/来源搜索）与匹配总数。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    q = (q or "").strip()
    conn = db.get_conn()
    try:
        where, args = [], []
        if q:
            where.append("(i.title LIKE ? OR s.name LIKE ?)")
            args.extend([f"%{q}%", f"%{q}%"])
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            "SELECT COUNT(*) c FROM candidate_pool c JOIN items i ON i.id=c.item_id "
            "LEFT JOIN sources s ON s.id=i.source_id" + wsql, args).fetchone()["c"]
        rows = conn.execute(
            "SELECT c.item_id, c.batch_id, c.potential, c.category, "
            "c.opportunity_type, c.created_at, i.title, i.url, i.published_at, "
            "s.name AS source "
            "FROM candidate_pool c JOIN items i ON i.id=c.item_id "
            "LEFT JOIN sources s ON s.id=i.source_id" + wsql +
            " ORDER BY c.potential DESC, c.created_at DESC, c.item_id DESC "
            "LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total,
                "cap": CAP, "limit": limit, "offset": offset, "q": q}
    finally:
        conn.close()


def get(conn, item_id: int):
    return conn.execute(
        "SELECT * FROM candidate_pool WHERE item_id=?", (item_id,)).fetchone()


def remove(conn, item_id: int) -> None:
    conn.execute("DELETE FROM candidate_pool WHERE item_id=?", (item_id,))


def count() -> int:
    conn = db.get_conn()
    try:
        return conn.execute("SELECT COUNT(*) c FROM candidate_pool").fetchone()["c"]
    finally:
        conn.close()
