#!/usr/bin/env bash
# 启动 StockManager Web 服务(后台运行,日志写入 data/server.log)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PID_FILE="$ROOT/data/server.pid"
LOG_FILE="$ROOT/data/server.log"
PORT=8000

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "服务已在运行 (pid $(cat "$PID_FILE"))"
  exit 0
fi

if lsof -nP -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被其他进程占用,请先停止它"
  exit 1
fi

mkdir -p "$ROOT/data"
nohup "$ROOT/.venv/bin/python" -m stock_manager.cli web \
  --db data/market.sqlite3 \
  --system-templates config/rule_templates \
  --user-templates data/user-templates \
  --static src/stock_manager/web/static \
  --sync-config config/sync.json \
  --lock-dir data/locks \
  --host 127.0.0.1 --port "$PORT" \
  >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 1
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "已启动 (pid $(cat "$PID_FILE")): http://127.0.0.1:$PORT"
  echo "日志: $LOG_FILE"
else
  echo "启动失败,请查看日志: $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
