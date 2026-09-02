"""Offline tests for P5-RD-1: domain contracts and versioned schema migration."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    ActiveGeneration,
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    CoverageVerification,
    GenerationPartition,
    IngestBatch,
    PublishedGeneration,
    ReadinessResult,
    ReadinessStatus,
    SyncPlan,
    SyncPlanMode,
    SyncPlanStatus,
    SyncSource,
    SyncTask,
    SyncTaskStatus,
    VerificationIssue,
    VerificationReport,
    VerificationStatus,
    IssueType,
    Repairability,
)
from stock_manager.protocols import DataSyncAdminRepositoryProtocol
from stock_manager.storage import SQLiteRepository
from stock_manager.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    migrate_database,
    schema_version,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)


def _plan(**overrides: object) -> SyncPlan:
    values: dict[str, object] = {
        "plan_id": "plan-1",
        "plan_version": 1,
        "mode": SyncPlanMode.BOOTSTRAP,
        "source": SyncSource.BAOSTOCK,
        "dataset_id": "market",
        "adjustment": AdjustmentMethod.QFQ,
        "universe_policy": "a-share",
        "target_start": date(2018, 9, 1),
        "target_end": DAY,
        "latest_completed_trading_day": DAY,
        "parent_generation": None,
        "candidate_generation_id": None,
        "required_data_types": ("daily_bars", "fundamentals"),
        "task_count": 10,
        "plan_fingerprint": "fp-1",
        "status": SyncPlanStatus.PLANNED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SyncPlan(**values)  # type: ignore[arg-type]


def _task(**overrides: object) -> SyncTask:
    values: dict[str, object] = {
        "task_id": "task-1",
        "plan_id": "plan-1",
        "sequence_no": 0,
        "data_type": "daily_bars",
        "partition_key": "2026-09-01",
        "codes": ("sh.600000",),
        "range_start": DAY,
        "range_end": DAY,
        "dependencies": (),
        "status": SyncTaskStatus.PENDING,
        "attempt_count": 0,
        "not_before": None,
        "row_count": None,
        "error_code": None,
        "error_message": None,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return SyncTask(**values)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> CandidateGeneration:
    values: dict[str, object] = {
        "candidate_generation_id": "cand-1",
        "plan_id": "plan-1",
        "parent_generation": None,
        "write_revision": 0,
        "status": CandidateGenerationStatus.PLANNED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return CandidateGeneration(**values)  # type: ignore[arg-type]


def _batch(**overrides: object) -> IngestBatch:
    values: dict[str, object] = {
        "batch_id": "batch-1",
        "candidate_generation_id": "cand-1",
        "data_type": "daily_bars",
        "partition_key": "2026-09-01",
        "codes": ("sh.600000",),
        "range_start": DAY,
        "range_end": DAY,
        "row_count": 1,
        "source": "fixture",
        "batch_sha256": "a" * 64,
        "created_at": NOW,
    }
    values.update(overrides)
    return IngestBatch(**values)  # type: ignore[arg-type]


def _verification(**overrides: object) -> CoverageVerification:
    values: dict[str, object] = {
        "candidate_generation_id": "cand-1",
        "data_type": "daily_bars",
        "partition_key": "2026-09-01",
        "expected_count": 1,
        "actual_count": 1,
        "distinct_count": 1,
        "duplicate_count": 0,
        "invalid_count": 0,
        "coverage_ratio": Decimal("1"),
        "missing_items": (),
        "status": VerificationStatus.COMPLETE,
        "verified_revision": 0,
        "manifest_sha256": "b" * 64,
        "verified_at": NOW,
        "details_json": "{}",
    }
    values.update(overrides)
    return CoverageVerification(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Domain contract validation
# ---------------------------------------------------------------------------


class TestSyncPlanContract:
    def test_valid_plan(self) -> None:
        plan = _plan()
        assert plan.plan_id == "plan-1"
        assert plan.mode is SyncPlanMode.BOOTSTRAP

    def test_empty_plan_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan(plan_id="  ")

    def test_reversed_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan(target_start=DAY, target_end=date(2018, 9, 1))

    def test_empty_data_types_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan(required_data_types=())

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan(created_at=datetime(2026, 9, 1, 12, 0))

    def test_negative_task_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            _plan(task_count=-1)


class TestSyncTaskContract:
    def test_valid_task(self) -> None:
        task = _task()
        assert task.status is SyncTaskStatus.PENDING

    def test_empty_codes_rejected(self) -> None:
        with pytest.raises(ValueError):
            _task(codes=())

    def test_success_requires_finished_at(self) -> None:
        with pytest.raises(ValueError):
            _task(status=SyncTaskStatus.SUCCESS, finished_at=None)

    def test_success_must_not_have_error(self) -> None:
        with pytest.raises(ValueError):
            _task(
                status=SyncTaskStatus.SUCCESS,
                finished_at=NOW,
                error_message="oops",
            )

    def test_failed_requires_error_message(self) -> None:
        with pytest.raises(ValueError):
            _task(status=SyncTaskStatus.FAILED, finished_at=NOW, error_message=None)

    def test_reversed_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            _task(range_start=DAY, range_end=date(2026, 8, 1))


class TestCandidateGenerationContract:
    def test_valid_candidate(self) -> None:
        assert _candidate().write_revision == 0

    def test_negative_revision_rejected(self) -> None:
        with pytest.raises(ValueError):
            _candidate(write_revision=-1)


class TestIngestBatchContract:
    def test_valid_batch(self) -> None:
        assert _batch().row_count == 1

    def test_negative_row_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            _batch(row_count=-1)

    def test_empty_sha_rejected(self) -> None:
        with pytest.raises(ValueError):
            _batch(batch_sha256="")


class TestCoverageVerificationContract:
    def test_valid_verification(self) -> None:
        assert _verification().coverage_ratio == Decimal("1")

    def test_ratio_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            _verification(coverage_ratio=Decimal("1.5"))

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValueError):
            _verification(actual_count=-1)


class TestPublishedGenerationContract:
    def test_valid_published(self) -> None:
        published = PublishedGeneration(
            generation="g1",
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            parent_generation=None,
            manifest_sha256="c" * 64,
            published_at=NOW,
            status=CandidateGenerationStatus.PUBLISHED,
        )
        assert published.status is CandidateGenerationStatus.PUBLISHED

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError):
            PublishedGeneration(
                generation="g1",
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                parent_generation=None,
                manifest_sha256="c" * 64,
                published_at=NOW,
                status=CandidateGenerationStatus.VERIFIED,
            )


class TestActiveGenerationContract:
    def test_valid_active(self) -> None:
        active = ActiveGeneration(
            "market", AdjustmentMethod.QFQ, "g1", NOW
        )
        assert active.generation == "g1"


class TestReadinessResultContract:
    def test_ready_requires_generation(self) -> None:
        with pytest.raises(ValueError):
            ReadinessResult(
                status=ReadinessStatus.READY,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                generation=None,
                reason=None,
            )

    def test_non_ready_requires_reason(self) -> None:
        with pytest.raises(ValueError):
            ReadinessResult(
                status=ReadinessStatus.NO_GENERATION,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                generation=None,
                reason=None,
            )


class TestVerificationReportContract:
    def test_valid_report(self) -> None:
        issue = VerificationIssue(
            issue_id="i1",
            candidate_generation_id="cand-1",
            data_type="daily_bars",
            partition_key="2026-09-01",
            trading_day=DAY,
            codes=("sh.600000",),
            issue_type=IssueType.MISSING,
            expected_count=1,
            actual_count=0,
            repairability=Repairability.REFETCH,
            details="missing bar",
        )
        report = VerificationReport("cand-1", (issue,), NOW)
        assert len(report.issues) == 1

    def test_mismatched_candidate_rejected(self) -> None:
        issue = VerificationIssue(
            issue_id="i1",
            candidate_generation_id="other",
            data_type="daily_bars",
            partition_key="2026-09-01",
            trading_day=DAY,
            codes=("sh.600000",),
            issue_type=IssueType.MISSING,
            expected_count=1,
            actual_count=0,
            repairability=Repairability.REFETCH,
            details="missing bar",
        )
        with pytest.raises(ValueError):
            VerificationReport("cand-1", (issue,), NOW)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "market.sqlite3")


class TestMigration:
    def test_empty_db_migrates_to_current(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.sqlite3"
        repo = SQLiteRepository(path)
        with sqlite3.connect(path) as connection:
            assert schema_version(connection) == CURRENT_SCHEMA_VERSION
            assert schema_version(connection) == 3
        repo = None  # noqa: F841

    def test_batch_columns_added(self, tmp_path: Path) -> None:
        path = tmp_path / "m.sqlite3"
        SQLiteRepository(path)
        with sqlite3.connect(path) as connection:
            for table in ("daily_bars", "stocks", "fundamentals", "dividends"):
                columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                assert "batch_id" in columns, table

    def test_bookkeeping_tables_created(self, tmp_path: Path) -> None:
        path = tmp_path / "m.sqlite3"
        SQLiteRepository(path)
        expected = {
            "sync_plans",
            "sync_tasks",
            "candidate_generations",
            "ingest_batches",
            "generation_partitions",
            "coverage_verifications",
            "active_generations",
            "seed_imports",
        }
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert expected <= tables

    def test_generation_manifest_keeps_all_code_batches(self, tmp_path: Path) -> None:
        """同一日期/类型的多个代码批次必须同时保留在 manifest。"""
        path = tmp_path / "m.sqlite3"
        SQLiteRepository(path)
        with sqlite3.connect(path) as connection:
            pk_columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(generation_partitions)"
                ).fetchall()
                if row[5] > 0
            ]
        assert pk_columns == ["generation", "data_type", "partition_key", "batch_id"]

    def test_v2_provider_budget_tables_are_removed(self, tmp_path: Path) -> None:
        path = tmp_path / "v2.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE provider_request_ledger (source TEXT PRIMARY KEY)"
            )
            connection.execute(
                "CREATE TABLE provider_circuit_breakers (source TEXT PRIMARY KEY)"
            )
            connection.execute("PRAGMA user_version = 2")
        SQLiteRepository(path)
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "provider_request_ledger" not in tables
            assert "provider_circuit_breakers" not in tables

    def test_legacy_db_migrates(self, tmp_path: Path) -> None:
        # Simulate a real pre-P5 v0 database: the full old schema created by
        # the previous SQLiteRepository (no batch_id, no bookkeeping), with
        # user_version 0.
        from stock_manager.storage.sqlite_repo import SCHEMA

        path = tmp_path / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA user_version = 0")
        SQLiteRepository(path)
        with sqlite3.connect(path) as connection:
            assert schema_version(connection) == CURRENT_SCHEMA_VERSION
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(daily_bars)"
                ).fetchall()
            }
            assert "batch_id" in columns

    def test_repeated_migration_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "m.sqlite3"
        first = SQLiteRepository(path)
        first = None  # noqa: F841
        second = SQLiteRepository(path)
        second = None  # noqa: F841
        with sqlite3.connect(path) as connection:
            assert schema_version(connection) == CURRENT_SCHEMA_VERSION

    def test_newer_schema_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "newer.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 99")
        with pytest.raises(ValueError):
            SQLiteRepository(path)

    def test_failed_migration_rolls_back(self, tmp_path: Path) -> None:
        # A real migration step that fails mid-transaction must roll back both
        # its DDL and user_version.
        import stock_manager.storage.migrations as migrations

        path = tmp_path / "fail.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 1")

        def failing_step(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE migration_probe (id INTEGER)")
            raise sqlite3.OperationalError("boom")

        original = migrations._MIGRATIONS[2]
        migrations._MIGRATIONS[2] = failing_step
        try:
            with sqlite3.connect(path) as connection:
                with pytest.raises(sqlite3.OperationalError, match="boom"):
                    migrations.migrate_database(connection)
                assert schema_version(connection) == 1
                probe = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'migration_probe'"
                ).fetchone()
                assert probe is None
        finally:
            migrations._MIGRATIONS[2] = original

    def test_real_repo_protocol_conformance(self, repo: SQLiteRepository) -> None:
        assert isinstance(repo, DataSyncAdminRepositoryProtocol)


class TestBookkeepingRoundTrip:
    def test_plan_round_trip(self, repo: SQLiteRepository) -> None:
        repo.save_sync_plan(_plan())
        loaded = repo.get_sync_plan("plan-1")
        assert loaded is not None
        assert loaded == _plan()

    def test_task_round_trip(self, repo: SQLiteRepository) -> None:
        repo.save_sync_plan(_plan())
        repo.save_sync_task(_task())
        loaded = repo.get_sync_task("task-1")
        assert loaded is not None
        assert loaded == _task()

    def test_task_status_filter(self, repo: SQLiteRepository) -> None:
        repo.save_sync_plan(_plan())
        repo.save_sync_task(_task())
        repo.save_sync_task(
            _task(
                task_id="task-2",
                sequence_no=1,
                status=SyncTaskStatus.SUCCESS,
                finished_at=NOW,
                row_count=5,
            )
        )
        pending = repo.tasks_by_status("plan-1", (SyncTaskStatus.PENDING,))
        done = repo.tasks_by_status("plan-1", (SyncTaskStatus.SUCCESS,))
        assert [t.task_id for t in pending] == ["task-1"]
        assert [t.task_id for t in done] == ["task-2"]

    def test_candidate_round_trip(self, repo: SQLiteRepository) -> None:
        repo.save_candidate_generation(_candidate())
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        assert loaded == _candidate()

    def test_batch_round_trip(self, repo: SQLiteRepository) -> None:
        repo.save_ingest_batch(_batch())
        loaded = repo.get_ingest_batch("batch-1")
        assert loaded is not None
        assert loaded == _batch()
        listed = repo.list_ingest_batches("cand-1")
        assert [b.batch_id for b in listed] == ["batch-1"]

    def test_verification_round_trip(self, repo: SQLiteRepository) -> None:
        repo.save_coverage_verification(_verification())
        listed = repo.list_coverage_verifications("cand-1")
        assert len(listed) == 1
        assert listed[0] == _verification()

    def test_partition_round_trip(self, repo: SQLiteRepository) -> None:
        partition = GenerationPartition("g1", "daily_bars", "2026-09-01", "batch-1")
        repo.save_generation_partition(partition)
        listed = repo.list_generation_partitions("g1")
        assert listed == (partition,)

    def test_published_and_active_round_trip(self, repo: SQLiteRepository) -> None:
        published = PublishedGeneration(
            generation="g1",
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            parent_generation=None,
            manifest_sha256="d" * 64,
            published_at=NOW,
            status=CandidateGenerationStatus.PUBLISHED,
        )
        repo.save_published_generation(published)
        loaded = repo.get_latest_published_generation("market", AdjustmentMethod.QFQ)
        assert loaded is not None
        assert loaded.generation == "g1"
        active = ActiveGeneration("market", AdjustmentMethod.QFQ, "g1", NOW)
        repo.save_active_generation(active)
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) == active

    def test_supersede_updates_pointer(self, repo: SQLiteRepository) -> None:
        first = PublishedGeneration(
            generation="g1",
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            parent_generation=None,
            manifest_sha256="e" * 64,
            published_at=NOW,
            status=CandidateGenerationStatus.PUBLISHED,
        )
        second = PublishedGeneration(
            generation="g2",
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            parent_generation="g1",
            manifest_sha256="f" * 64,
            published_at=NOW,
            status=CandidateGenerationStatus.PUBLISHED,
        )
        repo.save_published_generation(first)
        repo.save_published_generation(second)
        latest = repo.get_latest_published_generation("market", AdjustmentMethod.QFQ)
        assert latest is not None
        assert latest.generation == "g2"
        assert latest.parent_generation == "g1"
