# -*- coding: utf-8 -*-
"""模块：API 路由。职责：创建 FastAPI 应用，提供 GET /api/health；
    根路径返回 static/index.html（深色占位页），并挂载 /static 静态目录。
    关键依赖：fastapi、app.config（版本/密钥状态）、app.db（建表与健康检查）。"""
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from ..config import settings

# 进程启动时刻，用于 uptime_seconds
_START = time.time()

TABLES = ("sources", "batches", "items", "analyses", "notes",
          "favorites", "llm_calls", "config")


def create_app() -> FastAPI:
    """组装 FastAPI 应用：先建表，再挂路由与静态资源。"""
    app = FastAPI(title="Hotspot Collection Network", version=settings.VERSION, docs_url="/api/docs")
    app.state.start_time = _START

    @app.get("/api/health")
    def health():
        ok, names = db.db_ok()
        uptime = round(time.time() - _START, 1)
        return {
            "status": "ok",
            "version": settings.VERSION,
            "db": {"ok": ok, "tables": len(names), "path": str(settings.db_path)},
            "db_ok": ok,
            "db_tables": len(names),
            "key_configured": settings.key_configured,
            "uptime_seconds": uptime,
        }

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(settings.static_dir / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    return app
