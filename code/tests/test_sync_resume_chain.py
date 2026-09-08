"""Offline tests for startup_sync's frozen-window resume chain (方案 A).

Covers the SYNC_RESUME_CHAIN_PLAN acceptance tests:
- T1 跨日续传 + 追平:interrupted incremental window is finished (frozen)
  before the moved target is caught up, SUCCESS batches never re-fetched;
- T2 陈旧已覆盖 FAILED 计划跳过不续;
- T3 数据已最新重入:返回跳过结果、零 Provider 调用(含交易日历跳过);
- T4/T5 显式重试与冷却:FAILED 在途计划 retry_failed=False 抛
  RetryRequiredError,冷却内续传抛 RetryCooldownError,冷却后恢复;
- T6 REPAIR/LEGACY_IMPORT 计划不被 startup 链自动续;
- T7 首启 BOOTSTRAP 中断后跨日:先续旧 BOOTSTRAP 再 INCREMENTAL 追平。

全部离线(fixture provider,固定日历),禁止联网。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as wall_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGenerationStatus,
    DailyBar,
    FundamentalSnapshot,
    StockIdentity,
    SyncPlanMode,
    SyncPlanStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.data_sync_service import (
    DataSyncService,
    RetryRequiredError,
    SyncConfig,
    SyncHistoryConfig,
)
from stock_manager.sync.pipeline import PipelineError, PipelineRun

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 固定交易日历(跳过 2026-09-05/06 周末):让按交易日生成的 bar 与日历严格一致。
TRADING = (
    date(2026, 9, 1),
    date(2026, 9, 2),
    date(2026, 9, 3),
    date(2026, 9, 4),
    date(2026, 9, 7),
    date(2026, 9, 8),
)
CODES = ("sh.600000", "sh.600519", "sz.000001")


class NetworkDropError(RuntimeError):
    """Simulated provider network drop."""


class ChainProvider:
    """Fixture provider with call log and a one-shot daily-bar failure point."""

    source_name = "fixture"

    def __init__(self) -> None:
        self.trading_calls = 0
        self.stock_calls = 0
        self.fund_calls = 0
        self.daily_calls: list[tuple[tuple[str, ...], date, date]] = []
        self.fail_on_daily_call: int | None = None

    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        self.trading_calls += 1
        return [day for day in TRADING if start <= day <= end]

    def fetch_stocks(self, as_of: date) -> list[StockIdentity]:
        self.stock_calls += 1
        return [StockIdentity(code, "股票", "SSE", False, None, None) for code in CODES]

    def fetch_daily_bars(
        self,
        codes: list[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> list[DailyBar]:
        self.daily_calls.append((tuple(codes), start, end))
        if (
            self.fail_on_daily_call is not None
            and len(self.daily_calls) >= self.fail_on_daily_call
        ):
            raise NetworkDropError("simulated network drop")
        rows: list[DailyBar] = []
        for day in TRADING:
            if start <= day <= end:
                for code in codes:
                    rows.append(
                        DailyBar(
                            code, day, Decimal("10"), Decimal("11"), Decimal("9"),
                            Decimal("10.5"), Decimal("10"), Decimal("1000"),
                            Decimal("10500"), True,
                        )
                    )
        return rows

    def fetch_fundamentals(
        self, codes: list[str], as_of: date
    ) -> list[FundamentalSnapshot]:
        self.fund_calls += 1
        return [
            FundamentalSnapshot(code, as_of, as_of, Decimal("8"), Decimal("1"), "fixture")
            for code in codes
        ]


def make_env(
    tmp_path: Path,
    *,
    cooldown: timedelta = timedelta(seconds=10),
) -> tuple[DataSyncService, ChainProvider, SQLiteRepository, list[datetime]]:
    """Repository + provider + config-bound service sharing a mutable clock."""
    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    provider = ChainProvider()
    config = SyncConfig(
        cutoff_time=wall_time(9, 0),
        retry_cooldown=cooldown,
        minimum_request_interval_seconds=0.0,
        calendar_horizon_days=45,
        dividend_lookback_years=1,
        retention_days=360,
        history=SyncHistoryConfig(target_years=8),
        pipeline_default=True,
    )
    clock: list[datetime] = [datetime(2026, 9, 1, 18, 0, tzinfo=SHANGHAI)]
    service = DataSyncService(
        provider, repo, tmp_path / "locks", config, clock=lambda: clock[0]
    )
    return service, provider, repo, clock


def _bar_days_codes(daily_calls: list[tuple[tuple[str, ...], date, date]]) -> set[Any]:
    """Normalize daily fetch log into {(code, start, end)} for exact assertions."""
    return {
        (code, start, end)
        for codes, start, end in daily_calls
        for code in codes
    }


def _succeed_through(
    service: DataSyncService,
    provider: ChainProvider,
    clock: list[datetime],
    day: date,
) -> PipelineRun:
    """Drive startup_sync to a clean published coverage through ``day``."""
    clock[0] = datetime(day.year, day.month, day.day, 18, 0, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = None
    run = service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    assert isinstance(run, PipelineRun)
    assert run.published
    return run


def _mark_plan_failed(service: DataSyncService, plan_id: str) -> None:
    plan = service._repository.get_sync_plan(plan_id)
    assert plan is not None
    now = plan.updated_at  # 不早于 created_at,满足域校验
    service._repository.update_sync_plan_status(plan_id, SyncPlanStatus.FAILED, now)
    service._repository.update_candidate_status(
        plan.candidate_generation_id, CandidateGenerationStatus.FAILED, now
    )


# ---------------------------------------------------------------------------
# T1: 增量中断跨日重开 —— 冻结旧窗先续,发布后再追平;SUCCESS 批次零重拉
# ---------------------------------------------------------------------------


def test_interrupted_incremental_resumes_frozen_window_then_catches_up(
    tmp_path: Path,
) -> None:
    service, provider, repo, clock = make_env(tmp_path)
    # 阶段 A:干净同步到 09-01。
    _succeed_through(service, provider, clock, date(2026, 9, 1))
    calls_before = list(provider.daily_calls)
    base = len(calls_before)
    assert base == len(CODES)  # bootstrap 09-01, batch=1 → 每码 1 任务

    # 阶段 B:09-02 增量在第 3 次日线请求(第 3 只代码)掉线 → 计划 FAILED。
    clock[0] = datetime(2026, 9, 2, 18, 0, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = base + len(CODES)
    with pytest.raises(PipelineError, match="network drop"):
        service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    phase_b_end = len(provider.daily_calls)
    failed_plans = [
        plan for plan in repo.list_sync_plans("market", AdjustmentMethod.QFQ)
        if plan.status is SyncPlanStatus.FAILED
    ]
    assert len(failed_plans) == 1
    assert failed_plans[0].target_end == date(2026, 9, 2)
    success_codes = {
        codes[0]
        for codes, start, end in provider.daily_calls[base:phase_b_end]
        if codes[0] != CODES[-1]
    }
    assert len(success_codes) == len(CODES) - 1  # 前两只代码的批次已 SUCCESS

    # 阶段 C:09-04 才重开 → 先续旧窗(09-01..09-02)发布,再追平(09-02..09-04)。
    clock[0] = datetime(2026, 9, 4, 18, 0, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = None
    run = service.startup_sync(
        "market", AdjustmentMethod.QFQ, batch_size=1, retry_failed=True
    )
    assert isinstance(run, PipelineRun)
    assert run.published

    plans = repo.list_sync_plans("market", AdjustmentMethod.QFQ)
    succeeded = sorted(
        (plan.mode, plan.target_end)
        for plan in plans
        if plan.status is SyncPlanStatus.SUCCEEDED
    )
    assert (SyncPlanMode.BOOTSTRAP, date(2026, 9, 1)) in succeeded
    incremental_ends = sorted(
        target for mode, target in succeeded if mode is SyncPlanMode.INCREMENTAL
    )
    assert incremental_ends == [date(2026, 9, 2), date(2026, 9, 4)]
    coverage_end = repo.actual_coverage(AdjustmentMethod.QFQ, "daily_bars")[1]
    assert coverage_end == date(2026, 9, 4)

    # 零重拉断言:阶段 C 只新增 1 次旧窗失败批重拉 + 1 次新窗(09-03..09-04)全码拉取;
    # 旧窗(09-02)已 SUCCESS 的代码批次(前两只)绝不重拉。
    phase_c = provider.daily_calls[phase_b_end:]
    fetched = _bar_days_codes(phase_c)
    assert fetched == {
        (CODES[-1], date(2026, 9, 2), date(2026, 9, 2)),  # 掉线批重拉
        (CODES[0], date(2026, 9, 3), date(2026, 9, 4)),
        (CODES[1], date(2026, 9, 3), date(2026, 9, 4)),
        (CODES[2], date(2026, 9, 3), date(2026, 9, 4)),
    }
    assert not any(
        (code, date(2026, 9, 2), date(2026, 9, 2)) in fetched
        for code in CODES[:-1]
    ), "旧窗已 SUCCESS 批次被重拉"
    assert all(plan.status is not SyncPlanStatus.FAILED for plan in plans)


# ---------------------------------------------------------------------------
# T2: 窗口已被更宽已发布数据覆盖的陈旧 FAILED 计划跳过不续
# ---------------------------------------------------------------------------


def test_superseded_failed_plan_is_skipped(tmp_path: Path) -> None:
    service, provider, repo, clock = make_env(tmp_path)
    _succeed_through(service, provider, clock, date(2026, 9, 4))

    # 伪造一个失败的在途增量计划,其窗口(09-03..09-04)已完全落在发布数据(到 09-04)内。
    from stock_manager.sync.pipeline import SyncPipeline

    pipeline = service.build_pipeline()
    assert isinstance(pipeline, SyncPipeline)
    stale = pipeline.plan(
        mode=SyncPlanMode.INCREMENTAL,
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=date(2026, 9, 3),
        target_end=date(2026, 9, 4),
        required_data_types=("stocks", "daily_bars", "fundamentals"),
        batch_size=1,
    )
    _mark_plan_failed(service, stale.plan.plan_id)

    daily_before = list(provider.daily_calls)
    trading_before = provider.trading_calls
    run = service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    # 数据已最新:返回跳过结果(SUCCEEDED + warning),而不是续陈旧计划。
    assert isinstance(run, PipelineRun)
    assert run.plan_status is SyncPlanStatus.SUCCEEDED
    assert run.published is False
    assert run.warning is not None
    assert provider.daily_calls == daily_before  # 陈旧计划没有被续、没有整窗重拉
    assert provider.trading_calls == trading_before  # 交易日历也未重取
    plan = repo.get_sync_plan(stale.plan.plan_id)
    assert plan is not None
    assert plan.status is SyncPlanStatus.FAILED  # 留档不续


# ---------------------------------------------------------------------------
# T3: 数据已最新重入 → 跳过结果 + 零 Provider 调用
# ---------------------------------------------------------------------------


def test_already_latest_returns_skip_with_zero_provider_calls(tmp_path: Path) -> None:
    service, provider, repo, clock = make_env(tmp_path)
    _succeed_through(service, provider, clock, date(2026, 9, 4))

    daily_before = list(provider.daily_calls)
    stock_before = provider.stock_calls
    fund_before = provider.fund_calls
    trading_before = provider.trading_calls

    # 同一“已完成交易日”(clock 不变)再次启动 → 直接跳过,零 Provider 调用。
    run = service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    assert isinstance(run, PipelineRun)
    assert run.plan_status is SyncPlanStatus.SUCCEEDED
    assert run.published is False
    assert run.warning is not None
    assert provider.daily_calls == daily_before
    assert provider.stock_calls == stock_before
    assert provider.fund_calls == fund_before
    assert provider.trading_calls == trading_before
    assert repo.actual_coverage(AdjustmentMethod.QFQ, "daily_bars")[1] == date(2026, 9, 4)


# ---------------------------------------------------------------------------
# T4/T5: FAILED 在途计划需显式重试;冷却内续传被拒;冷却后恢复
# ---------------------------------------------------------------------------


def test_failed_pending_requires_explicit_retry_and_respects_cooldown(
    tmp_path: Path,
) -> None:
    service, provider, repo, clock = make_env(tmp_path)
    _succeed_through(service, provider, clock, date(2026, 9, 1))

    clock[0] = datetime(2026, 9, 2, 18, 0, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = len(provider.daily_calls) + 1
    with pytest.raises(PipelineError, match="network drop"):
        service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)

    # retry_failed=False → 显式重试要求,不自动续。
    with pytest.raises(RetryRequiredError, match="explicit retry"):
        service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)

    # retry_failed=True 但仍在冷却内 → RetryCooldownError。
    from stock_manager.sync.pipeline import RetryCooldownError

    with pytest.raises(RetryCooldownError):
        service.startup_sync(
            "market", AdjustmentMethod.QFQ, batch_size=1, retry_failed=True
        )

    # 冷却过后(同一目标窗口)→ 续传成功发布。
    clock[0] = datetime(2026, 9, 2, 18, 30, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = None
    run = service.startup_sync(
        "market", AdjustmentMethod.QFQ, batch_size=1, retry_failed=True
    )
    assert isinstance(run, PipelineRun)
    assert run.published
    assert repo.actual_coverage(AdjustmentMethod.QFQ, "daily_bars")[1] == date(2026, 9, 2)


# ---------------------------------------------------------------------------
# T6: REPAIR 计划不会被 startup 链自动续传
# ---------------------------------------------------------------------------


def test_repair_plan_is_never_auto_resumed(tmp_path: Path) -> None:
    service, provider, repo, clock = make_env(tmp_path)
    _succeed_through(service, provider, clock, date(2026, 9, 1))

    # 伪造一个 REPAIR 计划(窗口 09-01..09-03 超出当前已发布覆盖 09-01),并标记 FAILED。
    pipeline = service.build_pipeline()
    repair = pipeline.plan(
        mode=SyncPlanMode.INCREMENTAL,
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=date(2026, 9, 1),
        target_end=date(2026, 9, 3),
        required_data_types=("stocks", "daily_bars", "fundamentals"),
        batch_size=1,
    )
    _mark_plan_failed(service, repair.plan.plan_id)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute(
            "UPDATE sync_plans SET mode = ? WHERE plan_id = ?",
            (SyncPlanMode.REPAIR.value, repair.plan.plan_id),
        )
    repair_plan = repo.get_sync_plan(repair.plan.plan_id)
    assert repair_plan is not None
    assert repair_plan.mode is SyncPlanMode.REPAIR

    # 09-02 启动:REPAIR 计划不被自动续,直接规划并执行正常增量尾差(09-01..09-02)。
    clock[0] = datetime(2026, 9, 2, 18, 0, tzinfo=SHANGHAI)
    daily_before = list(provider.daily_calls)
    run = service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    assert isinstance(run, PipelineRun)
    assert run.published
    repair_plan = repo.get_sync_plan(repair.plan.plan_id)
    assert repair_plan is not None
    assert repair_plan.status is SyncPlanStatus.FAILED  # REPAIR 计划未被触碰
    assert repo.actual_coverage(AdjustmentMethod.QFQ, "daily_bars")[1] == date(2026, 9, 2)
    # 尾差只拉 (09-01..09-02),没有为 REPAIR 的 (09-01..09-03) 或任何后续窗口拉数据。
    new_fetches = provider.daily_calls[len(daily_before):]
    assert len(new_fetches) == len(CODES)
    assert all(end == date(2026, 9, 2) for _codes, _start, end in new_fetches)


# ---------------------------------------------------------------------------
# T7: 首启 BOOTSTRAP 中断后跨日重开 —— 先续旧 BOOTSTRAP 再 INCREMENTAL 追平
# ---------------------------------------------------------------------------


def test_fresh_bootstrap_interrupted_then_catches_up(tmp_path: Path) -> None:
    service, provider, repo, clock = make_env(tmp_path)
    # 首启(无 active generation)在第一次日线请求即掉线。
    clock[0] = datetime(2026, 9, 2, 18, 0, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = 1
    with pytest.raises(PipelineError, match="network drop"):
        service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None

    # 09-04 重开:续旧 BOOTSTRAP(冻结窗口)发布,再 INCREMENTAL 追平到 09-04。
    clock[0] = datetime(2026, 9, 4, 18, 0, tzinfo=SHANGHAI)
    provider.fail_on_daily_call = None
    run = service.startup_sync(
        "market", AdjustmentMethod.QFQ, batch_size=1, retry_failed=True
    )
    assert isinstance(run, PipelineRun)
    assert run.published

    plans = repo.list_sync_plans("market", AdjustmentMethod.QFQ)
    succeeded = sorted(
        (plan.mode, plan.target_end)
        for plan in plans
        if plan.status is SyncPlanStatus.SUCCEEDED
    )
    assert (SyncPlanMode.BOOTSTRAP, date(2026, 9, 2)) in succeeded  # 旧窗冻结,未扩成 09-04
    assert (SyncPlanMode.INCREMENTAL, date(2026, 9, 4)) in succeeded  # 追平到最新
    assert repo.actual_coverage(AdjustmentMethod.QFQ, "daily_bars")[1] == date(2026, 9, 4)


# ---------------------------------------------------------------------------
# T8: P5 发布后自动把发布日登记进 dataset_metadata（注册快照日），
#     已发布但缺登记行的旧库在下次“数据已最新”启动时自愈（零 Provider 调用）。
# ---------------------------------------------------------------------------


def test_incremental_publish_registers_snapshot_day_in_metadata(
    tmp_path: Path,
) -> None:
    """增量 P5 发布(已有 active generation)后，market/qfq 的 dataset_metadata
    自动登记新快照日。

    回归：发布走 GenerationCommitter 只写 generation 表、不写 legacy 注册表，
    导致 registered_days / as_of 候选缺少新发布日(真实库 09-07 缺行)。BOOTSTRAP
    首启因 universe 预写(service 内 save_stocks)碰巧有行,这里用纯增量路径复现。
    """
    service, provider, repo, clock = make_env(tmp_path)
    _succeed_through(service, provider, clock, date(2026, 9, 1))  # BOOTSTRAP 09-01
    _succeed_through(service, provider, clock, date(2026, 9, 4))  # INCREMENTAL 追平
    metadata = repo.get_dataset_metadata(
        "market", date(2026, 9, 4), AdjustmentMethod.QFQ
    )
    assert metadata is not None
    assert metadata.trading_day == date(2026, 9, 4)
    days = [
        m.trading_day
        for m in repo.list_dataset_metadata("market", AdjustmentMethod.QFQ)
    ]
    assert date(2026, 9, 4) in days


def test_already_latest_heals_missing_registered_day_without_provider(
    tmp_path: Path,
) -> None:
    """已发布但未登记（旧代码产物）时，下次启动自愈补登记，且零 Provider 调用。"""
    service, provider, repo, clock = make_env(tmp_path)
    _succeed_through(service, provider, clock, date(2026, 9, 4))
    # 模拟旧代码产物：数据已发布但没有 dataset_metadata 登记行。
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute(
            "DELETE FROM dataset_metadata WHERE dataset_id = ? AND adjustment = ?",
            ("market", AdjustmentMethod.QFQ.value),
        )
    assert (
        repo.get_dataset_metadata("market", date(2026, 9, 4), AdjustmentMethod.QFQ)
        is None
    )

    daily_before = list(provider.daily_calls)
    stock_before = provider.stock_calls
    fund_before = provider.fund_calls
    trading_before = provider.trading_calls
    run = service.startup_sync("market", AdjustmentMethod.QFQ, batch_size=1)
    assert isinstance(run, PipelineRun)
    assert run.published is False
    assert run.warning is not None  # 数据已最新，跳过拉取
    # 自愈只做本地登记，不触碰 Provider。
    assert provider.daily_calls == daily_before
    assert provider.stock_calls == stock_before
    assert provider.fund_calls == fund_before
    assert provider.trading_calls == trading_before
    metadata = repo.get_dataset_metadata(
        "market", date(2026, 9, 4), AdjustmentMethod.QFQ
    )
    assert metadata is not None
    assert metadata.trading_day == date(2026, 9, 4)
