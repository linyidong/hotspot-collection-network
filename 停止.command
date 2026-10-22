#!/bin/bash
# 热点采集网络 · macOS 停止器
# 用法：双击本文件，或在终端 ./停止.command
# 行为：优先按 pid 文件停；再按端口兜底（9000 服务 + 8053 RSS 桥）
cd "$(dirname "$0")" || exit 1

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' < .env)
  set +a
fi
PORT="${PORT:-9000}"
BRIDGE_PORT="${RSS_BRIDGE_PORT:-8053}"

# 1) pid 文件
if [ -f data/logs/app.pid ]; then
  PID=$(cat data/logs/app.pid 2>/dev/null || true)
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "结束服务进程 pid=$PID"
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f data/logs/app.pid
fi

# 2) 端口兜底（服务 + 桥）
for p in "$PORT" "$BRIDGE_PORT"; do
  PIDS=$(lsof -ti tcp:"$p" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "结束端口 $p 上的进程：$PIDS"
    # shellcheck disable=SC2086
    kill -9 $PIDS 2>/dev/null || true
  fi
done

echo "已停止。"
exit 0
