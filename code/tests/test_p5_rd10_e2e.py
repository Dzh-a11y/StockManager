"""P5-RD-10 end-to-end pipeline: plan -> fetch -> stage -> verify -> publish -> gate.

Uses a deterministic in-memory provider so the whole DataSync reconstruction
pipeline runs fully offline, mirroring the plan's §14.6 "断网红线".
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
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
    SyncTask,
    SyncTaskStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.committer import GenerationCommitter, ReadinessGate
from stock_manager.sync.planner import SyncPlanner
from stock_manager.sync.staging import StagingWriter
from stock_manager.sync.verifier import CoverageVerifier
from stock_manager.sync.worker import SerialFetchWorker

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAYS = (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3))
CODES = ("sh.600000", "sz.000001", "sh.600519")


class DeterministicProvider:
    """Offline provider with a fixed calendar/universe."""

    source_name = "deterministic"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_stocks(self, as_of: date) -> list[StockIdentity]:
        self.calls.append("stocks")
        return [
            StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES
        ]

    def fetch_daily_bars(
        self,
        codes: list[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> list[DailyBar]:
        self.calls.append("daily_bars")
        bars: list[DailyBar] = []
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
            from datetime import timedelta

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


class TestEndToEndPipeline:
    def test_bootstrap_pipeline(self, repo: SQLiteRepository) -> None:
        provider = DeterministicProvider()
        planner = SyncPlanner(
            calendar=lambda start, end: tuple(d for d in DAYS if start <= d <= end),
            coverage=lambda adj, dt: (None, None),
            universe_codes=lambda day: CODES,
            now=lambda: NOW,
        )
        output = planner.plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            required_data_types=("daily_bars", "fundamentals"),
        )
        assert output.plan.task_count == 2  # 批量粒度:3 只代码各 1 个 batch × 2 类型

        # 持久化计划与任务
        repo.save_sync_plan(output.plan)
        repo.save_candidate_generation(output.candidate)
        for task in output.tasks:
            repo.save_sync_task(task)

        # 执行:worker → staging
        worker = SerialFetchWorker(
            provider, adjustment=AdjustmentMethod.QFQ, now=lambda: NOW
        )
        writer = StagingWriter(_factory(repo), now=lambda: NOW)
        writer.begin_candidate(output.candidate, "deterministic")
        candidate = repo.get_candidate_generation(output.candidate.candidate_generation_id)
        assert candidate is not None
        for task in output.tasks:
            result = worker.execute(task)
            rows = _rows_for(provider, task)
            writer.write_batch(
                candidate,
                task,
                rows,
                source="deterministic",
                adjustment=AdjustmentMethod.QFQ,
            )
            done = SyncTask(
                task_id=task.task_id,
                plan_id=task.plan_id,
                sequence_no=task.sequence_no,
                data_type=task.data_type,
                partition_key=task.partition_key,
                codes=task.codes,
                range_start=task.range_start,
                range_end=task.range_end,
                dependencies=task.dependencies,
                status=SyncTaskStatus.SUCCESS,
                attempt_count=1,
                not_before=None,
                row_count=result.row_count,
                error_code=None,
                error_message=None,
                started_at=NOW,
                finished_at=result.finished_at,
            )
            repo.update_sync_task_status(done)
        writer.finish_candidate(candidate)

        # 验证
        candidate = repo.get_candidate_generation(output.candidate.candidate_generation_id)
        assert candidate is not None
        verifier = CoverageVerifier(
            _factory(repo),
            trading_days=lambda start, end: tuple(DAYS),
            expected_universe_size=lambda day: len(CODES),
        )
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            tasks=output.tasks,
        )
        assert outcome.report.issues == ()
        for record in outcome.records:
            repo.save_coverage_verification(record)
        repo.update_candidate_status(
            candidate.candidate_generation_id,
            CandidateGenerationStatus.VERIFIED,
            NOW,
        )
        verified = repo.get_candidate_generation(candidate.candidate_generation_id)
        assert verified is not None

        # 发布
        committer = GenerationCommitter(_factory(repo), now=lambda: NOW)
        partitions = []
        for batch in repo.list_ingest_batches(verified.candidate_generation_id):
            partitions.append(
                type("P", (), {
                    "generation": verified.candidate_generation_id,
                    "data_type": batch.data_type,
                    "partition_key": batch.partition_key,
                    "batch_id": batch.batch_id,
                })()
            )
        # 用真实 domain 类型重建 partitions
        from stock_manager.domain import GenerationPartition

        partitions = [
            GenerationPartition(
                generation=verified.candidate_generation_id,
                data_type=batch.data_type,
                partition_key=batch.partition_key,
                batch_id=batch.batch_id,
            )
            for batch in repo.list_ingest_batches(verified.candidate_generation_id)
        ]
        published = committer.publish(
            verified,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            verifications=outcome.records,
            partitions=partitions,
        )
        assert published.generation == verified.candidate_generation_id

        # 门禁:READY 覆盖请求;超出范围拒绝
        gate = ReadinessGate(_factory(repo))
        ready = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars", "fundamentals"),
            requested_start=DAYS[0],
            requested_end=DAYS[-1],
        )
        assert ready.status is ReadinessStatus.READY
        assert ready.generation == verified.candidate_generation_id
        out_of_range = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars",),
            requested_start=date(2026, 8, 1),
            requested_end=date(2026, 8, 31),
        )
        assert out_of_range.status is ReadinessStatus.OUT_OF_RANGE

        # 断网红线:全部组件无 Provider 访问路径
        assert provider.calls  # worker 已调用
        screening = repo.get_daily_bars(
            CODES, DAYS[0], DAYS[-1], AdjustmentMethod.QFQ
        )
        assert len(screening) == 9  # 3 天 × 3 只

    def test_publish_failure_keeps_old_active(self, repo: SQLiteRepository) -> None:
        """重复发布失败(candidate 已被改状态)不改变 active 指针。"""
        from stock_manager.domain import (
            CoverageVerification,
            GenerationPartition,
            VerificationStatus,
        )

        candidate_id = "cand-x"
        repo.save_candidate_generation(
            type("C", (), {
                "candidate_generation_id": candidate_id,
                "plan_id": "p",
                "parent_generation": None,
                "write_revision": 1,
                "status": CandidateGenerationStatus.VERIFIED,
                "created_at": NOW,
                "updated_at": NOW,
            })()
        )
        verification = CoverageVerification(
            candidate_generation_id=candidate_id,
            data_type="daily_bars",
            partition_key=DAYS[0].isoformat(),
            expected_count=1,
            actual_count=1,
            distinct_count=1,
            duplicate_count=0,
            invalid_count=0,
            coverage_ratio=Decimal("1"),
            missing_items=(),
            status=VerificationStatus.COMPLETE,
            verified_revision=1,
            manifest_sha256="m" * 64,
            verified_at=NOW,
            details_json="{}",
        )
        repo.save_coverage_verification(verification)
        # 竞态:提交前状态被改成 NEEDS_REPAIR
        repo.update_candidate_status(
            candidate_id, CandidateGenerationStatus.NEEDS_REPAIR, NOW
        )
        committer = GenerationCommitter(_factory(repo), now=lambda: NOW)
        from stock_manager.sync.committer import PublishError

        with pytest.raises(PublishError):
            committer.publish(
                _candidate(candidate_id),
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(verification,),
                partitions=(
                    GenerationPartition(candidate_id, "daily_bars", DAYS[0].isoformat(), "b1"),
                ),
            )
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None


def _candidate(candidate_id: str):
    from stock_manager.domain import CandidateGeneration

    return CandidateGeneration(
        candidate_generation_id=candidate_id,
        plan_id="p",
        parent_generation=None,
        write_revision=1,
        status=CandidateGenerationStatus.VERIFIED,
        created_at=NOW,
        updated_at=NOW,
    )


def _rows_for(provider: DeterministicProvider, task: SyncTask):
    """Replay provider rows for a task (deterministic offline replay)."""
    if task.data_type == "stocks":
        return provider.fetch_stocks(task.range_end)
    if task.data_type == "daily_bars":
        return provider.fetch_daily_bars(
            list(task.codes), task.range_start, task.range_end, AdjustmentMethod.QFQ
        )
    if task.data_type == "fundamentals":
        return provider.fetch_fundamentals(list(task.codes), task.range_end)
    if task.data_type == "dividends":
        return provider.fetch_dividends(list(task.codes), task.range_start, task.range_end)
    raise AssertionError(f"unknown data type {task.data_type}")
