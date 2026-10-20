# -*- coding: utf-8 -*-
"""模块：预算熔断。职责：按月预算统计本月已花费（llm_calls 成本求和）与剩余比例，
   判定当前熔断模式 mode ∈ ok/soft/hard（剩余 < 硬停阈值 → hard；< 软停阈值 → soft）。
   关键依赖：app.db、app.settings_store。"""
from datetime import datetime

from . import db, settings_store


def _month_start() -> str:
    """本月 1 日 00:00:00（llm_calls.created_at 为本地时间字符串，可直接比较）。"""
    return datetime.now().strftime("%Y-%m-01 00:00:00")


def spent_this_month(conn=None) -> float:
    """本月 llm_calls 成本合计（元，round 6 位）。conn 复用连接时由调用方负责提交。"""
    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost),0) c FROM llm_calls WHERE created_at >= ?",
            (_month_start(),)).fetchone()
        return round(row["c"] or 0.0, 6)
    finally:
        if own:
            conn.close()


def status(conn=None) -> dict:
    """预算状态：{budget, spent, remaining, ratio, mode}。
    mode：剩余比例 < budget_hard_ratio → hard；< budget_stop_ratio → soft；否则 ok。"""
    cfg = settings_store.load()
    budget = float(cfg.get("monthly_budget_cny") or 0.0)
    spent = spent_this_month(conn=conn)
    remaining = round(max(0.0, budget - spent), 6)
    ratio = round(remaining / budget, 6) if budget > 0 else 0.0
    hard = float(cfg.get("budget_hard_ratio") or 0.0)
    stop = float(cfg.get("budget_stop_ratio") or 0.0)
    mode = "hard" if ratio < hard else ("soft" if ratio < stop else "ok")
    return {"budget": budget, "spent": spent, "remaining": remaining,
            "ratio": ratio, "mode": mode}
