"""Offline tests for P5-RD-9: legacy shared-table migration (LEGACY_IMPORT)."""

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
    DatasetMetadata,
    FundamentalSnapshot,
    StockIdentity,
    SyncRecord,
    SyncStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.committer import GenerationCommitter
from stock_manager.sync.legacy import LegacyImportError, LegacyImporter
from stock_manager.sync.verifier import CoverageVerifier

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)


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


@pytest.fixture()
def importer(repo: SQLiteRepository) -> LegacyImporter:
    return LegacyImporter(_factory(repo), now=lambda: NOW)


def _bar(code: str) -> DailyBar:
    return DailyBar(
        code, DAY, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"),
        Decimal("10"), Decimal("1000"), Decimal("10500"), True,
    )


def _populate_legacy(repo: SQLiteRepository) -> None:
    """Simulate a legacy database whose rows have no batch binding."""
    stocks = tuple(
        StockIdentity(c, "股票", "SSE", False, None, None)
        for c in ("sh.600000", "sz.000001", "sh.600519")
    )
    bars = tuple(_bar(c) for c in ("sh.600000", "sz.000001", "sh.600519"))
    funds = tuple(
        FundamentalSnapshot(c, DAY, DAY, Decimal("8"), Decimal("1"), "legacy")
        for c in ("sh.600000", "sz.000001", "sh.600519")
    )
    metadata = DatasetMetadata("market", DAY, "legacy", NOW, AdjustmentMethod.QFQ)
    record = SyncRecord(
        "market", DAY, SyncStatus.SUCCESS, "legacy", AdjustmentMethod.QFQ,
        NOW, NOW, None,
    )
    repo.save_market_snapshot(stocks, bars, funds, (), (DAY,), metadata, record)


class TestLegacyImporter:
    def test_build_candidate_writing(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        candidate = importer.build_candidate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy",
            candidate_id="cand-legacy",
        )
        loaded = repo.get_candidate_generation("cand-legacy")
        assert loaded is not None
        assert loaded.status is CandidateGenerationStatus.WRITING

    def test_import_partition_copies_rows(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        _populate_legacy(repo)
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        batch = importer.import_partition(
            candidate,
            data_type="daily_bars",
            partition_key=DAY.isoformat(),
            batch_id="batch-legacy-bars",
            source="legacy",
            adjustment=AdjustmentMethod.QFQ,
        )
        assert batch.row_count == 3
        with sqlite3.connect(repo.database_path) as connection:
            staging = connection.execute(
                "SELECT COUNT(*) FROM daily_bars_staging WHERE batch_id = ?",
                ("batch-legacy-bars",),
            ).fetchone()[0]
            legacy = connection.execute(
                "SELECT COUNT(*) FROM daily_bars"
            ).fetchone()[0]
        assert staging == 3
        assert legacy == 3  # 旧表不被修改

    def test_daily_bars_requires_adjustment(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        _populate_legacy(repo)
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        with pytest.raises(LegacyImportError):
            importer.import_partition(
                candidate,
                data_type="daily_bars",
                partition_key=DAY.isoformat(),
                batch_id="b",
                source="legacy",
            )

    def test_unsupported_type_rejected(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        with pytest.raises(LegacyImportError):
            importer.import_partition(
                candidate, data_type="bogus", partition_key=DAY.isoformat(),
                batch_id="b", source="legacy",
            )

    def test_revision_bumps(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        _populate_legacy(repo)
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        importer.import_partition(
            candidate, data_type="stocks", partition_key=DAY.isoformat(),
            batch_id="b1", source="legacy",
        )
        importer.import_partition(
            candidate, data_type="fundamentals", partition_key=DAY.isoformat(),
            batch_id="b2", source="legacy",
        )
        loaded = repo.get_candidate_generation("cand-legacy")
        assert loaded is not None
        assert loaded.write_revision == 2

    def test_finish_and_partitions(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        _populate_legacy(repo)
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        importer.import_partition(
            candidate, data_type="daily_bars", partition_key=DAY.isoformat(),
            batch_id="b1", source="legacy", adjustment=AdjustmentMethod.QFQ,
        )
        finished = importer.finish_candidate(candidate)
        assert finished.status is CandidateGenerationStatus.VERIFYING
        partitions = importer.partitions_for(
            "cand-legacy", generation="cand-legacy"
        )
        assert len(partitions) == 1
        assert partitions[0].batch_id == "b1"


class TestLegacyImportEndToEnd:
    def test_full_pipeline(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        """旧库 → candidate → 验证 → 发布 → ReadinessGate READY"""
        _populate_legacy(repo)
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        for data_type, batch_id, adjustment in (
            ("stocks", "b-stocks", None),
            ("daily_bars", "b-bars", AdjustmentMethod.QFQ),
            ("fundamentals", "b-funds", None),
        ):
            importer.import_partition(
                candidate, data_type=data_type, partition_key=DAY.isoformat(),
                batch_id=batch_id, source="legacy", adjustment=adjustment,
            )
        finished = importer.finish_candidate(candidate)

        verifier = CoverageVerifier(
            _factory(repo),
            trading_days=lambda start, end: (DAY,),
            expected_universe_size=lambda day: 3,
        )
        tasks = [
            _legacy_task("stocks", "b-stocks"),
            _legacy_task("daily_bars", "b-bars"),
            _legacy_task("fundamentals", "b-funds"),
        ]
        outcome = verifier.verify(
            finished,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=tasks,
        )
        assert outcome.report.issues == ()

        # 落库验证记录(committer 需要事务内 recheck)
        for record in outcome.records:
            repo.save_coverage_verification(record)
        repo.update_candidate_status(
            "cand-legacy", CandidateGenerationStatus.VERIFIED, NOW
        )
        verified = repo.get_candidate_generation("cand-legacy")
        assert verified is not None

        committer = GenerationCommitter(_factory(repo), now=lambda: NOW)
        partitions = importer.partitions_for("cand-legacy", generation="cand-legacy")
        published = committer.publish(
            verified,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            verifications=outcome.records,
            partitions=partitions,
        )
        assert published.generation == "cand-legacy"
        active = repo.get_active_generation("market", AdjustmentMethod.QFQ)
        assert active is not None
        assert active.generation == "cand-legacy"

    def test_verification_failure_keeps_legacy(self, repo: SQLiteRepository, importer: LegacyImporter) -> None:
        """验证不通过时旧表与 active 指针保持不变。"""
        _populate_legacy(repo)
        candidate = importer.build_candidate(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            plan_id="plan-legacy", candidate_id="cand-legacy",
        )
        # 只导入 1 只股票的 bar,覆盖率不足 → 验证失败
        with sqlite3.connect(repo.database_path) as connection:
            connection.execute(
                "DELETE FROM daily_bars WHERE code != 'sh.600000'"
            )
            connection.commit()
        importer.import_partition(
            candidate, data_type="daily_bars", partition_key=DAY.isoformat(),
            batch_id="b-bars", source="legacy", adjustment=AdjustmentMethod.QFQ,
        )
        finished = importer.finish_candidate(candidate)
        verifier = CoverageVerifier(
            _factory(repo),
            trading_days=lambda start, end: (DAY,),
            expected_universe_size=lambda day: 3,
        )
        outcome = verifier.verify(
            finished,
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAY,
            target_end=DAY,
            tasks=(_legacy_task("daily_bars", "b-bars"),),
        )
        assert outcome.report.issues  # MISSING
        # 旧表未被删除,active 指针不存在
        with sqlite3.connect(repo.database_path) as connection:
            legacy = connection.execute(
                "SELECT COUNT(*) FROM daily_bars"
            ).fetchone()[0]
        assert legacy == 1
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None


def _legacy_task(data_type: str, batch_id: str):
    from stock_manager.domain import SyncTask, SyncTaskStatus

    codes = {
        "stocks": ("sh.600000", "sz.000001", "sh.600519"),
        "daily_bars": ("sh.600000", "sz.000001", "sh.600519"),
        "fundamentals": ("sh.600000", "sz.000001", "sh.600519"),
    }.get(data_type, ("sh.600000",))
    return SyncTask(
        task_id=f"task-{data_type}",
        plan_id="plan-legacy",
        sequence_no=0,
        data_type=data_type,
        partition_key=DAY.isoformat(),
        codes=codes,
        range_start=DAY,
        range_end=DAY,
        dependencies=(),
        status=SyncTaskStatus.SUCCESS,
        attempt_count=1,
        not_before=None,
        row_count=3,
        error_code=None,
        error_message=None,
        started_at=NOW,
        finished_at=NOW,
    )
