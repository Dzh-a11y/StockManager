"""Offline tests for P5-RD-4: StagingWriter batch isolation and revisions."""

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
    StockIdentity,
    SyncTask,
    SyncTaskStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.staging import (
    CandidateNotWritableError,
    StagingWriteError,
    StagingWriter,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)


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


def _task(data_type: str = "daily_bars", **overrides: object) -> SyncTask:
    values: dict[str, object] = {
        "task_id": "t1",
        "plan_id": "plan-1",
        "sequence_no": 0,
        "data_type": data_type,
        "partition_key": DAY.isoformat(),
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


def _bar(code: str = "sh.600000") -> DailyBar:
    return DailyBar(
        code, DAY, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"),
        Decimal("10"), Decimal("1000"), Decimal("10500"), True,
    )


def _fund(code: str = "sh.600000") -> FundamentalSnapshot:
    return FundamentalSnapshot(code, DAY, DAY, Decimal("8.5"), Decimal("0.9"), "fake")


def _stock(code: str = "sh.600000") -> StockIdentity:
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


class TestStagingWriter:
    def test_begin_candidate_writes_status(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        assert loaded.status is CandidateGenerationStatus.WRITING

    def test_begin_non_planned_rejected(self, writer: StagingWriter) -> None:
        with pytest.raises(CandidateNotWritableError):
            writer.begin_candidate(
                _candidate(status=CandidateGenerationStatus.WRITING), "fake"
            )

    def test_write_batch_daily_bars(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        batch = writer.write_batch(
            candidate,
            _task("daily_bars"),
            [_bar()],
            source="fake",
            adjustment=AdjustmentMethod.QFQ,
        )
        assert batch.row_count == 1
        assert batch.batch_id.startswith("batch-")
        loaded = repo.get_ingest_batch(batch.batch_id)
        assert loaded is not None
        assert loaded == batch
        # 正式表不被触碰
        with sqlite3.connect(repo.database_path) as connection:
            bars = connection.execute(
                "SELECT COUNT(*) FROM daily_bars"
            ).fetchone()[0]
            staging = connection.execute(
                "SELECT COUNT(*) FROM daily_bars_staging"
            ).fetchone()[0]
        assert bars == 0
        assert staging == 1

    def test_batch_rows_checkpoint_and_revision_are_atomic(
        self,
        writer: StagingWriter,
        repo: SQLiteRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """批次登记失败时不得留下无 checkpoint 的孤儿 staging 行。"""
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None

        def fail_batch_record(connection: sqlite3.Connection, batch: object) -> None:
            raise sqlite3.OperationalError("checkpoint write failed")

        monkeypatch.setattr(
            StagingWriter, "_save_batch_record", staticmethod(fail_batch_record)
        )
        with pytest.raises(StagingWriteError, match="checkpoint write failed"):
            writer.write_batch(
                candidate,
                _task("daily_bars"),
                [_bar()],
                source="fake",
                adjustment=AdjustmentMethod.QFQ,
            )
        with sqlite3.connect(repo.database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM daily_bars_staging"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM ingest_batches"
            ).fetchone()[0] == 0
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        assert loaded.write_revision == 0

    def test_write_rechecks_persisted_candidate_status(
        self, writer: StagingWriter, repo: SQLiteRepository
    ) -> None:
        """调用方持有旧 WRITING 对象时也不能写入已失败 candidate。"""
        writer.begin_candidate(_candidate(), "fake")
        stale = repo.get_candidate_generation("cand-1")
        assert stale is not None
        repo.update_candidate_status(
            "cand-1", CandidateGenerationStatus.FAILED, NOW
        )
        with pytest.raises(CandidateNotWritableError, match="not writable"):
            writer.write_batch(
                stale,
                _task("daily_bars"),
                [_bar()],
                source="fake",
                adjustment=AdjustmentMethod.QFQ,
            )
        with sqlite3.connect(repo.database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM daily_bars_staging"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM ingest_batches"
            ).fetchone()[0] == 0

    def test_recover_batch_registers_matching_orphan_without_refetch(
        self, writer: StagingWriter, repo: SQLiteRepository
    ) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        task = _task(
            "daily_bars",
            status=SyncTaskStatus.SUCCESS,
            attempt_count=1,
            row_count=1,
            started_at=NOW,
            finished_at=NOW,
        )
        batch_id = writer.batch_id(candidate, task)
        with sqlite3.connect(repo.database_path) as connection:
            connection.execute(
                """INSERT INTO daily_bars_staging
                   (batch_id, code, trading_day, adjustment, open, high, low,
                    close, preclose, volume, amount, is_trading)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id, "sh.600000", DAY.isoformat(), "qfq", "10", "11",
                    "9", "10.5", "10", "1000", "10500", 1,
                ),
            )
        recovered = writer.recover_batch(
            candidate,
            task,
            source="fake",
        )
        assert recovered is not None
        assert recovered.row_count == 1
        assert repo.get_ingest_batch(batch_id) == recovered

    def test_write_revision_bumps_per_batch(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        assert candidate.write_revision == 0
        writer.write_batch(
            candidate, _task("daily_bars"), [_bar()],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        writer.write_batch(
            candidate, _task("fundamentals"), [_fund()],
            source="fake",
        )
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        assert loaded.write_revision == 2

    def test_idempotent_rewrite_replaces_rows(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        first = writer.write_batch(
            candidate, _task("daily_bars"), [_bar("sh.600000")],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        second = writer.write_batch(
            candidate, _task("daily_bars"), [_bar("sh.600000"), _bar("sz.000001")],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        assert first.batch_id == second.batch_id
        with sqlite3.connect(repo.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM daily_bars_staging WHERE batch_id = ?",
                (first.batch_id,),
            ).fetchone()[0]
        assert count == 2  # 重写替换而非追加

    def test_write_requires_writable_candidate(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        with pytest.raises(CandidateNotWritableError):
            writer.write_batch(
                _candidate(status=CandidateGenerationStatus.VERIFIED),
                _task("daily_bars"),
                [_bar()],
                source="fake",
                adjustment=AdjustmentMethod.QFQ,
            )

    def test_finish_candidate_verifying(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        writer.write_batch(
            candidate, _task("daily_bars"), [_bar()],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        writer.finish_candidate(candidate)
        loaded = repo.get_candidate_generation("cand-1")
        assert loaded is not None
        assert loaded.status is CandidateGenerationStatus.VERIFYING

    def test_finish_non_writing_rejected(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        with pytest.raises(CandidateNotWritableError):
            writer.finish_candidate(
                _candidate(status=CandidateGenerationStatus.VERIFYING)
            )

    def test_daily_bars_requires_adjustment(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        with pytest.raises(StagingWriteError):
            writer.write_batch(
                candidate, _task("daily_bars"), [_bar()], source="fake"
            )

    def test_unknown_data_type_rejected(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        with pytest.raises(StagingWriteError):
            writer.write_batch(
                candidate, _task("not_a_type"), [], source="fake"
            )

    def test_staging_invisible_to_published_read(self, writer: StagingWriter, repo: SQLiteRepository) -> None:
        # 写入正式表一条已发布数据,再写 candidate 批次;常规读取只见已发布数据。
        metadata = repo.get_dataset_metadata
        from stock_manager.domain import DatasetMetadata, SyncRecord, SyncStatus

        sync_metadata = DatasetMetadata("market", DAY, "published", NOW, AdjustmentMethod.QFQ)
        record = SyncRecord(
            "market", DAY, SyncStatus.SUCCESS, "published", AdjustmentMethod.QFQ,
            NOW, NOW, None,
        )
        repo.save_market_snapshot(
            [_stock()], [_bar()], [_fund()], (), (DAY,), sync_metadata, record
        )
        writer.begin_candidate(_candidate(), "fake")
        candidate = repo.get_candidate_generation("cand-1")
        assert candidate is not None
        writer.write_batch(
            candidate, _task("daily_bars"), [_bar("sz.000001")],
            source="fake", adjustment=AdjustmentMethod.QFQ,
        )
        bars = repo.get_daily_bars(("sh.600000", "sz.000001"), DAY, DAY, AdjustmentMethod.QFQ)
        assert [b.code for b in bars] == ["sh.600000"]  # candidate 不可见
