"""Offline tests for P5-RD-5: CoverageVerifier."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    DailyBar,
    FundamentalSnapshot,
    IssueType,
    Repairability,
    StockIdentity,
    SyncTask,
    SyncTaskStatus,
    VerificationStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.staging import StagingWriter
from stock_manager.sync.verifier import (
    CoverageVerifier,
    VerificationError,
    VerificationOutcome,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)
CODES = ("sh.600000", "sz.000001", "sh.600519", "sz.300750")


def _candidate(**overrides: object) -> CandidateGeneration:
    values: dict[str, object] = {
        "candidate_generation_id": "cand-1",
        "plan_id": "plan-1",
        "parent_generation": None,
        "write_revision": 2,
        "status": CandidateGenerationStatus.VERIFYING,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return CandidateGeneration(**values)  # type: ignore[arg-type]


def _task(data_type: str = "daily_bars", **overrides: object) -> SyncTask:
    values: dict[str, object] = {
        "task_id": "t1",
        "plan_id": "plan-1",
        "sequence_no": 0,
        "data_type": data_type,
        "partition_key": DAY.isoformat(),
        "codes": CODES,
        "range_start": DAY,
        "range_end": DAY,
        "dependencies": (),
        "status": SyncTaskStatus.SUCCESS,
        "attempt_count": 1,
        "not_before": None,
        "row_count": 4,
        "error_code": None,
        "error_message": None,
        "started_at": NOW,
        "finished_at": NOW,
    }
    values.update(overrides)
    return SyncTask(**values)  # type: ignore[arg-type]


def _bar(code: str, *, high: str = "11", low: str = "9") -> DailyBar:
    return DailyBar(
        code, DAY, Decimal("10"), Decimal(high), Decimal(low), Decimal("10.5"),
        Decimal("10"), Decimal("1000"), Decimal("10500"), True,
    )


def _fund(code: str) -> FundamentalSnapshot:
    return FundamentalSnapshot(code, DAY, DAY, Decimal("8.5"), Decimal("0.9"), "fake")


def _stock(code: str) -> StockIdentity:
    return StockIdentity(code, "浦发银行", "SSE", False, None, None)


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "market.sqlite3")


@pytest.fixture()
def writer(repo: SQLiteRepository) -> StagingWriter:
    def factory() -> sqlite3.Connection:
        connection = sqlite3.connect(repo.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    return StagingWriter(factory, now=lambda: NOW)


@pytest.fixture()
def verifier(repo: SQLiteRepository) -> CoverageVerifier:
    def factory() -> sqlite3.Connection:
        connection = sqlite3.connect(repo.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    return CoverageVerifier(
        factory,
        trading_days=lambda start, end: (DAY,),
        expected_universe_size=lambda day: len(CODES),
    )


def _stage_bars(
    writer: StagingWriter,
    repo: SQLiteRepository,
    bars: list[DailyBar],
) -> CandidateGeneration:
    writer.begin_candidate(_candidate(status=CandidateGenerationStatus.PLANNED), "fake")
    candidate = repo.get_candidate_generation("cand-1")
    assert candidate is not None
    writer.write_batch(
        candidate, _task("daily_bars"), bars,
        source="fake", adjustment=AdjustmentMethod.QFQ,
    )
    writer.finish_candidate(candidate)
    loaded = repo.get_candidate_generation("cand-1")
    assert loaded is not None
    return loaded


class TestCoverageVerifier:
    def test_complete_daily_bars_no_issues(self, writer: StagingWriter, repo: SQLiteRepository, verifier: CoverageVerifier) -> None:
        candidate = _stage_bars(writer, repo, [_bar(c) for c in CODES])
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("daily_bars"),),
        )
        assert isinstance(outcome, VerificationOutcome)
        assert outcome.report.issues == ()
        assert len(outcome.records) == 1
        record = outcome.records[0]
        assert record.status is VerificationStatus.COMPLETE
        assert record.coverage_ratio == Decimal("1")
        assert record.verified_revision == candidate.write_revision

    def test_incomplete_daily_bars_95_boundary(self, writer: StagingWriter, repo: SQLiteRepository, verifier: CoverageVerifier) -> None:
        # 4 只中只写入 3 只 = 75% < 95% → INCOMPLETE + MISSING 问题
        candidate = _stage_bars(writer, repo, [_bar(c) for c in CODES[:3]])
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("daily_bars"),),
        )
        issues = outcome.report.issues
        assert len(issues) == 1
        assert issues[0].issue_type is IssueType.MISSING
        assert issues[0].repairability is Repairability.REFETCH
        assert issues[0].codes == ("sz.300750",)
        assert outcome.records[0].status is VerificationStatus.INCOMPLETE

    def test_invalid_bar_detected(self, repo: SQLiteRepository, verifier: CoverageVerifier) -> None:
        # 领域对象拒绝 high<low,模拟损坏数据必须直接注入 staging 表。
        from stock_manager.sync.staging import StagingWriter

        writer = StagingWriter(
            lambda: sqlite3.connect(repo.database_path), now=lambda: NOW
        )
        writer.begin_candidate(_candidate(status=CandidateGenerationStatus.PLANNED), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        writer.write_batch(
            candidate, _task("daily_bars"), [_bar(c) for c in CODES],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        with sqlite3.connect(repo.database_path) as connection:
            connection.execute(
                """UPDATE daily_bars_staging SET high = '5', low = '9'
                   WHERE code = ?""",
                (CODES[3],),
            )
            connection.commit()
        writer.finish_candidate(candidate)
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        outcome = verifier.verify(
            loaded,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("daily_bars"),),
        )
        types = {i.issue_type for i in outcome.report.issues}
        assert IssueType.INVALID in types

    def test_duplicate_detected(self, writer: StagingWriter, repo: SQLiteRepository, verifier: CoverageVerifier) -> None:
        # 同一 candidate 两个重叠批次(如 REPAIR 覆盖)在 staging 中产生重复行。
        writer.begin_candidate(_candidate(status=CandidateGenerationStatus.PLANNED), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        writer.write_batch(
            candidate, _task("daily_bars", codes=CODES[:2]), [_bar(c) for c in CODES[:2]],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        writer.write_batch(
            candidate, _task("daily_bars", codes=CODES[1:3], task_id="t2"),
            [_bar(c) for c in CODES[1:3]],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        writer.finish_candidate(candidate)
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        outcome = verifier.verify(
            loaded,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("daily_bars", codes=CODES),),
        )
        types = {i.issue_type for i in outcome.report.issues}
        assert IssueType.DUPLICATE in types
        dup = next(i for i in outcome.report.issues if i.issue_type is IssueType.DUPLICATE)
        assert dup.repairability is Repairability.REBUILD

    def test_empty_staged_rows_incomplete(self, verifier: CoverageVerifier) -> None:
        candidate = _candidate(status=CandidateGenerationStatus.VERIFYING)
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("daily_bars"),),
        )
        assert outcome.records[0].status is VerificationStatus.INCOMPLETE
        assert outcome.records[0].actual_count == 0

    def test_fundamentals_pit_violation(self, writer: StagingWriter, repo: SQLiteRepository, verifier: CoverageVerifier) -> None:
        writer.begin_candidate(_candidate(status=CandidateGenerationStatus.PLANNED), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        future = FundamentalSnapshot(CODES[0], DAY, date(2026, 9, 5), Decimal("8"), Decimal("1"), "fake")
        writer.write_batch(candidate, _task("fundamentals"), [future], source="fake")
        writer.finish_candidate(candidate)
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        outcome = verifier.verify(
            loaded,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("fundamentals"),),
        )
        assert any(
            i.issue_type is IssueType.PIT_VIOLATION
            for i in outcome.report.issues
        )

    def test_stocks_missing_snapshot(self, verifier: CoverageVerifier) -> None:
        candidate = _candidate(status=CandidateGenerationStatus.VERIFYING)
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("stocks"),),
        )
        assert any(
            i.issue_type is IssueType.MISSING and i.data_type == "stocks"
            for i in outcome.report.issues
        )

    def test_dividends_unavailable_is_not_refetch(self, verifier: CoverageVerifier) -> None:
        candidate = _candidate(status=CandidateGenerationStatus.VERIFYING)
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("dividends"),),
        )
        assert outcome.records[0].status is VerificationStatus.UNAVAILABLE
        assert all(
            i.repairability is not Repairability.REFETCH
            for i in outcome.report.issues
        )

    def test_unsupported_data_type_manual(self, verifier: CoverageVerifier) -> None:
        candidate = _candidate(status=CandidateGenerationStatus.VERIFYING)
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("bogus"),),
        )
        assert all(
            i.repairability is Repairability.MANUAL
            for i in outcome.report.issues
        )

    def test_sqlite_failure_raises_verification_error(self, tmp_path: Path) -> None:
        repo = SQLiteRepository(tmp_path / "m.sqlite3")
        # 传入一个损坏的连接工厂(表不存在) → sqlite3.Error → VerificationError
        def bad_factory() -> sqlite3.Connection:
            connection = sqlite3.connect(tmp_path / "m.sqlite3")
            connection.row_factory = sqlite3.Row
            return connection

        bad = CoverageVerifier(
            bad_factory,
            trading_days=lambda start, end: (DAY,),
            expected_universe_size=lambda day: 4,
        )
        # 删除 staging 表模拟损坏
        with sqlite3.connect(tmp_path / "m.sqlite3") as connection:
            connection.execute("DROP TABLE daily_bars_staging")
        with pytest.raises(VerificationError):
            bad.verify(
                _candidate(),
                adjustment=AdjustmentMethod.QFQ,
                target_start=DAY,
                target_end=DAY,
                tasks=(_task("daily_bars"),),
            )

    def test_dedup_partitions(self, writer: StagingWriter, repo: SQLiteRepository, verifier: CoverageVerifier) -> None:
        candidate = _stage_bars(writer, repo, [_bar(c) for c in CODES])
        outcome = verifier.verify(
            candidate,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_task("daily_bars"), _task("daily_bars", task_id="t2")),
        )
        assert len(outcome.records) == 1  # 同分区去重
