#!/usr/bin/env bash
# 查看 StockManager Web 服务状态
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PID_FILE="$ROOT/data/server.pid"
PORT=8000

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "运行中 (pid $(cat "$PID_FILE"),脚本启动)"
elif lsof -nP -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  PID="$(lsof -nP -i :"$PORT" -sTCP:LISTEN -t | head -1)"
  echo "运行中 (pid $PID,非脚本启动)"
else
  echo "未运行"
  if [[ -f "$PID_FILE" ]]; then
    echo "(发现过期 PID 文件,已清理)"
    rm -f "$PID_FILE"
  fi
  exit 0
fi

if command -v curl >/dev/null 2>&1; then
  HEALTH="$(curl -s --max-time 2 http://127.0.0.1:$PORT/health 2>/dev/null || echo '无响应')"
  echo "健康检查: $HEALTH"
fi
