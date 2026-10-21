#!/bin/bash
# 热点采集网络 · macOS 一键启动
# 用法：双击本文件；或在终端执行 ./启动.command
# 首次运行会自动创建 .venv 并安装依赖（用清华镜像加速）
set -e
cd "$(dirname "$0")"

PORT="${PORT:-9000}"
BRIDGE_PORT="${RSS_BRIDGE_PORT:-8053}"

echo "============================================"
echo "  热点采集网络 · Hotspot Collection Network"
echo "  http://127.0.0.1:${PORT}/"
echo "============================================"

if [ -f .env ]; then
  set -a; source .env; set +a
fi
PORT="${PORT:-9000}"

# 端口已占用 → 直接开浏览器
if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[提示] 服务已在运行，直接打开页面…"
  open "http://127.0.0.1:${PORT}/"
  exit 0
fi

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "未找到 python3，请先安装 Python 3.11+（https://www.python.org/downloads/macos/）"
  read -r -p "按回车退出…" _
  exit 1
fi
echo "Python: $($PY --version)"

if [ ! -d .venv ]; then
  echo "创建虚拟环境 .venv …"
  "$PY" -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
echo "安装依赖（首次约 1-3 分钟）…"
python -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "启动服务（首次约 5-10 秒，浏览器会自动打开）…"
( sleep 6; open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true ) &

python run.py
