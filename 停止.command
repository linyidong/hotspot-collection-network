#!/bin/bash
# 热点采集网络 · macOS 停止
cd "$(dirname "$0")"
PORT="${PORT:-9000}"
BRIDGE_PORT="${RSS_BRIDGE_PORT:-8053}"
for p in "$PORT" "$BRIDGE_PORT"; do
  PIDS=$(lsof -ti tcp:"$p" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "结束端口 $p 上的进程：$PIDS"
    kill -9 $PIDS 2>/dev/null || true
  fi
done
echo "已停止。"
sleep 1
