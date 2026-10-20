# -*- coding: utf-8 -*-
"""模块：定时调度与手动触发。职责：按每日固定轮次（默认 08/11/14/17/20/23）
    或测试用间隔（RUN_INTERVAL_MINUTES）触发 pipeline.run_once；用进程内锁保证
    同一时刻只有一轮在跑；启动时若距上次完成超过间隔则补跑一次。
    关键依赖：apscheduler、app.pipeline、app.db、app.config。"""
import os
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .config import settings
from .pipeline import run_once

# 全局互斥：同一时刻只允许一轮
_LOCK = threading.Lock()

# 运行状态（供 /api/run/status 读取）
STATE = {
    "running": False,
    "current_batch_id": None,
    "last_finished_at": None,
    "last_trigger": None,
    "next_run_at": None,
    "last_stats": None,
}

_scheduler = None
_DEEP_LIMIT = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _last_finished_at():
    """从 batches 表取最近一次成功完成的轮次时间（done/degraded）。"""
    try:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT MAX(finished_at) f FROM batches "
                "WHERE status IN ('done','degraded')").fetchone()
            return row["f"] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def run_round(trigger: str = "自动", deep_limit=None):
    """执行一轮。已在运行则跳过（不排队），返回 None。"""
    if not _LOCK.acquire(blocking=False):
        print(f"[调度] 上一轮尚未结束，本轮（{trigger}）跳过", flush=True)
        return None
    STATE["running"] = True
    try:
        st = run_once(deep_limit=deep_limit, trigger=trigger)
        STATE["current_batch_id"] = st.get("batch_id")
        STATE["last_finished_at"] = _now()
        STATE["last_trigger"] = trigger
        STATE["last_stats"] = st
        print(f"[调度] 本轮（{trigger}）完成：批次 #{st.get('batch_id')} "
              f"抓取 {st.get('fetched')} / 深加工 ok {st.get('deep_ok')} "
              f"/ 花费 {st.get('cost', 0):.4f} 元", flush=True)
        return st
    except Exception as exc:
        print(f"[调度] 本轮（{trigger}）失败：{type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        STATE["running"] = False
        _LOCK.release()


def _interval_minutes() -> int:
    """测试用间隔（分钟）；<=0 表示按每日固定时刻。可用环境变量覆盖。"""
    raw = os.environ.get("RUN_INTERVAL_MINUTES", "")
    if not raw:
        raw = getattr(settings, "RUN_INTERVAL_MINUTES", 0) or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def start(deep_limit=None):
    """启动调度器：优先按 RUN_INTERVAL_MINUTES，否则按每日轮次。"""
    global _scheduler, _DEEP_LIMIT
    _DEEP_LIMIT = deep_limit
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    minutes = _interval_minutes()
    if minutes > 0:
        sched.add_job(lambda: run_round("自动", _DEEP_LIMIT),
                      IntervalTrigger(minutes=minutes), id="round",
                      max_instances=1, coalesce=True)
        note = f"每 {minutes} 分钟"
    else:
        for h in (8, 11, 14, 17, 20, 23):
            sched.add_job(lambda: run_round("自动", _DEEP_LIMIT),
                          CronTrigger(hour=h, minute=0), id=f"round_{h}",
                          max_instances=1, coalesce=True)
        note = "每日 08/11/14/17/20/23"

    sched.start()
    job = sched.get_jobs()[0]
    STATE["next_run_at"] = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else None
    print(f"[调度] 已启动：{note}；下一轮 {STATE['next_run_at']}", flush=True)

    # 启动补跑：距上次完成超过间隔（或从未跑过）→ 立即补 1 次
    last = _last_finished_at()
    need = True
    if last:
        try:
            gap = datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            need = gap >= timedelta(minutes=(minutes if minutes > 0 else 180))
        except ValueError:
            need = True
    if need:
        print(f"[调度] 上次完成于 {last or '从未'}，启动补跑 1 次", flush=True)
        threading.Thread(target=run_round, args=("自动", _DEEP_LIMIT),
                         daemon=True).start()
    _scheduler = sched
    return sched


def shutdown():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
        print("[调度] 已停止", flush=True)
