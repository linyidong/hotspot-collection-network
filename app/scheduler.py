# -*- coding: utf-8 -*-
"""模块：定时调度与手动触发。职责：按每日固定轮次（默认 08/11/14/17/20/23）
    或测试用间隔（RUN_INTERVAL_MINUTES）触发 pipeline.run_once；用进程内锁保证
    同一时刻只有一轮在跑；启动时若距上次完成超过间隔则补跑一次。
    另含每日过期数据清理（每天 04:05 触发 + 启动时补一次，失败只记日志）。
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
    """轮次间隔（分钟）：环境变量 > 界面设置 > 默认 180。
    返回 0 表示按每日固定时刻（08/11/14/17/20/23）。"""
    raw = os.environ.get("RUN_INTERVAL_MINUTES", "")
    if not raw:
        try:
            from . import settings_store
            raw = settings_store.get("run_interval_minutes") or 0
        except Exception:
            raw = 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _backup_now():
    """每日数据库备份（保留 7 份）：失败只记日志，不影响采集。"""
    try:
        from .backup import run as backup_run
        r = backup_run()
        print(f"[备份] 已生成 {r['path']}（{r['size'] // 1024} KB，保留 {r['kept']} 份，清理 {r['pruned']} 份）", flush=True)
    except Exception as exc:
        print(f"[备份] 失败（不影响采集）：{type(exc).__name__}: {exc}", flush=True)


def _cleanup_now():
    """过期数据清理：失败只记日志，绝不影响采集。"""
    try:
        from .cleanup import run as cleanup_run
        res = cleanup_run()
        print(f"[清理] 完成：items {res['items']} / analyses {res['analyses']} / "
              f"notes {res['notes']} / favorites {res['favorites']} / "
              f"llm_calls {res['llm_calls']}", flush=True)
    except Exception as exc:
        print(f"[清理] 失败（不影响采集）：{type(exc).__name__}: {exc}", flush=True)


def start(deep_limit=None):
    """启动调度器：优先按 RUN_INTERVAL_MINUTES，否则按每日轮次。
    额外挂每日 04:05 的过期数据清理任务，并立即补跑一次清理。"""
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

    # 每日过期数据清理（04:05，失败只记日志）
    sched.add_job(_backup_now, CronTrigger(hour=3, minute=30), id="backup_daily",
                  max_instances=1, coalesce=True)

    sched.add_job(_cleanup_now, CronTrigger(hour=4, minute=5), id="cleanup_daily",
                  max_instances=1, coalesce=True)

    sched.start()
    # 启动清理：立即补一次过期数据清理
    # 启动补备份：超过 24 小时没有备份就补一份
    try:
        from .backup import last_backup
        import time as _t
        lb = last_backup()
        if lb is None or (_t.time() - lb.stat().st_mtime) > 86400:
            _backup_now()
    except Exception as exc:
        print(f"[备份] 启动检查失败：{exc}", flush=True)

    _cleanup_now()
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
