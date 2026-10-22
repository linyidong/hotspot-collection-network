# -*- coding: utf-8 -*-
"""模块：API 路由。职责：创建 FastAPI 应用，提供 GET /api/health、GET/PUT /api/sources；
    根路径返回 static/index.html，并挂载 /static 静态目录。
    /api/health 携带 bridge.ok（RSS 桥状态，挂了自动重启）；/api/sources 返回 8 源状态。
    关键依赖：fastapi、app.config、app.db、app.bridge、app.sources.registry。"""
import json
import threading
import time
from datetime import datetime

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from ..bridge import bridge
from ..config import settings
from ..sources.registry import SOURCES, get_spec

# 进程启动时刻，用于 uptime_seconds
_START = time.time()

TABLES = ("sources", "batches", "items", "analyses", "notes",
          "favorites", "llm_calls", "config")

PROXY_MODES = ("direct", "proxy")


def _source_status(row) -> str:
    """由数据库状态推断源当前状态：最近失败→error；最近成功→ok；从未→never。"""
    if row["last_error"]:
        return "error"
    if row["last_ok_at"]:
        return "ok"
    return "never"


def _load_sources() -> list:
    """8 源全量（registry 元数据 + 数据库开关/状态）。"""
    conn = db.get_conn()
    try:
        rows = {r["name"]: r for r in conn.execute(
            "SELECT name, url, enabled, proxy_mode, proxy_url, "
            "last_ok_at, last_error, last_count FROM sources").fetchall()}
    finally:
        conn.close()
    out = []
    for spec in SOURCES:
        row = rows.get(spec.name)
        out.append({
            "id": spec.id,
            "name": spec.name,
            "type": spec.type,
            "path": spec.path,
            "channel": spec.channel,
            "url": spec.url,
            "enabled": (row["enabled"] if row else 1) == 1,
            "proxy_mode": row["proxy_mode"] if row else "direct",
            "proxy_url": row["proxy_url"] if row else None,
            "last_ok_at": row["last_ok_at"] if row else None,
            "last_error": row["last_error"] if row else None,
            "last_count": row["last_count"] if row else 0,
            "status": _source_status(row) if row else "never",
        })
    return out


def create_app() -> FastAPI:
    """组装 FastAPI 应用：先建表，再挂路由与静态资源。"""
    app = FastAPI(title="Hotspot Collection Network", version=settings.VERSION, docs_url="/api/docs")
    app.state.start_time = _START

    @app.get("/api/health")
    def health():
        ok, names = db.db_ok()
        uptime = round(time.time() - _START, 1)
        bridge.ensure()
        return {
            "status": "ok",
            "version": settings.VERSION,
            "db": {"ok": ok, "tables": len(names), "path": str(settings.db_path)},
            "db_ok": ok,
            "db_tables": len(names),
            "key_configured": settings.key_configured,
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
                    "next_action": na,
                    "money_form": mh.get("form"),
                    "feasibility": card.get("feasibility"),
                })
            return {"batch_id": batch_id, "leads": out, "count": len(out)}
        finally:
            conn.close()

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
        if proxy_mode == "proxy" and proxy_url:
            u = str(proxy_url).strip()
            if not (u.startswith("http://") or u.startswith("https://")) or " " in u:
                return JSONResponse(status_code=400, content={
                    "ok": False,
                    "error": "代理地址必须以 http:// 或 https:// 开头，例如 http://127.0.0.1:7897"})

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM sources WHERE name=?", (spec.name,)).fetchone()
            if row is None:
                return {"ok": False, "error": f"源未初始化：{spec.name}"}
            sets, vals = [], []
            if enabled is not None:
                sets.append("enabled=?")
                vals.append(1 if enabled in (True, 1) else 0)
            if proxy_mode is not None:
                sets.append("proxy_mode=?")
                vals.append(proxy_mode)
            if proxy_mode == "proxy" and proxy_url:
                sets.append("proxy_url=?")
                vals.append(str(proxy_url))
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
            rows = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, enabled, proxy_mode, proxy_url FROM sources")}
        finally:
            conn.close()

        def one(spec):
            row = rows.get(spec.id) or {}
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
                    conn.execute("UPDATE sources SET last_ok_at=?, last_error=NULL, last_count=? WHERE id=?",
                                 (now, r["count"], r["id"]))
                else:
                    conn.execute("UPDATE sources SET last_error=? WHERE id=?",
                                 (r.get("error") or "失败", r["id"]))
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
    def _deep_of(conn, item_id: int):
        """取该条目的深加工结果（最新一条），返回 (card, reasons, status)。"""
        row = conn.execute(
            "SELECT payload_json, status FROM analyses "
            "WHERE item_id=? AND stage='deep' ORDER BY id DESC LIMIT 1",
            (item_id,)).fetchone()
        if row is None:
            return None, [], None
        try:
            data = json.loads(row["payload_json"] or "{}")
        except (ValueError, TypeError):
            data = {}
        return data.get("card"), data.get("reasons") or [], row["status"]

    @app.get("/api/item/{item_id}")
    def item_detail(item_id: int):
        """条目详情：原文 + 深加工卡 + 备注 + 收藏态。"""
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT i.id, i.title, i.url, i.summary, i.published_at, i.region, "
                "s.name AS source FROM items i LEFT JOIN sources s ON s.id=i.source_id "
                "WHERE i.id=?", (item_id,)).fetchone()
            if row is None:
                return JSONResponse(status_code=404,
                                    content={"error": "条目不存在"})
            card, reasons, deep_status = _deep_of(conn, item_id)
            note = conn.execute(
                "SELECT content, updated_at FROM notes WHERE item_id=? "
                "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
            fav = conn.execute(
                "SELECT 1 FROM favorites WHERE item_id=?", (item_id,)).fetchone()
            return {
                "item": {"id": row["id"], "title": row["title"], "url": row["url"],
                         "summary": row["summary"], "published_at": row["published_at"],
                         "region": row["region"], "source": row["source"]},
                "card": card,
                "reasons": reasons,
                "deep_status": deep_status,
                "note": note["content"] if note else None,
                "note_updated_at": note["updated_at"] if note else None,
                "favorite": bool(fav),
            }
        finally:
            conn.close()

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
    def list_notes(q: str = "", limit: int = 100):
        """备注列表（含搜索）；同时返回是否已收藏。"""
        limit = max(1, min(int(limit), 500))
        conn = db.get_conn()
        try:
            sql = ("SELECT n.item_id, n.content, n.updated_at, i.title, i.url, "
                   "i.published_at, s.name AS source, "
                   "EXISTS(SELECT 1 FROM favorites f WHERE f.item_id=n.item_id) AS favorite "
                   "FROM notes n JOIN items i ON i.id=n.item_id "
                   "LEFT JOIN sources s ON s.id=i.source_id ")
            args = []
            if q:
                sql += "WHERE n.content LIKE ? "
                args.append(f"%{q}%")
            sql += "ORDER BY n.updated_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            return {"notes": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/favorites")
    def list_favorites(limit: int = 100):
        """收藏列表（按收藏时间倒序）。"""
        limit = max(1, min(int(limit), 500))
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT f.item_id, f.created_at, i.title, i.url, i.published_at, "
                "s.name AS source FROM favorites f JOIN items i ON i.id=f.item_id "
                "LEFT JOIN sources s ON s.id=i.source_id "
                "ORDER BY f.created_at DESC LIMIT ?", (limit,)).fetchall()
            return {"favorites": [dict(r) for r in rows]}
        finally:
            conn.close()

    # ---------------- T10：预算 / 成本 / 日志 ----------------
    @app.get("/api/budget")
    def budget_status():
        """预算熔断状态：本月预算/已花费/剩余/比例/模式（ok/soft/hard）。"""
        from .. import budget
        return budget.status()

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
