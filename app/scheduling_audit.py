# -*- coding: utf-8 -*-
"""模块：调度审计（T-003）公共逻辑。
职责：
    - 记录服务心跳（heartbeats 表，每 5 分钟一条），用来区分「服务在线」与「关机/睡眠」；
    - 由心跳还原「在线区间」，只在在线区间内核对每日固定时刻（08/11/14/17/20/23）；
    - 输出每个时刻的结果：准时 / 迟到 / 失败 / 漏轮 / 离线跳过。
输入输出：
    record_heartbeat() 无返回；online_intervals(conn, start, end) 返回 [(s, e)]；
    audit(conn, start, end) 返回 {"slots": [...], "summary": {...}, "passed": bool}。
关键依赖：sqlite3（由调用方传入连接）、datetime。
"""
from datetime import datetime, timedelta

SLOTS = (8, 11, 14, 17, 20, 23)
TOLERANCE_MIN = 45        # 到点后 45 分钟内跑起来算准时/迟到容忍
GAP_MIN = 12              # 两条心跳间隔超过该值视为离线/睡眠
FMT = "%Y-%m-%d %H:%M:%S"


def record_heartbeat(conn) -> None:
    """写一条心跳（同一分钟只保留一条）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("INSERT OR IGNORE INTO heartbeats(ts) VALUES(?)", (now,))
        conn.commit()
    except Exception:
        pass


def online_intervals(conn, start: datetime, end: datetime, gap_min: int = GAP_MIN):
    """由心跳还原在线区间；没有心跳数据时返回整段（视为一直在线）。"""
    rows = conn.execute(
        "SELECT ts FROM heartbeats WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (start.strftime(FMT), end.strftime(FMT))).fetchall()
    stamps = []
    for r in rows:
        try:
            stamps.append(datetime.strptime(r["ts"], FMT))
        except (ValueError, TypeError):
            continue
    if not stamps:
        return [(start, end)]
    out, seg_start, prev = [], stamps[0], stamps[0]
    for t in stamps[1:]:
        if (t - prev) > timedelta(minutes=gap_min):
            out.append((seg_start, prev))
            seg_start = t
        prev = t
    out.append((seg_start, prev))
    # 首尾外扩：心跳间隔内视为在线（±gap_min/2），并夹到审计窗口
    fixed = []
    for s, e in out:
        s = max(start, s - timedelta(minutes=gap_min // 2))
        e = min(end, e + timedelta(minutes=gap_min // 2))
        if e > s:
            fixed.append((s, e))
    return fixed or [(start, start)]


def _is_online(intervals, t: datetime, tol_min: int) -> bool:
    """时刻 t 是否落在某个在线区间（允许前后容差）。"""
    for s, e in intervals:
        if (s - timedelta(minutes=tol_min)) <= t <= (e + timedelta(minutes=tol_min)):
            return True
    return False


def audit(conn, start: datetime, end: datetime, tol_min: int = TOLERANCE_MIN) -> dict:
    """核对 [start, end] 内的每日固定时刻。"""
    intervals = online_intervals(conn, start, end)
    rows = conn.execute(
        "SELECT id, started_at, status, stats_json FROM batches "
        "WHERE trigger='自动' AND started_at >= ? AND started_at <= ? ORDER BY started_at",
        (start.strftime(FMT), end.strftime(FMT))).fetchall()
    autos = []
    for r in rows:
        try:
            autos.append((datetime.strptime(r["started_at"], FMT), r))
        except (ValueError, TypeError):
            continue

    slots = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for h in SLOTS:
            t = day.replace(hour=h)
            if start <= t <= end:
                slots.append(t)
        day += timedelta(days=1)

    items, ok = [], 0
    fail = miss = offline = late = 0
    for t in slots:
        online = _is_online(intervals, t, tol_min)
        hit = None
        for dt, r in autos:
            delta = (dt - t).total_seconds() / 60
            if 0 <= delta <= tol_min:
                hit = (dt, r, delta)
                break
        if hit:
            dt, r, delta = hit
            status = r["status"]
            if status in ("done", "degraded"):
                ok += 1
                label = "准时" if delta <= 5 else f"迟到{int(delta)}分"
            else:
                fail += 1
                label = f"失败({status})"
            items.append({"time": t.strftime(FMT), "result": label, "online": online,
                          "batch_id": r["id"], "started": dt.strftime(FMT)})
        elif not online:
            offline += 1
            items.append({"time": t.strftime(FMT), "result": "离线跳过", "online": False,
                          "batch_id": None, "started": None})
        else:
            miss += 1
            items.append({"time": t.strftime(FMT), "result": "漏轮", "online": True,
                          "batch_id": None, "started": None})

    counted = ok + fail + miss           # 离线跳过的时刻不计入考核
    summary = {"slots": len(slots), "online": len(slots) - offline, "ok": ok,
               "late": late, "fail": fail, "miss": miss, "offline": offline}
    passed = counted > 0 and fail == 0 and miss == 0
    return {"slots": items, "summary": summary, "passed": passed,
            "insufficient": counted == 0,
            "intervals": [(s.strftime(FMT), e.strftime(FMT)) for s, e in intervals]}
