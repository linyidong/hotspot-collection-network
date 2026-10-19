# -*- coding: utf-8 -*-
"""模块：唯一启动入口。职责：依次检查 Python 版本≥3.11、依赖齐全、端口可用，
    初始化配置与数据库，打印中文摘要后起 uvicorn；任一失败给出中文错误+怎么办。
    关键依赖：app.config / app.db / app.api.routes、fastapi、uvicorn。"""
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
from app.db import init_db, table_names

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

# ===== 4. 端口占用检查 =====
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

# ===== 6. 控制台中文摘要（版本/端口/库路径/Key 状态）=====
key_txt = "已配置 " + mask_key(settings.DEEPSEEK_API_KEY) if settings.key_configured \
    else "未配置（key_configured=false，页面会提示去申请）"
print("=" * 56)
print("热点采集网络 · T2 骨架 启动中")
print(f"版本    : v{settings.VERSION}")
print(f"端口    : http://127.0.0.1:{_port}")
print(f"数据库  : {settings.db_path}（{len(_tables)} 张表，WAL）")
print(f"API Key : {key_txt}")
print("=" * 56, flush=True)

# ===== 7. 起 uvicorn =====
import uvicorn
from app.api.routes import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=_port, log_level="info")
