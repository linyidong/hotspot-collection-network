# -*- coding: utf-8 -*-
"""模块：SQLite 数据库层。职责：连接 data/app.db、开 WAL、幂等建 8 张表
    （sources/batches/items/analyses/notes/favorites/llm_calls/config），字段按 PRD §8；
    提供 get_conn / init_db / table_names / db_ok。关键依赖：stdlib sqlite3、app.config。"""
import sqlite3
from pathlib import Path

from .config import settings

# 8 张表的建表语句（IF NOT EXISTS，可重复执行；字段按 PRD §8 数据表）
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sources(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        proxy_mode TEXT NOT NULL DEFAULT 'direct',
        proxy_url TEXT,
        last_ok_at TEXT,
        last_error TEXT)""",
    """CREATE TABLE IF NOT EXISTS batches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        trigger TEXT NOT NULL DEFAULT '自动',
        status TEXT NOT NULL DEFAULT 'running',
        stats_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER,
        source_id INTEGER,
        title TEXT NOT NULL,
        url TEXT,
        url_hash TEXT,
        summary TEXT,
        published_at TEXT,
        region TEXT,
        is_duplicate INTEGER NOT NULL DEFAULT 0,
        duplicate_of INTEGER)""",
    """CREATE TABLE IF NOT EXISTS analyses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        stage TEXT NOT NULL,
        model TEXT,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        cost REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS favorites(
        item_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS llm_calls(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER,
        stage TEXT,
        provider TEXT,
        model TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        unit_price_in REAL NOT NULL DEFAULT 0,
        unit_price_out REAL NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS config(
        key TEXT PRIMARY KEY,
        value TEXT)""",
]


def get_conn() -> sqlite3.Connection:
    """打开数据库连接（自动建 data 目录）。"""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> int:
    """建表并开 WAL；幂等，可重复调用。返回建表后的表数量。"""
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for ddl in SCHEMA:
            conn.execute(ddl)
        _migrate_sources(conn)
        conn.commit()
        return table_names(conn)
    finally:
        conn.close()


def _migrate_sources(conn: sqlite3.Connection):
    """sources 表增量迁移：补 last_count 列（T3 采集层需要）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
    if "last_count" not in cols:
        conn.execute(
            "ALTER TABLE sources ADD COLUMN last_count INTEGER NOT NULL DEFAULT 0")


def table_names(conn: sqlite3.Connection = None) -> list:
    """返回库内全部用户表名。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        if own:
            conn.close()


def db_ok() -> tuple:
    """健康检查：连接是否可用、是否恰好 8 张表。返回 (ok: bool, tables: list)。"""
    try:
        names = table_names()
        ok = len(names) == 8 and "batches" in names and "items" in names
        return ok, names
    except sqlite3.Error:
        return False, []
