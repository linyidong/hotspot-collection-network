# -*- coding: utf-8 -*-
"""模块：API 路由。职责：创建 FastAPI 应用，提供 GET /api/health、GET/PUT /api/sources；
    根路径返回 static/index.html，并挂载 /static 静态目录。
    /api/health 携带 bridge.ok（RSS 桥状态，挂了自动重启）；/api/sources 返回 8 源状态。
    关键依赖：fastapi、app.config、app.db、app.bridge、app.sources.registry。"""
import time

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse
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
    def sources():
        bridge.ensure()
        return {"sources": _load_sources()}

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

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(settings.static_dir / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    return app
