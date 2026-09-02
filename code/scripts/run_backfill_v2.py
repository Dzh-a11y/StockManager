"""Eight-year backfill runner on the P5 pipeline (network script, not product code).

Loads the sync config and runs startup_sync(force_pipeline=True): the P5
SyncPipeline with batch granularity (20 codes x range) resolves the
eight-year window, stages fetches, verifies and atomically publishes a
generation. Safe to interrupt: plan tasks are check-pointed and the next
launch resumes only PENDING/INTERRUPTED tasks; SUCCESS tasks are never
re-fetched.

Usage:
    python3 scripts/run_backfill_v2.py [--config config/sync.json]
        [--db data/market.sqlite3] [--lock-dir data/locks]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_manager.domain import AdjustmentMethod
from stock_manager.providers.baostock_provider import BaostockProvider
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync import DataSyncService
from stock_manager.sync.config import load_sync_config
from stock_manager.sync.data_sync_service import SyncConfig

SHANGHAI = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="run the P5A-1 eight-year v2 backfill")
    parser.add_argument("--config", type=Path, default=Path("config/sync.json"))
    parser.add_argument("--db", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--lock-dir", type=Path, default=Path("data/locks"))
    parser.add_argument(
        "--adjustment",
        choices=tuple(item.value for item in AdjustmentMethod),
        default=AdjustmentMethod.QFQ.value,
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="本次显式启动内的最大尝试次数;默认 1,避免后台自动重试",
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
        request_interval_seconds=(
            config.backfill_request_interval_seconds
            or config.minimum_request_interval_seconds
        ),
    )

    def progress(event: dict[str, object]) -> None:
        phase = event.get("phase", "?")
        completed = event.get("completed", 0)
        total = event.get("total", 0)
        current = event.get("current_code", "")
        stamp = datetime.now(SHANGHAI).strftime("%H:%M:%S")
        print(f"[{stamp}] {phase} {completed}/{total} {current}", flush=True)

    # 防重复启动:非阻塞独占锁,第二个进程启动即退出,避免多实例抢同一计划。
    from stock_manager.sync.locks import try_persistent_file_lock

    lock_path = args.lock_dir / "backfill_runner.lock"
    with try_persistent_file_lock(lock_path) as acquired:
        if not acquired:
            print(
                "另一个回补进程已在运行(backfill_runner.lock 被持有),本进程退出。",
                file=sys.stderr,
            )
            return 2
        return _run_with_lock(args, config, repository, provider, progress)


def _run_with_lock(
    args: argparse.Namespace,
    config: SyncConfig,
    repository: SQLiteRepository,
    provider: BaostockProvider,
    progress: Callable[[dict[str, object]], None],
) -> int:
    """在持有 backfill_runner.lock 期间执行一次显式回补。"""
    from stock_manager.sync import DataSyncService

    adjustment = AdjustmentMethod(args.adjustment)
    service = DataSyncService(
        provider,
        repository,
        args.lock_dir,
        config,
        progress=progress,
    )
    pipeline = service.build_pipeline()

    # 上一实例被强制终止时可能残留 RUNNING 任务；只恢复中断状态。
    # FAILED 状态仍由下面的 retry_failed=True 走显式重试与冷却检查。
    for plan in repository.list_sync_plans("market", adjustment):
        if plan.status.value == "SUCCEEDED":
            continue
        reset = pipeline.recover_interrupted(plan.plan_id)
        if reset:
            print(f"清理上一实例残留: 重置 {reset} 个任务为 PENDING", flush=True)

    # 默认只尝试一次；只有调用者主动传 --max-attempts 才会在同一
    # 显式启动内做有界重试。Provider 自己仍负责单请求瞬时网络重试。
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
            # 新架构:startup_sync(force_pipeline=True) 走 SyncPipeline 批量粒度
            # (20 只 × 区间),staging → 验证 → 原子发布 generation。
            # pipeline.execute 幂等:SUCCESS 任务跳过,中断后从 PENDING/INTERRUPTED 续传。
            run = service.startup_sync(
                "market", adjustment, force_pipeline=True,
                batch_size=args.batch_size,
                retry_failed=True,
            )
            elapsed_minutes = (time.monotonic() - started) / 60
            plan_status = getattr(run, "plan_status", None)
            published = getattr(run, "published", False)
            warning = getattr(run, "warning", None)
            print(
                f"回补结果: plan_status={plan_status} published={published}"
                f"{' warning=' + warning if warning else ''}",
                flush=True,
            )
            print(f"耗时 {elapsed_minutes:.1f} 分钟", flush=True)
            if published:
                print("generation 已原子发布,ReadinessGate 将返回 READY", flush=True)
            else:
                print(
                    "数据已入库但 generation 未发布(验证未全通过);"
                    "可用 sync-status / sync-verify 查看原因",
                    flush=True,
                )
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
