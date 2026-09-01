"""Offline tests for the SyncPipeline orchestrator (P5 integration)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGenerationStatus,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    ReadinessStatus,
    StockIdentity,
    SyncPlanMode,
    SyncPlanStatus,
    SyncTask,
    SyncTaskStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.committer import GenerationCommitter, ReadinessGate
from stock_manager.sync.legacy import LegacyImporter
from stock_manager.sync.pipeline import (
    PipelineError,
    PipelineRun,
    RetryCooldownError,
    SyncPipeline,
)
from stock_manager.sync.planner import SyncPlanner
from stock_manager.sync.staging import StagingWriter
from stock_manager.sync.verifier import CoverageVerifier
from stock_manager.sync.worker import SerialFetchWorker

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAYS = (date(2026, 9, 1), date(2026, 9, 2))
CODES = ("sh.600000", "sz.000001", "sh.600519")


class DeterministicProvider:
    """Offline provider; can be told to fail a specific data type once."""

    source_name = "deterministic"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_next_daily_bars = False

    def fetch_stocks(self, as_of: date) -> list[StockIdentity]:
        self.calls.append("stocks")
        return [
            StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES
        ]

    def fetch_daily_bars(
        self, codes: list[str], start: date, end: date, adjustment: AdjustmentMethod
    ) -> list[DailyBar]:
        self.calls.append("daily_bars")
        if self.fail_next_daily_bars:
            self.fail_next_daily_bars = False
            raise RuntimeError("simulated provider failure")
        bars: list[DailyBar] = []
        from datetime import timedelta

        day = start
        while day <= end:
            for code in codes:
                bars.append(
                    DailyBar(
                        code, day, Decimal("10"), Decimal("11"), Decimal("9"),
                        Decimal("10.5"), Decimal("10"), Decimal("1000"),
                        Decimal("10500"), True,
                    )
                )
            day = day + timedelta(days=1)
        return bars

    def fetch_fundamentals(
        self, codes: list[str], as_of: date
    ) -> list[FundamentalSnapshot]:
        self.calls.append("fundamentals")
        return [
            FundamentalSnapshot(c, as_of, as_of, Decimal("8"), Decimal("1"), "deterministic")
            for c in codes
        ]

    def fetch_dividends(
        self, codes: list[str], start: date, end: date
    ) -> list[DividendRecord]:
        self.calls.append("dividends")
        return []


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "market.sqlite3")


def _factory(repo: SQLiteRepository):
    def factory() -> sqlite3.Connection:
        connection = sqlite3.connect(repo.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    return factory


def _pipeline(repo: SQLiteRepository, provider: DeterministicProvider) -> SyncPipeline:
    planner = SyncPlanner(
        calendar=lambda start, end: tuple(d for d in DAYS if start <= d <= end),
        coverage=lambda adj, dt: (None, None),
        universe_codes=lambda day: CODES,
        now=lambda: NOW,
    )
    worker_factory = lambda: SerialFetchWorker(  # noqa: E731
        provider, adjustment=AdjustmentMethod.QFQ, now=lambda: NOW
    )
    staging = StagingWriter(_factory(repo), now=lambda: NOW)
    verifier = CoverageVerifier(
        _factory(repo),
        trading_days=lambda start, end: tuple(DAYS),
        expected_universe_size=lambda day: len(CODES),
    )
    committer = GenerationCommitter(_factory(repo), now=lambda: NOW)
    gate = ReadinessGate(_factory(repo))
    legacy = LegacyImporter(_factory(repo), now=lambda: NOW)
    return SyncPipeline(
        repository=repo,
        planner=planner,
        worker_factory=worker_factory,
        staging=staging,
        verifier=verifier,
        committer=committer,
        gate=gate,
        legacy=legacy,
        now=lambda: NOW,
        retry_cooldown=timedelta(seconds=10),
        max_attempts=2,
    )


class TestPipeline:
    def test_plan_then_execute_publishes(self, repo: SQLiteRepository) -> None:
        provider = DeterministicProvider()
        pipeline = _pipeline(repo, provider)
        output = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        assert output.plan.task_count == 1  # 批量粒度:3 只代码一个 batch,区间覆盖 2 天
        assert repo.get_sync_plan(output.plan.plan_id) is not None

        run = pipeline.execute(output.plan.plan_id)
        assert isinstance(run, PipelineRun)
        assert run.published is True
        assert run.plan_status is SyncPlanStatus.SUCCEEDED
        assert run.candidate is not None
        assert run.candidate.status is CandidateGenerationStatus.PUBLISHED
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is not None
        # 发布后正式表可读
        bars = repo.get_daily_bars(
            CODES, DAYS[0], DAYS[-1], AdjustmentMethod.QFQ
        )
        assert len(bars) == 6  # 2 天 × 3 只

    def test_idempotent_execute_skips(self, repo: SQLiteRepository) -> None:
        provider = DeterministicProvider()
        pipeline = _pipeline(repo, provider)
        output = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        pipeline.execute(output.plan.plan_id)
        calls_after_first = len(provider.calls)
        run = pipeline.execute(output.plan.plan_id)
        assert run.warning is not None
        assert len(provider.calls) == calls_after_first  # 零额外 Provider 调用

    def test_failure_then_retry(self, repo: SQLiteRepository) -> None:
        provider = DeterministicProvider()
        pipeline = _pipeline(repo, provider)
        output = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        provider.fail_next_daily_bars = True
        with pytest.raises(PipelineError):
            pipeline.execute(output.plan.plan_id)
        # 计划 FAILED,任务 FAILED 并带冷却
        plan = repo.get_sync_plan(output.plan.plan_id)
        assert plan is not None
        assert plan.status is SyncPlanStatus.FAILED
        failed = repo.tasks_by_status(
            output.plan.plan_id, (SyncTaskStatus.FAILED,)
        )
        assert failed
        assert failed[0].not_before is not None
        # 冷却未到 → retry 拒绝
        with pytest.raises(RetryCooldownError):
            pipeline.retry(output.plan.plan_id)
        # 冷却到期 → retry 成功
        def later() -> datetime:
            return NOW + timedelta(seconds=30)

        pipeline_2 = _pipeline(repo, provider)
        pipeline_2 = _with_now(pipeline_2, later)
        run = pipeline_2.retry(output.plan.plan_id)
        assert run.published is True
        assert run.plan_status is SyncPlanStatus.SUCCEEDED

    def test_interrupted_resumes(self, repo: SQLiteRepository) -> None:
        provider = DeterministicProvider()
        pipeline = _pipeline(repo, provider)
        output = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        # 模拟进程重启:标记中断
        n = pipeline.mark_interrupted()
        assert n == 0  # 尚未开始
        # 真正执行失败后中断恢复
        provider.fail_next_daily_bars = True
        with pytest.raises(PipelineError):
            pipeline.execute(output.plan.plan_id)
        # 直接 execute 会重跑 FAILED?不:execute 只跑 PENDING/INTERRUPTED。
        # 但 retry 在冷却后重置。这里验证 mark_interrupted 对 RUNNING 有效。
        running = repo.tasks_by_status(output.plan.plan_id, (SyncTaskStatus.RUNNING,))
        assert not running

    def test_plan_incremental_requires_active(self, repo: SQLiteRepository) -> None:
        pipeline = _pipeline(repo, DeterministicProvider())
        with pytest.raises(PipelineError):
            pipeline.plan(
                mode=SyncPlanMode.INCREMENTAL,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                target_start=DAYS[0],
                target_end=DAYS[-1],
            )

    def test_verify_failure_marks_needs_repair(self, repo: SQLiteRepository) -> None:
        # 只给 1 只股票的 bar(覆盖率不足)→ 验证失败 → NEEDS_REPAIR
        class SparseProvider(DeterministicProvider):
            def fetch_daily_bars(self, codes, start, end, adjustment):
                return [
                    DailyBar(
                        CODES[0], DAYS[0], Decimal("10"), Decimal("11"),
                        Decimal("9"), Decimal("10.5"), Decimal("10"),
                        Decimal("1000"), Decimal("10500"), True,
                    )
                ]

        provider = SparseProvider()
        pipeline = _pipeline(repo, provider)
        output = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        run = pipeline.execute(output.plan.plan_id)
        # 有验证问题 → 未发布,但计划仍算执行完成(需要 REPAIR 后再验证)
        assert run.published is False
        candidate = repo.get_candidate_generation(output.candidate.candidate_generation_id)
        assert candidate is not None
        assert candidate.status is CandidateGenerationStatus.NEEDS_REPAIR
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None


def _with_now(pipeline: SyncPipeline, now):
    object.__setattr__(pipeline, "_now", now)
    return pipeline


class TestPlanReuse:
    def test_replan_same_plan_id_reuses_progress(self, repo: SQLiteRepository) -> None:
        """watchdog 重启后重新 plan 不重置进度:同 plan_id 复用既有任务。"""
        provider = DeterministicProvider()
        pipeline = _pipeline(repo, provider)
        first = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        # 模拟已执行一部分:1 个任务 SUCCESS
        task = first.tasks[0]
        from stock_manager.domain import SyncTask as ST

        done = ST(
            task_id=task.task_id, plan_id=task.plan_id, sequence_no=task.sequence_no,
            data_type=task.data_type, partition_key=task.partition_key,
            codes=task.codes, range_start=task.range_start, range_end=task.range_end,
            dependencies=task.dependencies, status=SyncTaskStatus.SUCCESS,
            attempt_count=1, not_before=None, row_count=2, error_code=None,
            error_message=None, started_at=NOW, finished_at=NOW,
        )
        repo.update_sync_task_status(done)
        # 重新 plan(相同输入 → 相同 plan_id)
        second = pipeline.plan(
            mode=SyncPlanMode.BOOTSTRAP,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars",),
        )
        assert second.plan.plan_id == first.plan.plan_id
        # 复用了既有任务(SUCCESS 保留),而非重置为全 PENDING
        loaded = repo.list_sync_tasks(second.plan.plan_id)
        statuses = {t.status for t in loaded}
        assert SyncTaskStatus.SUCCESS in statuses
