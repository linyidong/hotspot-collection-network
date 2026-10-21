# -*- coding: utf-8 -*-
"""模块：唯一启动入口。职责：依次检查 Python 版本≥3.11、依赖齐全、端口可用，
    初始化配置与数据库，拉起 RSS 桥子进程，seed 8 源进 sources 表，打印中文摘要后起 uvicorn；
    Ctrl+C 一并关闭桥子进程；`python run.py --fetch-once` 只跑一轮采集后退出。
    关键依赖：app.config / app.db / app.bridge / app.api.routes / app.sources.*、fastapi、uvicorn。"""
import importlib.util
import socket
import sys

# ===== 1. Python 版本检查 =====
if sys.version_info < (3, 11):
    print("错误：需要 Python 3.11 或更高版本。")
    print("怎么办：请到 https://www.python.org/downloads/ 安装 Python 3.11+，安装时勾选“Add Python to PATH”，然后重新运行 python run.py。")
    sys.exit(1)

# ===== 2. 依赖检查（缺哪个报哪个）=====
REQUIRED_MODULES = ["fastapi", "uvicorn", "dotenv", "httpx", "feedparser",
                    "apscheduler", "openai"]
_missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
if _missing:
    print("错误：缺少依赖：" + ", ".join(_missing))
    print("怎么办：在项目目录执行  pip install -r requirements.txt  后重新运行 python run.py。")
    sys.exit(1)

# ===== 3. 配置与数据库 =====
from app.config import settings, mask_key
from app.db import init_db, table_names, get_conn
from app.bridge import bridge
from app.sources.registry import SOURCES

FETCH_ONCE = "--fetch-once" in sys.argv
RUN_ONCE = "--run-once" in sys.argv
NO_SCHEDULER = "--no-scheduler" in sys.argv
PLATFORM_SNAPSHOT = "--platform-snapshot" in sys.argv

# ===== 平台榜单快照模式：不启动服务、不占端口，只抓榜单 =====
if PLATFORM_SNAPSHOT:
    from app.platform import refresh, PAGES
    print("正在用 headless Chrome 抓取平台榜单（约 1 分钟）…", flush=True)
    snap = refresh(PAGES, per_page=20)
    for k, v in (snap.get("pages") or {}).items():
        print(f"  {v.get('label', k):20s} {len(v.get('items') or []):2d} 条")
    for k, e in (snap.get("errors") or {}).items():
        print(f"  失败 {k}: {e}")
    print(f"快照时间：{snap.get('ts')} → data/platform_snapshot.json")
    sys.exit(0)

# --deep-limit N：本轮深加工上限（T5；默认取 config.DEEP_LIMIT_PER_RUN）
DEEP_LIMIT = None
if "--deep-limit" in sys.argv:
    _i = sys.argv.index("--deep-limit")
    try:
        DEEP_LIMIT = int(sys.argv[_i + 1])
    except (IndexError, ValueError):
        print("错误：--deep-limit 需要一个整数参数，例如 --deep-limit 10")
        sys.exit(1)

# data 目录可写检查（config 已加载，此处显式确认）
_db_parent = settings.db_path.parent
try:
    _db_parent.mkdir(parents=True, exist_ok=True)
    _probe = _db_parent / ".write_probe"
    _probe.write_text("ok", encoding="utf-8")
    _probe.unlink()
except OSError as e:
    print(f"错误：数据目录不可写：{_db_parent}（{e}）")
    print("怎么办：检查该路径的写入权限，或修改 data/config.json 中的数据库路径。")
    sys.exit(1)

# ===== 4. 端口占用检查（仅正式启动服务时；--fetch-once / --run-once 不占端口）=====
if not FETCH_ONCE and not RUN_ONCE and not PLATFORM_SNAPSHOT:
    _port = settings.port
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _sock.bind(("127.0.0.1", _port))
        _port_free = True
    except OSError:
        _port_free = False
    finally:
        _sock.close()
    if not _port_free:
        print(f"错误：端口 {_port} 已被占用，无法启动服务。")
        print(f"怎么办：① 改端口：在 .env 中把 PORT={_port} 改成其它值（如 8001）后重试；② 或关闭占用该端口的程序。")
        sys.exit(1)

# ===== 5. 建库建表 =====
_tables = init_db()
if len(_tables) != 8:
    print(f"错误：数据库表数量异常（期望 8，实际 {len(_tables)}：{_tables}）。")
    print("怎么办：删除 data/app.db 后重新运行 python run.py（会重新建表）。")
    sys.exit(1)


def seed_sources():
    """把注册表的 8 个源写入 sources 表（幂等：按 name 匹配，保留用户改过的开关/代理）。"""
    conn = get_conn()
    try:
        for spec in SOURCES:
            row = conn.execute("SELECT id FROM sources WHERE name=?", (spec.name,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sources(name, url, enabled, proxy_mode) VALUES(?,?,1,'direct')",
                    (spec.name, spec.url))
        conn.commit()
    finally:
        conn.close()


# ===== 6. 拉起 RSS 桥 =====
if not bridge.start():
    print("错误：本地 RSS 桥启动失败。")
    print("怎么办：确认 third_party/china-finance-rss/server.py 存在且 8053 端口未被占用；可用 --fetch-once 之外的方式单独排查桥日志。")
    sys.exit(1)
seed_sources()

if FETCH_ONCE or RUN_ONCE:
    print("=" * 56)
    print("热点采集网络 · " + ("单轮流水线（--run-once）" if RUN_ONCE else "单轮采集（--fetch-once）"))
    print(f"桥端口  : {bridge.port}")
    print("=" * 56, flush=True)
    if FETCH_ONCE:
        from app.sources.collect import collect_once
        results = collect_once()
        print("-" * 56)
        for spec in SOURCES:
            r = results.get(spec.id, {})
            if r.get("skipped"):
                print(f"{spec.name:12s} 已停用（未抓取）")
            elif r["error"] is None:
                print(f"{spec.name:12s} ok  {len(r['items']):3d} 条")
            else:
                print(f"{spec.name:12s} 失败  {r['error']}")
        print("-" * 56)
        n_ok = sum(1 for s in SOURCES
                   if (results.get(s.id) or {}).get("error") is None
                   and not (results.get(s.id) or {}).get("skipped"))
        print(f"成功源 {n_ok}/{len(SOURCES)}。失败源的 last_error 已写入 sources 表。")
    else:
        from app.pipeline import run_once
        st = run_once(deep_limit=DEEP_LIMIT, trigger="手动")
        print("-" * 56)
        print(f"批次      : #{st['batch_id']}（{st['status']}）")
        print(f"抓取数    : {st['fetched']} 条（源 ok {st['sources_ok']} / 失败 {st['sources_failed']}）")
        print(f"去重后    : {st['after_dedup']} 条（重复 {st['fetched'] - st['after_dedup']} 条）")
        print(f"粗筛通过  : {st['coarse_ok']} 条")
        print(f"粗筛拒绝  : {st['coarse_skipped']} 条（非经济）+ {st['coarse_failed']} 条（失败）")
        print(f"深加工    : 候选 {st['deep_candidates']} 条 → ok {st['deep_ok']}"
              f" / rejected {st['deep_rejected']}（其中 r11 能力闸拦截 {st['deep_r11']}）"
              f" / failed {st['deep_failed']}")
        print(f"候选池    : 剩余 {st['deep_skipped']} 条未加工（超出本轮 limit）")
        print(f"本轮花费  : {st['cost']:.4f} 元")
        print(f"耗时      : {st['duration_s']} 秒")
    print("-" * 56)
    bridge.stop()
    sys.exit(0)

# ===== 7. 控制台中文摘要（版本/端口/库路径/Key 状态/桥状态）=====
key_txt = "已配置 " + mask_key(settings.DEEPSEEK_API_KEY) if settings.key_configured \
    else "未配置（key_configured=false，页面会提示去申请）"
print("=" * 56)
print("热点采集网络 · T3 采集层 启动中")
print(f"版本    : v{settings.VERSION}")
print(f"端口    : http://127.0.0.1:{_port}")
print(f"数据库  : {settings.db_path}（{len(_tables)} 张表，WAL）")
print(f"RSS 桥  : http://127.0.0.1:{bridge.port}（子进程 pid={bridge.proc.pid if bridge.proc else '-'}）")
print(f"API Key : {key_txt}")
print("=" * 56, flush=True)

# ===== 8. 起 uvicorn（Ctrl+C 时一并关闭桥子进程）=====
import uvicorn
from app.api.routes import create_app

if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1",
                                           port=_port, log_level="info"))
    from app import scheduler as sched
    # 启动清理：上次被强杀留下的 running 批次，标记为 failed（避免僵尸批次）
    try:
        _c = get_conn()
        _n = _c.execute(
            "UPDATE batches SET status='failed', finished_at=datetime('now','localtime') "
            "WHERE status='running' AND finished_at IS NULL").rowcount
        _c.commit()
        _c.close()
        if _n:
            print(f"[启动] 清理了 {_n} 个上次中断的批次（标记 failed）", flush=True)
    except Exception as _e:
        print(f"[启动] 清理僵尸批次失败：{_e}", flush=True)
    if not NO_SCHEDULER:
        sched.start(deep_limit=DEEP_LIMIT)
    else:
        print("[调度] 已按 --no-scheduler 关闭（仅手动触发）", flush=True)
    try:
        server.run()
    finally:
        sched.shutdown()
        bridge.stop()
        print("RSS 桥已随主服务一并关闭。")
