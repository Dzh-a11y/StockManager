"""P5A-1 v2 backfill smoke test: real provider, bounded scope (network script).

Verifies the eight-year window resolution, a single-stock eight-year fetch and
the resume state of the interrupted run, WITHOUT executing the full backfill.
"""

from __future__ import annotations

import sys
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
    config = load_sync_config(Path("config/sync.json"))
    assert config.history is not None, "配置不是 v2"
    repository = SQLiteRepository(Path("data/market.sqlite3"))
    provider = BaostockProvider(
        request_interval_seconds=config.minimum_request_interval_seconds,
    )
    service = DataSyncService(provider, repository, Path("data/locks"), config)

    # 1) 八年目标区间解析
    target_start, target_end = service._resolve_targets_v2()
    print(f"[smoke] 目标区间: {target_start} ~ {target_end}", flush=True)
    assert target_start.year <= 2019, f"目标起点异常: {target_start}"
    print(f"[smoke] 八年窗口解析 OK(起点 {target_start},终点 {target_end})", flush=True)

    # 2) coverage 计划(缺口分析,不拉取)
    from stock_manager.sync.history_plan import plan_coverage

    plan = plan_coverage(
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=target_start,
        target_end=target_end,
        probe=repository.actual_coverage,
    )
    daily = plan.by_type("daily_bars")
    print(
        f"[smoke] daily_bars: earliest={daily.actual_earliest} latest={daily.actual_latest} "
        f"status={daily.status.value} prefix={daily.prefix_gap} tail={daily.tail_gap}",
        flush=True,
    )

    # 3) 单股票八年真实拉取验证
    bars = provider.fetch_daily_bars(("sh.600000",), target_start, target_end, AdjustmentMethod.QFQ)
    print(f"[smoke] 单股票八年拉取: sh.600000 返回 {len(bars)} 根 bar", flush=True)
    assert len(bars) > 1500, f"八年 bar 数异常: {len(bars)}"
    first, last = bars[0], bars[-1]
    print(f"[smoke] 范围: {first.trading_day} ~ {last.trading_day}", flush=True)

    # 4) 中断 run 的续传状态
    runs = repository.list_backfill_runs_v2("market", AdjustmentMethod.QFQ)
    for run in runs:
        chunks = repository.completed_chunk_codes_v2(run.run_id)
        total_chunks = sum(len(chunks.get(i, [])) for i in chunks)
        print(
            f"[smoke] run {run.run_id}: {run.target_start}~{run.target_end} "
            f"status={run.status.value} 已完成 chunk 组数={len(chunks)}",
            flush=True,
        )
    print("[smoke] 全部通过:可安全执行完整回补", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
