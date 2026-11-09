# -*- coding: utf-8 -*-
"""模块：API 路由。职责：创建 FastAPI 应用，提供 GET /api/health、GET/PUT /api/sources；
    根路径返回 static/index.html，并挂载 /static 静态目录。
    /api/health 携带 bridge.ok（RSS 桥状态，挂了自动重启）；/api/sources 返回 8 源状态。
    关键依赖：fastapi、app.config、app.db、app.bridge、app.sources.registry。"""
import json
import threading
import time
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import candidates as candidates_mod
from .. import db
from .. import search as search_mod
from ..bridge import bridge
from ..config import settings
from ..sources.registry import SOURCES, get_spec

# 进程启动时刻，用于 uptime_seconds
_START = time.time()

TABLES = ("sources", "batches", "items", "analyses", "notes",
          "favorites", "llm_calls", "config", "heartbeats", "candidate_pool")

PROXY_MODES = ("direct", "proxy")
SOCKS5_SOURCE_IDS = ("gnews-kuajing", "gnews-xuanpin", "gnews-guanshui",
                     "gnews-platform", "gnews-warehouse")
SOCKS5_SOURCE_NAMES = tuple(get_spec(x).name for x in SOCKS5_SOURCE_IDS
                            if get_spec(x) is not None)


def _mask_proxy_url(url) -> str | None:
    """代理地址脱敏：有 user:pass 时只显示 scheme://***:***@host:port。
    配置/日志/API 一律不返回明文密码（docs/人工参数 §4）。"""
    if not url:
        return None
    try:
        p = urlsplit(str(url))
    except ValueError:
        return "***"
    if p.username is None and p.password is None:
        return p._replace(fragment="").geturl()
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return f"{p.scheme}://***:***@{host}"


def _adjust_batch_stats(conn, batch_id, old_status, new_status,
                        cost_delta, old_reasons, new_reasons,
                        skipped_delta=0, candidates_delta=0, pool_delta=0,
                        manual_delta=0) -> bool:
    """按一次深加工重跑调整 batches.stats_json（T8：重加工后批次账必须同步）。
    输入：批次 id、旧/新 deep 状态（ok/rejected/failed/None=从未深加工）、
    本次新调用成本 cost_delta、旧/新拒绝原因。
    更新 deep_ok/deep_rejected/deep_failed/deep_r11/cost 五个键，其余键原样保留；
    计数用 max(0,…) 防负；stats_json 缺失或 JSON 非法时安全跳过返回 False（不写坏数据）。
    SELECT + UPDATE + commit 在同一连接同一事务内完成。返回是否更新成功。"""
    if batch_id is None:
        return False
    row = conn.execute("SELECT stats_json FROM batches WHERE id=?",
                       (batch_id,)).fetchone()
    if row is None or not row["stats_json"]:
        return False
    try:
        stats = json.loads(row["stats_json"])
    except (ValueError, TypeError):
        return False
    if not isinstance(stats, dict):
        return False

    counters = {"ok": "deep_ok", "rejected": "deep_rejected", "failed": "deep_failed"}
    for status, sign in ((old_status, -1), (new_status, 1)):
        key = counters.get(status)
        if key is None:
            continue
        cur = stats.get(key)
        cur = int(cur) if isinstance(cur, (int, float)) else 0
        stats[key] = max(0, cur + sign)

    def _is_r11(status, reasons):
        return status == "rejected" and any(
            str(x).startswith("r11") for x in (reasons or []))
    old_r11 = _is_r11(old_status, old_reasons)
    new_r11 = _is_r11(new_status, new_reasons)
    if old_r11 != new_r11:
        cur = stats.get("deep_r11")
        cur = int(cur) if isinstance(cur, (int, float)) else 0
        stats["deep_r11"] = max(0, cur + (1 if new_r11 else -1))

    cur_cost = stats.get("cost")
    cur_cost = float(cur_cost) if isinstance(cur_cost, (int, float)) else 0.0
    stats["cost"] = round(cur_cost + float(cost_delta or 0.0), 6)
    if skipped_delta:
        cur = stats.get("deep_skipped")
        cur = int(cur) if isinstance(cur, (int, float)) else 0
        stats["deep_skipped"] = max(0, cur + int(skipped_delta))
    if candidates_delta:
        cur = stats.get("deep_candidates")
        cur = int(cur) if isinstance(cur, (int, float)) else 0
        stats["deep_candidates"] = max(0, cur + int(candidates_delta))
    if pool_delta:
        cur = stats.get("candidate_pool_total")
        cur = int(cur) if isinstance(cur, (int, float)) else 0
        stats["candidate_pool_total"] = max(0, cur + int(pool_delta))
    if manual_delta:
        cur = stats.get("manual_promotions")
        cur = int(cur) if isinstance(cur, (int, float)) else 0
        stats["manual_promotions"] = max(0, cur + int(manual_delta))

    conn.execute("UPDATE batches SET stats_json=? WHERE id=?",
                 (json.dumps(stats, ensure_ascii=False), batch_id))
    conn.commit()
    return True


def _source_status(row) -> str:
    """由数据库状态推断源当前状态：最近失败→error；最近成功→ok；从未→never。"""
    if row["last_error"]:
        return "error"
    if row["last_ok_at"]:
        return "ok"
    return "never"


SNAPSHOT_MAX_AGE_HOURS = 20  # 快照超过该小时数即视为过期（T9-A）


def _snap_age_hours(value) -> int:
    """时间戳距今整数小时；非法/缺失返回 None（T9-B，不抛异常）。"""
    if not value:
        return None
    dt = None
    for parser in (lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
                   lambda v: datetime.fromisoformat(str(v))):
        try:
            dt = parser(str(value))
            break
        except (ValueError, TypeError):
            continue
    if dt is None:
        return None
    return int((datetime.now() - dt).total_seconds() // 3600)


def _snapshot_staleness(snap, load_error, page_key) -> dict:
    """快照源过期判定（T9-A/T9-B）：Amazon 榜单读本地快照，快照可能过期但 status 仍是 ok，
    必须单独用 stale 字段暴露，避免「stale 假绿」（静默失败红线）。
    输入：snap=platform.load() 结果（读取失败时为 None）、load_error=读取失败原因（中文可空）、
         page_key=spec.path 对应的快照页键。
    输出四字段：snapshot_ts / snapshot_age_hours（整数，无可用时间戳为 null）/
         stale（bool）/ stale_reason（中文短语，不 stale 时为 null）。
    T9-B：age 优先按「该页 stale 时的 page.fetched_at」计算（真实数据年龄），
         没有 fetched_at 再退回顶层 ts；否则历史 ts 被错误重写时会显示假新鲜。
    stale 条件（满足任一）：快照读取失败、快照缺失、该页缺失、page.stale 为真、age>20 小时。
    本函数不抛异常：读失败只标 stale，绝不让 /api/sources 返回 500。"""
    out = {"snapshot_ts": None, "snapshot_age_hours": None,
           "stale": False, "stale_reason": None}
    if load_error:
        out["stale"] = True
        out["stale_reason"] = "快照文件读取失败：" + str(load_error)
        return out
    reasons = []
    snap = snap or {}
    ts = snap.get("ts")
    out["snapshot_ts"] = ts
    page = (snap.get("pages") or {}).get(page_key)
    page_stale = bool(isinstance(page, dict) and page.get("stale"))
    # 年龄基准：stale 页优先用其 fetched_at（真实数据时间），否则退回顶层 ts（T9-B）
    age = None
    if page_stale:
        age = _snap_age_hours(page.get("fetched_at"))
    if age is None:
        age = _snap_age_hours(ts)
    out["snapshot_age_hours"] = age
    if not ts:
        reasons.append("快照缺失")
    if age is not None and age > SNAPSHOT_MAX_AGE_HOURS:
        reasons.append("快照已过期 {} 小时".format(age))
    if page is None:
        if ts:
            reasons.append("快照中缺少该榜单页")
    elif page_stale:
        fetched = page.get("fetched_at")
        reasons.append("该榜单页抓取失败（沿用 {} 旧数据）".format(fetched)
                       if fetched else "该榜单页抓取失败（沿用旧数据）")
    if reasons:
        out["stale"] = True
        out["stale_reason"] = "；".join(reasons)
    return out


def _load_sources() -> list:
    """8 源全量（registry 元数据 + 数据库开关/状态）。"""
    conn = db.get_conn()
    try:
        rows = {r["name"]: r for r in conn.execute(
            "SELECT name, url, enabled, proxy_mode, proxy_url, "
            "last_ok_at, last_error, last_count FROM sources").fetchall()}
    finally:
        conn.close()
    # 快照源（Amazon 榜单）：整体只读一次快照，读失败不抛 500，改标 stale（T9-A）
    snap, snap_err = None, None
    if any(getattr(spec, "type", "") == "snapshot" for spec in SOURCES):
        try:
            from .. import platform as _platform
            snap = _platform.load()
        except Exception as e:
            snap_err = str(e) or e.__class__.__name__
    out = []
    for spec in SOURCES:
        row = rows.get(spec.name)
        d = {
            "id": spec.id,
            "name": spec.name,
            "type": spec.type,
            "path": spec.path,
            "channel": spec.channel,
            "url": spec.url,
            "enabled": (row["enabled"] if row else 1) == 1,
            "proxy_mode": row["proxy_mode"] if row else "direct",
            "proxy_url": _mask_proxy_url(row["proxy_url"] if row else None),
            "proxy_url_set": bool(row and row["proxy_url"]),
            "last_ok_at": row["last_ok_at"] if row else None,
            "last_error": row["last_error"] if row else None,
            "last_count": row["last_count"] if row else 0,
            "status": _source_status(row) if row else "never",
        }
        if getattr(spec, "type", "") == "snapshot":
            d.update(_snapshot_staleness(snap, snap_err, spec.path))
        out.append(d)
    return out


def create_app() -> FastAPI:
    """组装 FastAPI 应用：先建表，再挂路由与静态资源。"""
    app = FastAPI(title="Hotspot Collection Network", version=settings.VERSION, docs_url="/api/docs")
    app.state.start_time = _START

    @app.get("/api/health")
    def health():
        ok, names = db.db_ok()
        uptime = round(time.time() - _START, 1)
        # 桥的自愈放后台线程：健康检查保持毫秒级，避免启动脚本误判「启动超时」
        if not globals().get("_bridge_checking"):
            globals()["_bridge_checking"] = True

            def _kick_bridge():
                try:
                    bridge.ensure()
                except Exception:
                    pass
                finally:
                    globals()["_bridge_checking"] = False

            threading.Thread(target=_kick_bridge, daemon=True).start()
        return {
            "status": "ok",
            "version": settings.VERSION,
            "db": {"ok": ok, "tables": len(names), "path": str(settings.db_path)},
            "db_ok": ok,
            "db_tables": len(names),
            "key_configured": settings.key_configured,
            "search_configured": search_mod.configured(),
            "uptime_seconds": uptime,
            "bridge": {
                "ok": bridge.is_alive(),
                "port": bridge.port,
                "restarts": bridge.restarts,
            },
        }

    @app.get("/api/sources")
    def list_sources():
        """8 个数据源：元数据 + 开关/代理 + 健康状态 + 上次条数。"""
        return {"sources": _load_sources()}

    @app.get("/api/items")
    def items(batch: str = "latest", limit: int = 20):
        """条目列表。batch=latest 取最近批次，limit 默认 20。
        返回 title/source/published_at/summary/potential/is_economic/anchors。"""
        limit = max(1, min(int(limit), 200))
        conn = db.get_conn()
        try:
            if batch == "latest":
                row = conn.execute(
                    "SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:
                    return {"items": [], "batch_id": None}
                batch_id = row["id"]
            else:
                try:
                    batch_id = int(batch)
                except ValueError:
                    return {"ok": False, "error": "batch 参数必须是 latest 或批次数字"}
            rows = conn.execute(
                "SELECT i.id, i.title, i.url, i.summary, i.published_at, "
                "i.is_duplicate, i.duplicate_of, s.name AS source, "
                "a.payload_json, a.status AS coarse_status "
                "FROM items i JOIN sources s ON s.id=i.source_id "
                "LEFT JOIN analyses a ON a.item_id=i.id AND a.id=("
                "  SELECT MAX(a2.id) FROM analyses a2 WHERE a2.item_id=i.id AND a2.stage='coarse') "
                "WHERE i.batch_id=? ORDER BY i.id DESC LIMIT ?",
                (batch_id, limit)).fetchall()
            out = []
            for r in rows:
                payload = {}
                if r["payload_json"]:
                    try:
                        payload = json.loads(r["payload_json"])
                    except (ValueError, TypeError):
                        payload = {}
                out.append({
                    "id": r["id"], "title": r["title"], "url": r["url"],
                    "source": r["source"], "published_at": r["published_at"],
                    "summary": r["summary"], "is_duplicate": r["is_duplicate"],
                    "duplicate_of": r["duplicate_of"], "coarse_status": r["coarse_status"],
                    "potential": payload.get("potential"),
                    "is_economic": payload.get("is_economic"),
                    "opportunity_type": payload.get("opportunity_type"),
                    "anchors": payload.get("anchors"),
                })
            return {"items": out, "batch_id": batch_id}
        finally:
            conn.close()

    @app.get("/api/batches")
    def batches():
        """批次列表（id/started_at/finished_at/status/统计）。"""
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT b.id, b.started_at, b.finished_at, b.trigger, b.status, b.stats_json, "
                "COUNT(i.id) AS item_count "
                "FROM batches b LEFT JOIN items i ON i.batch_id=b.id "
                "GROUP BY b.id ORDER BY b.id DESC LIMIT 50").fetchall()
            out = []
            for r in rows:
                stats = {}
                if r["stats_json"]:
                    try:
                        stats = json.loads(r["stats_json"])
                    except (ValueError, TypeError):
                        stats = {}
                out.append({
                    "id": r["id"], "started_at": r["started_at"],
                    "finished_at": r["finished_at"], "trigger": r["trigger"],
                    "status": r["status"], "stats": stats, "item_count": r["item_count"],
                })
            return {"batches": out}
        finally:
            conn.close()

    @app.post("/api/run")
    def run_now(deep_limit: int = Body(default=None, embed=True)):
        """手动触发一轮（后台线程执行，立即返回）。已有轮次在跑 → 409。"""
        from .. import scheduler as sched
        if sched.STATE["running"]:
            return JSONResponse(
                status_code=409,
                content={"accepted": False,
                         "reason": "上一轮尚未结束，请等它跑完（或稍后重试）",
                         "current_batch_id": sched.STATE["current_batch_id"]})
        threading.Thread(target=sched.run_round, args=("手动", deep_limit),
                         daemon=True).start()
        return {"accepted": True, "trigger": "手动",
                "deep_limit": deep_limit,
                "message": "已开始本轮，进度见 /api/run/status"}

    @app.get("/api/run/status")
    def run_status():
        """调度与轮次状态：是否在跑、当前批次、上次完成、下次计划。"""
        from .. import scheduler as sched
        sched.refresh_next_run()      # 每次查询实时计算，避免停在启动时的时间
        return dict(sched.STATE)

    @app.get("/api/leads")
    def leads(batch: str = "latest", limit: int = 50):
        """线索卡列表（T5：深加工 ok 的条目）。batch=latest 取含线索卡的最新批次。
        返回 stage/vertical/confidence/potential/事实/下一步。"""
        limit = max(1, min(int(limit), 200))
        conn = db.get_conn()
        try:
            if batch == "latest":
                row = conn.execute(
                    "SELECT b.id FROM batches b WHERE EXISTS ("
                    "  SELECT 1 FROM items i JOIN analyses a ON a.item_id=i.id "
                    "  AND a.stage='deep' AND a.status='ok' WHERE i.batch_id=b.id)"
                    " ORDER BY b.id DESC LIMIT 1").fetchone()
                if row is None:
                    return {"batch_id": None, "leads": [], "count": 0}
                batch_id = row["id"]
            else:
                try:
                    batch_id = int(batch)
                except ValueError:
                    return {"ok": False, "error": "batch 参数必须是 latest 或批次数字"}
            rows = conn.execute(
                "SELECT i.id, i.title, i.url, i.published_at, s.name AS source, "
                "a.payload_json, ca.payload_json AS coarse_json "
                "FROM analyses a JOIN items i ON i.id=a.item_id "
                "JOIN sources s ON s.id=i.source_id "
                "LEFT JOIN analyses ca ON ca.item_id=i.id AND ca.id=("
                "  SELECT MAX(x.id) FROM analyses x WHERE x.item_id=i.id AND x.stage='coarse') "
                "WHERE a.stage='deep' AND a.status='ok' AND i.batch_id=? "
                "ORDER BY i.id DESC LIMIT ?",
                (batch_id, limit)).fetchall()
            out = []
            for r in rows:
                card = {}
                try:
                    card = (json.loads(r["payload_json"] or "{}") or {}).get("card") or {}
                except (ValueError, TypeError):
                    card = {}
                coarse = {}
                try:
                    coarse = json.loads(r["coarse_json"] or "{}") or {}
                except (ValueError, TypeError):
                    coarse = {}
                anchor = card.get("anchor") or {}
                na = card.get("next_action") or {}
                mh = card.get("money_hypothesis") or {}
                out.append({
                    "id": r["id"], "title": r["title"], "url": r["url"],
                    "source": r["source"], "published_at": r["published_at"],
                    "fact": card.get("fact"),
                    "stage": card.get("stage"),
                    "vertical": anchor.get("vertical"),
                    "country": anchor.get("country"),
                    "category": anchor.get("category"),
                    "confidence": card.get("confidence"),
                    "potential": coarse.get("potential"),
                    "opportunity_type": coarse.get("opportunity_type"),
                    "next_action": na,
                    "money_form": mh.get("form"),
                    "feasibility": card.get("feasibility"),
                })
            return {"batch_id": batch_id, "leads": out, "count": len(out)}
        finally:
            conn.close()

    @app.get("/api/candidates")
    def candidate_list(limit: int = 50, offset: int = 0, q: str = ""):
        """候选池：超出深加工限额的粗筛合格条目（最多 200），支持分页/搜索。"""
        return candidates_mod.list_items(limit=limit, offset=offset, q=q)

    @app.post("/api/candidates/{item_id}/promote")
    def promote_candidate(item_id: int):
        """手动提级：对候选池条目跑一次深加工；成功/拒绝/失败都移出池并同步批次账。"""
        from .. import scheduler as sched
        from .. import budget
        from ..pipeline import deep_one
        from .. import settings_store

        conn = db.get_conn()
        try:
            c = candidates_mod.get(conn, item_id)
            if c is None:
                return JSONResponse(status_code=404, content={
                    "ok": False, "error": "该条不在候选池（可能已被提级或淘汰）"})
            item = conn.execute(
                "SELECT i.id, i.title, i.summary, i.published_at, i.url, "
                "s.name AS source_name FROM items i LEFT JOIN sources s "
                "ON s.id=i.source_id WHERE i.id=?", (item_id,)).fetchone()
            coarse_row = conn.execute(
                "SELECT payload_json FROM analyses WHERE item_id=? AND stage='coarse' "
                "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        finally:
            conn.close()
        if item is None:
            return JSONResponse(status_code=404, content={
                "ok": False, "error": "条目不存在"})
        if sched.STATE.get("running"):
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "本轮采集中，请稍后再提级"})
        bs = budget.status()
        if bs["mode"] != "ok":
            mode_cn = {"hard": "硬停", "soft": "软停"}.get(bs["mode"], bs["mode"])
            return JSONResponse(status_code=400, content={
                "ok": False, "error": f"预算{mode_cn}，暂不能提级；请调整预算后再试"})
        try:
            coarse = json.loads(coarse_row["payload_json"] or "{}") if coarse_row else {}
        except (ValueError, TypeError):
            coarse = {}
        if not isinstance(coarse, dict) or not coarse:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "该条缺少粗筛结果，无法提级"})
        cfg = settings_store.load()
        try:
            res = deep_one(item, coarse, c["batch_id"], cfg["deep_model"])
        except Exception as exc:
            return JSONResponse(status_code=500, content={
                "ok": False, "error": f"提级失败：{type(exc).__name__}: {str(exc)[:160]}"})
        conn = db.get_conn()
        try:
            payload = {"card": res["card"], "reasons": res["reasons"],
                       "manual": True, "promoted_from": "candidate_pool"}
            conn.execute(
                "INSERT INTO analyses(item_id, stage, model, payload_json, status, cost, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (item_id, "deep", cfg["deep_model"],
                 json.dumps(payload, ensure_ascii=False), res["status"],
                 res["cost"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            candidates_mod.remove(conn, item_id)
            _adjust_batch_stats(conn, c["batch_id"], None, res["status"],
                                res["cost"], [], res["reasons"],
                                skipped_delta=-1, candidates_delta=1, pool_delta=-1,
                                manual_delta=1)
            conn.commit()
        finally:
            conn.close()
        return {"ok": res["status"] == "ok", "status": res["status"],
                "card": res["card"], "reasons": res["reasons"],
                "cost": res["cost"], "model": cfg["deep_model"]}

    @app.get("/api/sources/socks5")
    def get_socks5_preset():
        """境外 SOCKS5 预设：从 Google×5 现有代理反读 host/port/user。
        永不返回密码；password_set 只表示已保存过密码。"""
        conn = db.get_conn()
        try:
            marks = ",".join("?" * len(SOCKS5_SOURCE_NAMES))
            rows = conn.execute(
                f"SELECT name, proxy_url FROM sources WHERE name IN ({marks})",
                SOCKS5_SOURCE_NAMES).fetchall()
        finally:
            conn.close()
        row = next((r for r in rows if r["proxy_url"]), None)
        if row is None:
            return {"configured": False, "host": "", "port": "",
                    "username": "", "password_set": False,
                    "masked": "", "source_ids": list(SOCKS5_SOURCE_IDS)}
        try:
            u = urlsplit(row["proxy_url"])
        except ValueError:
            return {"configured": False, "host": "", "port": "",
                    "username": "", "password_set": False,
                    "masked": "", "source_ids": list(SOCKS5_SOURCE_IDS)}
        return {"configured": True,
                "host": u.hostname or "", "port": u.port or "",
                "username": unquote(u.username) if u.username else "",
                "password_set": u.password is not None,
                "masked": _mask_proxy_url(row["proxy_url"]),
                "source_ids": list(SOCKS5_SOURCE_IDS)}

    @app.post("/api/sources/socks5")
    def apply_socks5_preset(payload: dict = Body(default={})):
        """把一组 SOCKS5 字段应用到 Google×5；密码留空时复用已保存密码，
        绝不在响应中返回密码。"""
        host = str(payload.get("host") or "").strip()
        port_raw = str(payload.get("port") or "").strip()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not host or "://" in host or "/" in host or " " in host or "@" in host:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "SOCKS5 地址填主机名或 IP（不要带 socks5://、端口或路径）"})
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "端口必须是 1–65535 的数字"})
        if not (1 <= port <= 65535):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "端口必须在 1–65535 之间"})
        if password and not username:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "填了密码就必须填用户名；无认证代理请把用户名和密码都留空"})

        conn = db.get_conn()
        try:
            marks = ",".join("?" * len(SOCKS5_SOURCE_NAMES))
            rows = conn.execute(
                f"SELECT name, proxy_url FROM sources WHERE name IN ({marks})",
                SOCKS5_SOURCE_NAMES).fetchall()
            existing = next((r["proxy_url"] for r in rows if r["proxy_url"]), None)
            existing_pw = ""
            if existing:
                try:
                    existing_pw = unquote(urlsplit(existing).password or "")
                except ValueError:
                    existing_pw = ""
            if username and not password and existing_pw:
                password = existing_pw
            if username and not password:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": "该 SOCKS5 需要密码：请填写密码后保存"})
            userinfo = ""
            if username:
                userinfo = quote(username, safe="") + ":" + quote(password, safe="") + "@"
            proxy_url = f"socks5://{userinfo}{host}:{port}"
            n = 0
            for r in rows:
                cur = conn.execute(
                    "UPDATE sources SET proxy_mode='proxy', proxy_url=? WHERE name=?",
                    (proxy_url, r["name"]))
                n += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if n == 0:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "没有找到 Google 源，未应用"})
        return {"ok": True, "count": n, "masked": _mask_proxy_url(proxy_url),
                "source_ids": list(SOCKS5_SOURCE_IDS)}

    @app.put("/api/sources/{source_id}")
    def update_source(source_id: str, body: dict = Body(...)):
        """读写源的 enabled / proxy_mode / proxy_url。proxy_mode 必须是 direct|proxy。"""
        spec = get_spec(source_id)
        if not spec:
            return {"ok": False, "error": f"未知源：{source_id}"}
        enabled = body.get("enabled")
        proxy_mode = body.get("proxy_mode")
        proxy_url = body.get("proxy_url", "")

        if enabled is not None and enabled not in (True, False, 0, 1):
            return {"ok": False, "error": "enabled 必须是 true/false"}
        if proxy_mode is not None and proxy_mode not in PROXY_MODES:
            return {"ok": False, "error": "proxy_mode 必须是 direct 或 proxy"}

        # 代理地址：允许 socks:// 别名；含 *** 的值是脱敏显示，视为“不修改”
        u = str(proxy_url or "").strip()
        if u.startswith("socks://"):
            u = "socks5://" + u[len("socks://"):]
        masked = "***" in u
        if proxy_mode == "proxy" and u and not masked:
            if not (u.startswith("http://") or u.startswith("https://")
                    or u.startswith("socks5://") or u.startswith("socks5h://")) or " " in u:
                return JSONResponse(status_code=400, content={
                    "ok": False,
                    "error": "代理地址必须以 http://、https://、socks5:// 或 socks5h:// 开头，"
                             "例如 http://127.0.0.1:7897 或 socks5://127.0.0.1:1080"})

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT id, proxy_url FROM sources WHERE name=?", (spec.name,)).fetchone()
            if row is None:
                return {"ok": False, "error": f"源未初始化：{spec.name}"}
            if proxy_mode == "proxy" and not u and not row["proxy_url"]:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": "选择代理模式时必须填写代理地址（如 socks5://127.0.0.1:1080）"})
            sets, vals = [], []
            if enabled is not None:
                sets.append("enabled=?")
                vals.append(1 if enabled in (True, 1) else 0)
            if proxy_mode is not None:
                sets.append("proxy_mode=?")
                vals.append(proxy_mode)
            if proxy_mode == "proxy" and u and not masked:
                sets.append("proxy_url=?")
                vals.append(u)
            if proxy_mode == "direct":
                sets.append("proxy_url=NULL")
            if sets:
                conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?",
                             vals + [row["id"]])
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    # ---------------- T13：按需 AI 解析 + 建议清单 ----------------
    @app.get("/api/suggestions")
    def suggestions(limit: int = 8):
        """AI 建议解析的热点：最新批次里粗筛通过、但还没深加工的条目（按潜力分排序）。"""
        limit = max(1, min(int(limit), 30))
        conn = db.get_conn()
        try:
            b = conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
            if b is None:
                return {"batch_id": None, "items": []}
            rows = conn.execute(
                "SELECT i.id, i.title, i.url, i.published_at, s.name AS source, "
                "a.payload_json FROM items i "
                "LEFT JOIN sources s ON s.id=i.source_id "
                "JOIN analyses a ON a.item_id=i.id AND a.stage='coarse' AND a.status='ok' "
                "LEFT JOIN analyses d ON d.item_id=i.id AND d.stage='deep' "
                "WHERE i.batch_id=? AND i.is_duplicate=0 AND d.id IS NULL "
                "ORDER BY i.id DESC LIMIT 200", (b["id"],)).fetchall()
            out = []
            for r in rows:
                try:
                    p = json.loads(r["payload_json"] or "{}")
                except (ValueError, TypeError):
                    p = {}
                anchors = p.get("anchors") or {}
                out.append({
                    "id": r["id"], "title": r["title"], "url": r["url"],
                    "published_at": r["published_at"], "source": r["source"],
                    "potential": p.get("potential", 0),
                    "country": anchors.get("country") or "",
                    "category": anchors.get("category") or "",
                })
            out.sort(key=lambda x: -(x["potential"] or 0))
            return {"batch_id": b["id"], "items": out[:limit]}
        finally:
            conn.close()

    @app.post("/api/item/{item_id}/analyze")
    def analyze_item(item_id: int):
        """对单条按需做 AI 解析（同步，约 5–10 秒）；已解析过则直接返回缓存。"""
        from ..pipeline import analyze_one
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return JSONResponse(status_code=404, content={"ok": False, "error": "条目不存在"})
            cached = conn.execute(
                "SELECT payload_json, status FROM analyses WHERE item_id=? AND stage='deep' "
                "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        finally:
            conn.close()
        if cached is not None:
            try:
                data = json.loads(cached["payload_json"] or "{}")
            except (ValueError, TypeError):
                data = {}
            if data.get("card"):
                return {"ok": True, "cached": True, "status": cached["status"],
                        "card": data.get("card"), "reasons": data.get("reasons") or []}
        try:
            res = analyze_one(item_id)
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"ok": False, "error": f"解析失败：{type(exc).__name__}: {exc}"})
        return {"ok": True, "cached": False, "status": res["status"],
                "card": res.get("card"), "reasons": res.get("reasons") or []}

    # ---------------- T9：业务设置 ----------------
    @app.get("/api/settings")
    def get_settings():
        """读取界面可改的业务配置（模型/限额/预算/间隔）。"""
        from .. import settings_store
        return {"settings": settings_store.load(),
                "defaults": settings_store.DEFAULTS}

    @app.get("/api/settings/model-log")
    def model_switch_log(limit: int = 10):
        """最近模型切换记录（F-011）：只含时间/字段/旧→新，不含任何密钥。"""
        from .. import settings_store
        return {"items": settings_store.read_log(max(1, min(int(limit), 50)))}

    @app.put("/api/settings")
    def put_settings(payload: dict = Body(default={})):
        """保存业务设置；校验失败返回 400 + 中文原因。间隔类改动需重启生效。"""
        from .. import settings_store
        ok, data = settings_store.save(payload or {})
        if not ok:
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": data})
        return {"ok": True, "settings": data,
                "note": "模型与限额下一轮生效；轮次间隔需重启服务后生效"}

    # ---------------- 平台信号（热搜 / 榜单类原始条目） ----------------
    PLATFORM_SOURCE_NAMES = ("微博热搜", "抖音热点榜", "百度热搜")

    @app.get("/api/platform_items")
    def platform_items(per_source: int = 12):
        """最近一批里的平台侧条目：热搜在前、榜单在后；每个源最多 per_source 条。"""
        import re as _re
        per_source = max(1, min(int(per_source), 30))
        conn = db.get_conn()
        try:
            batch = conn.execute("SELECT MAX(id) b FROM batches").fetchone()["b"]
            if batch is None:
                return {"batch_id": None, "items": [], "count": 0}
            rows = conn.execute(
                "SELECT i.id, i.title, i.summary, i.url, i.published_at, i.is_duplicate, "
                "       s.name AS source FROM items i JOIN sources s ON s.id=i.source_id "
                "WHERE i.batch_id=? AND (s.name IN (?,?,?) OR s.name LIKE 'Amazon%') "
                "ORDER BY i.id", (batch, *PLATFORM_SOURCE_NAMES)).fetchall()
        finally:
            conn.close()

        JUNK = ("click for details", "shop now", "sponsored")
        groups, order = {}, []
        for r in rows:
            d = dict(r)
            title = (d.get("title") or "").strip()
            if not title or len(title) < 6 or title.lower() in JUNK:
                continue
            summary = d.get("summary") or ""
            m = _re.search(r"第\s*(\d+)\s*[位名]", summary)
            h = _re.search(r"热度\s*([0-9.]+)", summary)
            d["rank"] = int(m.group(1)) if m else None
            if d["rank"] == 0:
                d["rank"] = None          # 微博接口 rank 从 0 开始，0 视为无排名
            d["heat"] = h.group(1) if h else None
            d.pop("summary", None)
            src = d["source"]
            if src not in groups:
                groups[src] = {"fresh": [], "dup": []}
                order.append(src)
            bucket = groups[src]["dup"] if d.get("is_duplicate") else groups[src]["fresh"]
            if len(bucket) < per_source:
                bucket.append(d)

        # 热搜优先（微博/抖音/百度），其次 Amazon 榜单
        def prio(name):
            return (0, PLATFORM_SOURCE_NAMES.index(name)) if name in PLATFORM_SOURCE_NAMES else (1, name)
        order.sort(key=prio)
        items = []
        for src in order:
            g = groups[src]
            items.extend((g["fresh"] + g["dup"])[:per_source])
        return {"batch_id": batch, "items": items, "count": len(items),
                "by_source": {src: len((groups[src]["fresh"] + groups[src]["dup"])[:per_source])
                              for src in order}}

    # ---------------- 出口网络 & 采集模式切换 ----------------
    DOMESTIC_DIRECT = {"wscn-global", "wscn-a-stock", "wscn-forex", "sina", "weibo-hot"}
    FOREIGN_DIRECT = {"gnews-kuajing", "gnews-xuanpin", "gnews-guanshui",
                      "gnews-platform", "gnews-warehouse"}

    @app.get("/api/network")
    def network_info(refresh: int = 0):
        """当前出口 IP 与地区（缓存 10 分钟，refresh=1 强制刷新）。"""
        from .. import network as net
        return net.current(force=bool(refresh))

    @app.post("/api/sources/region-mode")
    def region_mode(payload: dict = Body(default={})):
        """一键切换采集模式：cn=境内（国内源直连、国外源代理）/ intl=境外（相反）。"""
        mode = (payload.get("mode") or "cn").lower()
        proxy_url = (payload.get("proxy_url") or "").strip()
        from .. import settings_store
        if not proxy_url:
            proxy_url = settings_store.get("proxy_url_default") or "http://127.0.0.1:7897"
        from ..sources.registry import SOURCES
        name_of = {x.id: x.name for x in SOURCES}
        conn = db.get_conn()
        changed = []
        try:
            for sid in sorted(DOMESTIC_DIRECT | FOREIGN_DIRECT):
                name = name_of.get(sid)
                if not name:
                    continue
                is_domestic = sid in DOMESTIC_DIRECT
                want_proxy = (mode == "intl") if is_domestic else (mode == "cn")
                conn.execute("UPDATE sources SET proxy_mode=?, proxy_url=? WHERE name=?",
                             ("proxy" if want_proxy else "direct",
                              proxy_url if want_proxy else "", name))
                changed.append({"id": sid, "name": name, "proxy": want_proxy})
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "mode": mode, "proxy_url": _mask_proxy_url(proxy_url),
                "changed": changed,
                "note": "只影响直连型/桥接型源；Amazon 榜单跟随系统网络"}

    @app.post("/api/sources/set-proxy")
    def set_proxy(payload: dict = Body(default={})):
        """把指定源切到代理（用于体检后一键修复失败的直连源）。"""
        ids = payload.get("ids") or []
        proxy_url = (payload.get("proxy_url") or "").strip()
        from .. import settings_store
        if not proxy_url:
            proxy_url = settings_store.get("proxy_url_default") or "http://127.0.0.1:7897"
        if not ids:
            return {"ok": False, "error": "没有指定要切换的源"}
        from ..sources.registry import SOURCES
        name_of = {x.id: x.name for x in SOURCES}
        conn = db.get_conn()
        n = 0
        try:
            for sid in ids:
                name = name_of.get(sid)
                if not name:
                    continue
                cur = conn.execute("UPDATE sources SET proxy_mode='proxy', proxy_url=? WHERE name=?",
                                   (proxy_url, name))
                n += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "count": n, "proxy_url": _mask_proxy_url(proxy_url)}

    # ---------------- 源体检（异地/换网络后一键检查） ----------------
    @app.post("/api/sources/check")
    def sources_check():
        """并发探测每个已启用源能否采到数据（不调用模型、不写 items）。"""
        import time as _t
        from concurrent.futures import ThreadPoolExecutor
        from ..sources.collect import _build_source
        from ..sources.registry import SOURCES

        conn = db.get_conn()
        try:
            rows = {r["name"]: dict(r) for r in conn.execute(
                "SELECT name, enabled, proxy_mode, proxy_url FROM sources")}
        finally:
            conn.close()

        def one(spec):
            row = rows.get(spec.name) or {}
            if not row.get("enabled", 1):
                return {"id": spec.id, "name": spec.name, "skipped": True}
            t0 = _t.time()
            try:
                src = _build_source(spec, row.get("proxy_mode") or "direct",
                                    row.get("proxy_url") or "")
                items = src.fetch()
                return {"id": spec.id, "name": spec.name, "ok": True,
                        "count": len(items), "ms": int((_t.time() - t0) * 1000)}
            except Exception as exc:
                return {"id": spec.id, "name": spec.name, "ok": False, "count": 0,
                        "error": f"{type(exc).__name__}: {exc}"[:220],
                        "ms": int((_t.time() - t0) * 1000)}

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(one, SOURCES))

        # 回写状态，让大屏与设置页同步显示
        conn = db.get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in results:
                if r.get("skipped"):
                    continue
                if r.get("ok"):
                    conn.execute("UPDATE sources SET last_ok_at=?, last_error=NULL, last_count=? WHERE name=?",
                                 (now, r["count"], r["name"]))
                else:
                    conn.execute("UPDATE sources SET last_error=? WHERE name=?",
                                 (r.get("error") or "失败", r["name"]))
            conn.commit()
        finally:
            conn.close()

        ok_n = sum(1 for r in results if r.get("ok"))
        return {"ok": True, "checked_at": now, "ok_count": ok_n,
                "total": sum(1 for r in results if not r.get("skipped")), "results": results}

    # ---------------- T12：手动清理 ----------------
    @app.post("/api/cleanup")
    def cleanup_now(days: int = Body(default=0, embed=True)):
        """立即清理过期数据。days>0 时临时覆盖保留天数（不写回配置）。"""
        from .. import cleanup as cleanup_mod
        try:
            if days and int(days) > 0:
                import sqlite3, datetime
                from .. import db
                conn = db.get_conn()
                try:
                    cutoff_items = (datetime.datetime.now() - datetime.timedelta(days=int(days))
                                    ).strftime("%Y-%m-%d %H:%M:%S")
                    old = [r["id"] for r in conn.execute(
                        "SELECT i.id FROM items i LEFT JOIN batches b ON b.id=i.batch_id "
                        "WHERE COALESCE(i.published_at, b.started_at) < ?", (cutoff_items,)).fetchall()]
                    n = {"items": 0, "analyses": 0, "notes": 0, "favorites": 0, "llm_calls": 0}
                    if old:
                        marks = ",".join("?" * len(old))
                        n["favorites"] = conn.execute(f"DELETE FROM favorites WHERE item_id IN ({marks})", old).rowcount
                        n["notes"] = conn.execute(f"DELETE FROM notes WHERE item_id IN ({marks})", old).rowcount
                        n["analyses"] = conn.execute(f"DELETE FROM analyses WHERE item_id IN ({marks})", old).rowcount
                        n["items"] = conn.execute(f"DELETE FROM items WHERE id IN ({marks})", old).rowcount
                    conn.commit()
                    res = n
                finally:
                    conn.close()
            else:
                res = cleanup_mod.run()
            return {"ok": True, "deleted": res, "days": int(days) or None}
        except Exception as exc:
            return JSONResponse(status_code=500,
                                content={"ok": False, "error": f"清理失败：{type(exc).__name__}: {exc}"})

    # ---------------- T8：详情 / 备忘录 / 收藏 ----------------
    def _deep_of(conn, item_id: int) -> dict:
        """取该条目最新一条深加工记录，返回 dict：
        card/reasons/status/model/cost/analyzed_at。
        没有深加工记录时 status/model/analyzed_at 为 None、cost 为 0。"""
        row = conn.execute(
            "SELECT payload_json, status, model, cost, created_at FROM analyses "
            "WHERE item_id=? AND stage='deep' ORDER BY id DESC LIMIT 1",
            (item_id,)).fetchone()
        if row is None:
            return {"card": None, "reasons": [], "status": None,
                    "model": None, "cost": 0.0, "analyzed_at": None}
        try:
            data = json.loads(row["payload_json"] or "{}")
        except (ValueError, TypeError):
            data = {}
        return {"card": data.get("card"), "reasons": data.get("reasons") or [],
                "status": row["status"], "model": row["model"],
                "cost": float(row["cost"] or 0.0), "analyzed_at": row["created_at"]}

    @app.get("/api/item/{item_id}")
    def item_detail(item_id: int):
        """条目详情：原文 + 深加工卡 + 加工模型/花费/时间 + 备注 + 收藏态。
        can_reanalyze：deep 状态为 rejected/failed 或从未深加工时 True，ok 时 False。"""
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT i.id, i.title, i.url, i.summary, i.published_at, i.region, "
                "s.name AS source FROM items i LEFT JOIN sources s ON s.id=i.source_id "
                "WHERE i.id=?", (item_id,)).fetchone()
            if row is None:
                return JSONResponse(status_code=404,
                                    content={"error": "条目不存在"})
            deep = _deep_of(conn, item_id)
            coarse_row = conn.execute(
                "SELECT payload_json FROM analyses WHERE item_id=? AND stage='coarse' "
                "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
            coarse = {}
            if coarse_row is not None and coarse_row["payload_json"]:
                try:
                    coarse = json.loads(coarse_row["payload_json"]) or {}
                except (ValueError, TypeError):
                    coarse = {}
            note = conn.execute(
                "SELECT content, updated_at FROM notes WHERE item_id=? "
                "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
            fav = conn.execute(
                "SELECT 1 FROM favorites WHERE item_id=?", (item_id,)).fetchone()
            return {
                "item": {"id": row["id"], "title": row["title"], "url": row["url"],
                         "summary": row["summary"], "published_at": row["published_at"],
                         "region": row["region"], "source": row["source"]},
                "card": deep["card"],
                "reasons": deep["reasons"],
                "deep_status": deep["status"],
                "model": deep["model"],
                "cost": deep["cost"],
                "analyzed_at": deep["analyzed_at"],
                "can_reanalyze": deep["status"] != "ok",
                "opportunity_type": coarse.get("opportunity_type"),
                "note": note["content"] if note else None,
                "note_updated_at": note["updated_at"] if note else None,
                "favorite": bool(fav),
            }
        finally:
            conn.close()

    @app.post("/api/item/{item_id}/reanalyze")
    def reanalyze_item(item_id: int):
        """强制重新深加工一条（F-006 异常 3：校验不通过可人工重试）。
        不复用缓存卡：直接走 pipeline.analyze_one，立即写 llm_calls 并新增 analyses。
        拒绝路径：条目不存在→404；本轮采集中→409；预算熔断→400；均为中文错误。"""
        from .. import scheduler as sched
        from .. import budget
        from ..pipeline import analyze_one

        conn = db.get_conn()
        try:
            row = conn.execute("SELECT batch_id FROM items WHERE id=?",
                               (item_id,)).fetchone()
            if row is None:
                return JSONResponse(status_code=404, content={
                    "ok": False, "error": "条目不存在，请返回大屏确认条目 ID"})
            batch_id = row["batch_id"]
            old = _deep_of(conn, item_id)
            if old["status"] == "ok":
                # 服务端闸：已通过校验的卡不允许重烧钱（前端不显示按钮，这里防直连 API）
                return JSONResponse(status_code=409, content={
                    "ok": False,
                    "error": "该条已通过校验，无需重新加工；"
                             "如确需重算请使用 AI 解析或联系管理员"})
        finally:
            conn.close()

        if sched.STATE.get("running"):
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "本轮采集中，请稍后再试"})

        bs = budget.status()
        if bs["mode"] != "ok":
            mode_cn = {"hard": "硬停", "soft": "软停"}.get(bs["mode"], bs["mode"])
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": f"预算{mode_cn}（本月剩余 ¥{bs['remaining']:.2f} / "
                         f"预算 ¥{bs['budget']:.2f}），已暂停重新加工；"
                         f"请在大屏设置里提高月预算，或等待下月预算重置后再试"})

        try:
            analyze_one(item_id)
        except ValueError as exc:
            # analyze_one 内部的预算复检 / 条目不存在等中文原因
            return JSONResponse(status_code=400, content={
                "ok": False, "error": f"无法重新加工：{exc}"})
        except Exception as exc:
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": f"重新加工失败：{type(exc).__name__}: {str(exc)[:200]}；"
                         f"请检查网络与模型服务后重试，持续失败请查看 data/logs/app.log"})

        conn = db.get_conn()
        try:
            deep = _deep_of(conn, item_id)
        finally:
            conn.close()

        # 批次账同步：本次新调用已在 llm_calls 记了 deep["cost"]，
        # stats.cost 必须增加「本次新 cost」才能与 SUM(llm_calls) 对账一致；
        # 计数按旧→新状态增减一次。stats 更新失败不影响已完成的加工结果。
        stats_updated = True
        warning = None
        conn = db.get_conn()
        try:
            stats_updated = _adjust_batch_stats(
                conn, batch_id, old["status"], deep["status"],
                deep["cost"], old["reasons"], deep["reasons"])
            if not stats_updated:
                warning = ("批次统计未能自动更新（大屏计数可能暂时不准）；"
                           "加工结果本身已成功，请稍后重试或联系管理员")
                print(f"[reanalyze] 条目 {item_id} 批次 {batch_id} 统计更新未生效", flush=True)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            stats_updated = False
            warning = (f"批次统计更新失败：{type(exc).__name__}: {str(exc)[:160]}；"
                       f"加工结果本身已成功，请稍后重试或在日志 data/logs/app.log 查看原因")
            print(f"[reanalyze] 条目 {item_id} 统计更新异常：{exc}", flush=True)
        finally:
            conn.close()

        result = {"ok": True, "status": deep["status"], "card": deep["card"],
                  "reasons": deep["reasons"], "model": deep["model"],
                  "cost": deep["cost"], "analyzed_at": deep["analyzed_at"],
                  "stats_updated": stats_updated}
        if warning:
            result["warning"] = warning
        return result

    @app.get("/api/search/status")
    def search_status():
        """F-005 深挖搜索配置状态。只回布尔与供应商名，不回 Key/代理地址。"""
        return {"configured": search_mod.configured(),
                "provider": (settings.SEARCH_PROVIDER or "tavily"),
                "proxy_set": bool(search_mod._proxy_url())}

    @app.post("/api/item/{item_id}/deepdive")
    def deepdive_item(item_id: int):
        """F-005 深挖：手工触发二轮检索 + 模型加工。
        拒绝路径：条目不存在→404；调度闸/本轮采集中→409；预算熔断→400；
        未配置搜索 Key→400；无线索卡等加工前置不满足→400；均为中文。"""
        from .. import deepdive
        from .. import scheduler as sched
        from .. import budget

        conn = db.get_conn()
        try:
            row = conn.execute("SELECT 1 FROM items WHERE id=?",
                               (item_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return JSONResponse(status_code=404, content={
                "ok": False, "error": "条目不存在，请返回大屏确认条目 ID"})

        if not search_mod.configured():
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "未配置搜索 API Key（SEARCH_API_KEY）。"
                         "请把 Tavily Key 写入项目根目录 .env，并重启服务后再深挖"})

        if sched.STATE.get("running"):
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "本轮采集中，请稍后再试"})

        bs = budget.status()
        if bs["mode"] != "ok":
            mode_cn = {"hard": "硬停", "soft": "软停"}.get(bs["mode"], bs["mode"])
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": f"预算{mode_cn}（本月剩余 ¥{bs['remaining']:.2f} / "
                         f"预算 ¥{bs['budget']:.2f}），已暂停深挖；"
                         f"请在大屏设置里提高月预算，或等待下月预算重置后再试"})

        try:
            return deepdive.run(item_id)
        except ValueError as exc:
            # 无卡 / 检索无结果 / 预算复检等中文原因
            return JSONResponse(status_code=400, content={
                "ok": False, "error": f"无法深挖：{exc}"})
        except Exception as exc:
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": f"深挖失败：{type(exc).__name__}: {str(exc)[:200]}；"
                         f"请检查搜索 API/网络后重试，持续失败请查看 data/logs/app.log"})

    @app.get("/api/item/{item_id}/deepdive")
    def deepdive_get(item_id: int):
        """最新一条深挖记录（payload + model/cost/analyzed_at/status）。
        没有记录返回 {data: null}。"""
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT payload_json, status, model, cost, created_at FROM analyses "
                "WHERE item_id=? AND stage='deepdive' ORDER BY id DESC LIMIT 1",
                (item_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"data": None}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        return {"data": payload.get("data"),
                "reasons": payload.get("reasons") or [],
                "search_queries": payload.get("search_queries") or [],
                "search_count": payload.get("search_count") or 0,
                "sources": payload.get("sources") or [],
                "status": row["status"], "model": row["model"],
                "cost": float(row["cost"] or 0.0),
                "analyzed_at": row["created_at"]}

    @app.post("/api/item/{item_id}/note")
    def save_note(item_id: int, content: str = Body(default="", embed=True)):
        """写/更新备注（同一条目只保留一条）。空内容 = 删除备注。"""
        text = (content or "").strip()
        conn = db.get_conn()
        try:
            exists = conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
            if exists is None:
                return JSONResponse(status_code=404, content={"ok": False, "error": "条目不存在"})
            if not text:
                conn.execute("DELETE FROM notes WHERE item_id=?", (item_id,))
                conn.commit()
                return {"ok": True, "deleted": True}
            cur = conn.execute(
                "UPDATE notes SET content=?, updated_at=datetime('now','localtime') "
                "WHERE item_id=?", (text, item_id))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO notes(item_id, content, updated_at) "
                    "VALUES(?,?,datetime('now','localtime'))", (item_id, text))
            conn.commit()
            return {"ok": True, "saved_at": conn.execute(
                "SELECT updated_at FROM notes WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,)).fetchone()["updated_at"]}
        finally:
            conn.close()

    @app.post("/api/item/{item_id}/favorite")
    def toggle_favorite(item_id: int, favorite: bool = Body(default=True, embed=True)):
        """收藏 / 取消收藏。"""
        conn = db.get_conn()
        try:
            exists = conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
            if exists is None:
                return JSONResponse(status_code=404, content={"ok": False, "error": "条目不存在"})
            if favorite:
                conn.execute(
                    "INSERT OR IGNORE INTO favorites(item_id, created_at) "
                    "VALUES(?,datetime('now','localtime'))", (item_id,))
            else:
                conn.execute("DELETE FROM favorites WHERE item_id=?", (item_id,))
            conn.commit()
            return {"ok": True, "favorite": bool(favorite)}
        finally:
            conn.close()

    @app.get("/api/notes")
    def list_notes(q: str = "", limit: int = 100, stage: str = "",
                   vertical: str = "", opportunity_type: str = ""):
        """备注列表：q 匹配备注内容或标题；stage/vertical 按最新深加工卡；
        opportunity_type 按最新粗筛结果的 F-004 六类筛选（空=全部）。
        每条返回 stage/vertical/opportunity_type。非法值 400 中文错误。"""
        from ..validate import VERTICALS
        from ..opportunity import ALLOWED as OT_ALLOWED
        if stage and stage not in ("S1", "S2", "S3", "S4"):
            return JSONResponse(status_code=400, content={
                "error": f"非法的阶段值「{stage}」：只支持 S1/S2/S3/S4 或留空（全部）"})
        if vertical and vertical not in VERTICALS:
            return JSONResponse(status_code=400, content={
                "error": "非法的行业值：" + "、".join(VERTICALS) + " 之一，或留空（全部）"})
        if opportunity_type and opportunity_type not in OT_ALLOWED:
            return JSONResponse(status_code=400, content={
                "error": "非法的机会类型：" + "、".join(OT_ALLOWED) + " 之一，或留空（全部）"})
        limit = max(1, min(int(limit), 500))
        stage_expr = ("(SELECT json_extract(a.payload_json, '$.card.stage') FROM analyses a "
                      "WHERE a.item_id=n.item_id AND a.stage='deep' "
                      "ORDER BY a.id DESC LIMIT 1)")
        vert_expr = ("(SELECT json_extract(a.payload_json, '$.card.anchor.vertical') "
                     "FROM analyses a WHERE a.item_id=n.item_id AND a.stage='deep' "
                     "ORDER BY a.id DESC LIMIT 1)")
        type_expr = ("(SELECT json_extract(a.payload_json, '$.opportunity_type') "
                     "FROM analyses a WHERE a.item_id=n.item_id AND a.stage='coarse' "
                     "ORDER BY a.id DESC LIMIT 1)")
        conn = db.get_conn()
        try:
            sql = ("SELECT n.item_id, n.content, n.updated_at, i.title, i.url, "
                   "i.published_at, s.name AS source, "
                   "EXISTS(SELECT 1 FROM favorites f WHERE f.item_id=n.item_id) AS favorite, "
                   f"{stage_expr} AS stage, {vert_expr} AS vertical, "
                   f"{type_expr} AS opportunity_type "
                   "FROM notes n JOIN items i ON i.id=n.item_id "
                   "LEFT JOIN sources s ON s.id=i.source_id ")
            conds, args = [], []
            if q:
                conds.append("(n.content LIKE ? OR i.title LIKE ?)")
                args += [f"%{q}%", f"%{q}%"]
            if stage:
                conds.append(f"stage = ?")
                args.append(stage)
            if vertical:
                conds.append("vertical = ?")
                args.append(vertical)
            if opportunity_type:
                conds.append("opportunity_type = ?")
                args.append(opportunity_type)
            if conds:
                sql += "WHERE " + " AND ".join(conds) + " "
            sql += "ORDER BY n.updated_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            return {"notes": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/favorites")
    def list_favorites(limit: int = 100, q: str = "", opportunity_type: str = ""):
        """收藏列表（按收藏时间倒序）；q 按标题或最新备注模糊匹配；
        opportunity_type 按最新粗筛六类筛选。每条返回 note/opportunity_type。"""
        from ..opportunity import ALLOWED as OT_ALLOWED
        if opportunity_type and opportunity_type not in OT_ALLOWED:
            return JSONResponse(status_code=400, content={
                "error": "非法的机会类型：" + "、".join(OT_ALLOWED) + " 之一，或留空（全部）"})
        limit = max(1, min(int(limit), 500))
        note_expr = ("(SELECT n.content FROM notes n WHERE n.item_id=f.item_id "
                     "ORDER BY n.id DESC LIMIT 1)")
        type_expr = ("(SELECT json_extract(a.payload_json, '$.opportunity_type') "
                     "FROM analyses a WHERE a.item_id=f.item_id AND a.stage='coarse' "
                     "ORDER BY a.id DESC LIMIT 1)")
        conn = db.get_conn()
        try:
            sql = ("SELECT f.item_id, f.created_at, i.title, i.url, i.published_at, "
                   f"s.name AS source, {note_expr} AS note, "
                   f"{type_expr} AS opportunity_type "
                   "FROM favorites f JOIN items i ON i.id=f.item_id "
                   "LEFT JOIN sources s ON s.id=i.source_id ")
            args = []
            conds = []
            if q:
                conds.append("(i.title LIKE ? OR " + note_expr + " LIKE ?)")
                args += [f"%{q}%", f"%{q}%"]
            if opportunity_type:
                conds.append("opportunity_type = ?")
                args.append(opportunity_type)
            if conds:
                sql += "WHERE " + " AND ".join(conds) + " "
            sql += "ORDER BY f.created_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            return {"favorites": [dict(r) for r in rows]}
        finally:
            conn.close()

    # ---------------- T10：预算 / 成本 / 日志 ----------------
    @app.get("/api/budget")
    def budget_status():
        """预算熔断状态：本月预算/已花费/剩余/比例/模式（ok/soft/hard）。"""
        from .. import budget
        return budget.status()

    @app.get("/api/market")
    def market_quotes():
        """行情指标（F-015）：金价 / 油价 / 汇率。接口失败也返回 200 + 中文原因，
        由前端显示「暂不可用」，不允许让大屏崩掉。"""
        from ..market import get_quotes
        return get_quotes()

    @app.get("/api/cost")
    def cost_summary(days: int = 30):
        """成本分析：最近 days 天按日汇总 + 本月按阶段/模型汇总，本月合计与预算状态。"""
        from .. import budget
        days = max(1, min(int(days), 365))
        month_start = datetime.now().strftime("%Y-%m-01 00:00:00")
        conn = db.get_conn()
        try:
            by_day = [dict(r) for r in conn.execute(
                "SELECT substr(created_at,1,10) AS date, ROUND(SUM(cost),4) AS cost "
                "FROM llm_calls WHERE created_at >= datetime('now','localtime', ?) "
                "GROUP BY date ORDER BY date", (f"-{days} days",)).fetchall()]
            by_stage = [dict(r) for r in conn.execute(
                "SELECT COALESCE(stage,'—') AS stage, COUNT(*) AS calls, "
                "ROUND(SUM(cost),4) AS cost FROM llm_calls "
                "WHERE created_at >= ? GROUP BY stage ORDER BY cost DESC",
                (month_start,)).fetchall()]
            by_model = [dict(r) for r in conn.execute(
                "SELECT COALESCE(model,'—') AS model, COUNT(*) AS calls, "
                "ROUND(SUM(cost),4) AS cost FROM llm_calls "
                "WHERE created_at >= ? GROUP BY model ORDER BY cost DESC",
                (month_start,)).fetchall()]
            total_row = conn.execute(
                "SELECT ROUND(COALESCE(SUM(cost),0),4) c FROM llm_calls").fetchone()
            month_row = conn.execute(
                "SELECT ROUND(COALESCE(SUM(cost),0),4) c FROM llm_calls "
                "WHERE created_at >= ?", (month_start,)).fetchone()
            month_total = month_row["c"]
        finally:
            conn.close()
        return {"days": days, "month_total": month_total,
                "total": total_row["c"],
                "budget": budget.status(),
                "by_day": by_day, "by_stage": by_stage, "by_model": by_model}

    @app.get("/api/logs")
    def logs(limit: int = 50):
        """批次日志摘要：每轮含抓取/去重/粗筛/深加工/花费/耗时/失败源。"""
        limit = max(1, min(int(limit), 200))
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, trigger, status, stats_json "
                "FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                stats = {}
                if r["stats_json"]:
                    try:
                        stats = json.loads(r["stats_json"])
                    except (ValueError, TypeError):
                        stats = {}
                out.append({
                    "id": r["id"], "started_at": r["started_at"],
                    "finished_at": r["finished_at"], "trigger": r["trigger"],
                    "status": r["status"], "stats": stats,
                })
            return {"logs": out, "count": len(out)}
        finally:
            conn.close()

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(settings.static_dir / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    return app
