"""P5A-1 v2 eight-year backfill runner (network script, not product code).

Loads the v2 sync config and runs backfill_on_startup_v2: resolves the
eight-year window (latest completed trading day, 2080 trading days back),
plans prefix/tail gaps and fetches them serially with the configured request
pacing. Safe to interrupt: run checkpoints are range-bound and the next
launch resumes only the missing parts.

Usage:
    python3 scripts/run_backfill_v2.py [--config config/sync.json] [--db data/market.sqlite3]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_manager.domain import AdjustmentMethod
from stock_manager.providers.baostock_provider import BaostockProvider
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync import DataSyncService
from stock_manager.sync.config import load_sync_config

SHANGHAI = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="run the P5A-1 eight-year v2 backfill")
    parser.add_argument("--config", type=Path, default=Path("config/sync.json"))
    parser.add_argument("--db", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    config = load_sync_config(args.config)
    if config.history is None:
        print("错误:配置不是 v2(缺少 history 块),无法执行八年回补", file=sys.stderr)
        return 1

    repository = SQLiteRepository(args.db)
    provider = BaostockProvider(
        request_interval_seconds=config.minimum_request_interval_seconds,
    )

    def progress(event: dict[str, object]) -> None:
        phase = event.get("phase", "?")
        completed = event.get("completed", 0)
        total = event.get("total", 0)
        current = event.get("current_code", "")
        stamp = datetime.now(SHANGHAI).strftime("%H:%M:%S")
        print(f"[{stamp}] {phase} {completed}/{total} {current}", flush=True)

    service = DataSyncService(provider, repository, Path("data/locks"), config, progress=progress)

    print(
        f"开始八年回补:target_years={config.history.target_years} "
        f"batch_size={args.batch_size}",
        flush=True,
    )
    started = time.monotonic()
    outcome = service.backfill_on_startup_v2("market", AdjustmentMethod.QFQ)
    elapsed_minutes = (time.monotonic() - started) / 60
    if outcome is None:
        print("回补完成:目标窗口已覆盖或已由本运行补齐", flush=True)
    else:
        print(f"回补结果: status={outcome.status.value} skipped={outcome.skipped}", flush=True)
    print(f"耗时 {elapsed_minutes:.1f} 分钟", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
