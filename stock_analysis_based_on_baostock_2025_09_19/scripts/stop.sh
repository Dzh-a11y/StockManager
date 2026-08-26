#!/usr/bin/env bash
# 停止 StockManager Web 服务
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PID_FILE="$ROOT/data/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "没有 PID 文件(服务可能未通过脚本启动,或用 lsof -nP -i :8000 手动找)"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "进程 $PID 已不存在,清理过期 PID 文件"
  rm -f "$PID_FILE"
  exit 0
fi

kill "$PID"
for _ in $(seq 1 10); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "已停止 (pid $PID)"
    exit 0
  fi
  sleep 0.5
done

echo "进程未响应 SIGTERM,强制停止"
kill -9 "$PID" || true
rm -f "$PID_FILE"
echo "已强制停止 (pid $PID)"
