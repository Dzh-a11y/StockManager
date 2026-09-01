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
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        help="瞬时网络失败时最多重启尝试(断点续传)次数;默认 8",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=30.0,
        help="两次尝试之间的冷却秒数;默认 30",
    )
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

    # 瞬时网络故障会抛 SyncFailedError;为让八年回补能完成,
    # 在 runner 层做有界重启(断点续传,复用已完成块),避免整批任务因单次抖动报废。
    max_attempts = max(args.max_attempts, 1)
    cooldown = max(args.cooldown, 0.0)
    started = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        print(
            f"开始八年回补(第 {attempt}/{max_attempts} 次):"
            f"target_years={config.history.target_years} batch_size={args.batch_size}",
            flush=True,
        )
        try:
            outcome = service.backfill_on_startup_v2("market", AdjustmentMethod.QFQ)
            elapsed_minutes = (time.monotonic() - started) / 60
            if outcome is None:
                print("回补完成:目标窗口已覆盖或已由本运行补齐", flush=True)
            else:
                print(
                    f"回补结果: status={outcome.status.value} skipped={outcome.skipped}",
                    flush=True,
                )
            print(f"耗时 {elapsed_minutes:.1f} 分钟", flush=True)
            return 0
        except Exception as error:
            elapsed_minutes = (time.monotonic() - started) / 60
            print(
                f"回补失败(第 {attempt}/{max_attempts} 次,"
                f"已运行 {elapsed_minutes:.1f} 分):{type(error).__name__}: {error}",
                flush=True,
            )
            if attempt >= max_attempts:
                print("达到最大重试次数,放弃本次回补", flush=True)
                return 1
            print(f"{cooldown:g} 秒后从已完成块断点续传...", flush=True)
            time.sleep(cooldown)
    return 1


if __name__ == "__main__":
    sys.exit(main())
