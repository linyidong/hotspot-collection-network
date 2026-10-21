# -*- coding: utf-8 -*-
"""模块：采集编排（单轮）。职责：按数据库 sources 表的开关/代理配置，
    用线程池并行抓取 8 源，单源失败隔离（中文原因写 last_error），
    成功写 last_ok_at/last_count，返回 {source_id: {items, error, skipped}}。
    关键依赖：concurrent.futures、httpx、app.sources.*、app.db、app.config。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx

from .. import db
from ..config import settings
from .registry import SOURCES
from .gnews import GoogleNewsSource
from .rss_bridge import RssBridgeSource
from .sina import SinaSource
from .snapshot import SnapshotSource
from .weibo import WeiboHotSource
from .wallstcn import WallStcnSource

# 统一的单源超时（任务书：20s）
FETCH_TIMEOUT = 20.0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _human_error(exc: Exception) -> str:
    """把异常转成一行中文原因，不暴露裸堆栈。"""
    if isinstance(exc, httpx.ConnectTimeout):
        return f"连接超时（> {FETCH_TIMEOUT}秒）：目标不可达或网络不通"
    if isinstance(exc, httpx.ReadTimeout):
        return f"读取超时（> {FETCH_TIMEOUT}秒）：目标响应过慢"
    if isinstance(exc, httpx.ConnectError):
        return f"连接失败：目标地址不可达"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}：上游返回错误状态"
    if isinstance(exc, ValueError):
        return f"数据格式异常：{exc}"
    return str(exc) or exc.__class__.__name__


def _build_source(spec, proxy_mode: str, proxy_url: str):
    """按注册表定义构造源实例。type=api 直连，type=bridge 走本地桥。"""
    proxy = proxy_url if (proxy_mode == "proxy" and proxy_url) else None
    if spec.type == "rss":
        return GoogleNewsSource(spec, proxy_url=proxy, timeout=FETCH_TIMEOUT)
    if spec.type == "json":
        return WeiboHotSource(spec, proxy_url=proxy, timeout=FETCH_TIMEOUT)
    if spec.type == "snapshot":
        return SnapshotSource(spec, timeout=FETCH_TIMEOUT)
    if spec.type == "api":
        if spec.id == "sina":
            return SinaSource(spec, proxy_url=proxy, timeout=FETCH_TIMEOUT)
        return WallStcnSource(spec, proxy_url=proxy, timeout=FETCH_TIMEOUT)
    return RssBridgeSource(spec, bridge_port=settings.rss_bridge_port,
                           timeout=FETCH_TIMEOUT)


def _run_one(src):
    """抓取单个源；任何异常都转成中文原因返回，保证单个源失败不影响整体。"""
    try:
        items = src.fetch()
        return items, None
    except Exception as exc:
        return [], _human_error(exc)


def collect_once(conn=None) -> dict:
    """抓取一次所有启用的源。返回 {source_id: {"items", "error", "skipped"}}。
    慢源在独立线程并行，互不阻塞；失败原因已入库。"""
    own = conn is None
    if own:
        conn = db.get_conn()
    results = {}
    try:
        rows = conn.execute(
            "SELECT name, enabled, proxy_mode, proxy_url FROM sources"
        ).fetchall()
        config_by_name = {r["name"]: r for r in rows}

        jobs = []
        for spec in SOURCES:
            cfg = config_by_name.get(spec.name)
            enabled = (cfg["enabled"] if cfg is not None else 1) == 1
            if not enabled:
                results[spec.id] = {"items": [], "error": None, "skipped": True}
                continue
            proxy_mode = cfg["proxy_mode"] if cfg is not None else "direct"
            proxy_url = cfg["proxy_url"] if cfg is not None else None
            jobs.append((spec.id, _build_source(spec, proxy_mode, proxy_url)))

        with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as pool:
            futures = {pool.submit(_run_one, src): sid for sid, src in jobs}
            for fut in as_completed(futures):
                sid = futures[fut]
                items, err = fut.result()
                results[sid] = {"items": items, "error": err, "skipped": False}

        _write_back(conn, results)
        conn.commit()
    finally:
        if own:
            conn.close()
    return results


def _write_back(conn, results: dict):
    """把抓取结果写回 sources 表：成功更新 last_ok_at/last_count 并清 last_error；
    失败只写 last_error（保留旧 last_ok_at）。"""
    name_by_id = {spec.id: spec.name for spec in SOURCES}
    for sid, r in results.items():
        if r.get("skipped"):
            continue
        name = name_by_id.get(sid)
        if not name:
            continue
        if r["error"] is None:
            conn.execute(
                "UPDATE sources SET last_ok_at=?, last_error=NULL, last_count=? WHERE name=?",
                (_now(), len(r["items"]), name),
            )
        else:
            conn.execute(
                "UPDATE sources SET last_error=? WHERE name=?",
                (r["error"], name),
            )
