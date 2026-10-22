#!/bin/bash
# 热点采集网络 · macOS 启动器
# 用法：双击本文件（首次需 chmod +x 启动.command），或在终端 ./启动.command
# 行为：建 venv（首次）→ 后台启动服务 → 等健康检查 → 按选择打开页面 → 退出（可关终端）
set -e
cd "$(dirname "$0")"

# ---------- 1. 读取配置（兼容 Windows 生成的 CRLF .env） ----------
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' < .env)
  set +a
fi
PORT="${PORT:-9000}"
BRIDGE_PORT="${RSS_BRIDGE_PORT:-8053}"
BASE="http://127.0.0.1:${PORT}"

# ---------- 2. 选择要打开的页面 ----------
PAGES=( "/" "/static/settings.html" "/static/cost.html" "/static/notes.html" )
NAMES=( "大屏（3D 地球 + 线索）" "设置（源/模型/代理/体检）" "成本与预算" "备忘录 / 收藏" )
CHOICE="${HCN_PAGE:-}"

if [ -z "$CHOICE" ]; then
  echo "============================================"
  echo "  热点采集网络 · Hotspot Collection Network"
  echo "============================================"
  echo "启动后打开哪个页面？"
  for i in "${!PAGES[@]}"; do
    echo "  $((i+1))) ${NAMES[$i]}"
  done
  echo "  5) 只启动，不打开网页"
  echo "  0) 退出"
  read -r -p "请输入序号 [默认 1]： " CHOICE
  CHOICE="${CHOICE:-1}"
fi

case "$CHOICE" in
  0) echo "已取消。"; exit 0 ;;
  1) TARGET="/" ;;
  2) TARGET="/static/settings.html" ;;
  3) TARGET="/static/cost.html" ;;
  4) TARGET="/static/notes.html" ;;
  5) TARGET="" ;;
  *) TARGET="/" ;;
esac

# ---------- 3. 已在运行 → 直接开页面 ----------
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[提示] 服务已在运行。"
  if [ -n "$TARGET" ]; then open "${BASE}${TARGET}"; fi
  exit 0
fi

# ---------- 4. 准备 Python 与依赖 ----------
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "未找到 python3。请先安装 Python 3.11+："
  echo "  brew install python@3.12   或   https://www.python.org/downloads/macos/"
  read -r -p "按回车退出…" _
  exit 1
fi
echo "Python: $("$PY" --version 2>&1)"

if [ ! -d .venv ]; then
  echo "创建虚拟环境 .venv …"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .venv/.deps_ok ]; then
  echo "安装依赖（首次约 1–3 分钟，用清华镜像）…"
  python -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  python -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  touch .venv/.deps_ok
fi

# ---------- 5. 后台启动（关掉终端也不影响） ----------
mkdir -p data/logs
echo "启动服务（后台运行，日志：data/logs/app.log）…"
nohup python run.py >> data/logs/app.log 2>&1 &
echo $! > data/logs/app.pid

# ---------- 6. 等健康检查通过 ----------
READY=0
for _ in $(seq 1 60); do
  sleep 0.5
  if curl -fsS -m 2 "${BASE}/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
done

if [ "$READY" != "1" ]; then
  echo "服务启动超时。请查看日志：data/logs/app.log"
  tail -n 20 data/logs/app.log 2>/dev/null || true
  read -r -p "按回车退出…" _
  exit 1
fi

if [ -n "$TARGET" ]; then
  open "${BASE}${TARGET}"
  echo "已在浏览器打开：${BASE}${TARGET}"
else
  echo "服务已启动：${BASE}（按需自行打开页面）"
fi
echo "停止服务：双击 停止.command"
exit 0
