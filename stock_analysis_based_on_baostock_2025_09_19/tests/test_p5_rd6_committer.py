"""Offline tests for P5-RD-6: GenerationCommitter and ReadinessGate."""

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
    CoverageVerification,
    GenerationPartition,
    ReadinessResult,
    ReadinessStatus,
    VerificationStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.committer import (
    GenerationCommitter,
    PublishError,
    ReadinessGate,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)


def _candidate(**overrides: object) -> CandidateGeneration:
    values: dict[str, object] = {
        "candidate_generation_id": "cand-1",
        "plan_id": "plan-1",
        "parent_generation": None,
        "write_revision": 2,
        "status": CandidateGenerationStatus.VERIFIED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return CandidateGeneration(**values)  # type: ignore[arg-type]


def _verification(
    data_type: str = "daily_bars",
    partition_key: str = DAY.isoformat(),
    status: VerificationStatus = VerificationStatus.COMPLETE,
) -> CoverageVerification:
    return CoverageVerification(
        candidate_generation_id="cand-1",
        data_type=data_type,
        partition_key=partition_key,
        expected_count=1,
        actual_count=1,
        distinct_count=1,
        duplicate_count=0,
        invalid_count=0,
        coverage_ratio=Decimal("1"),
        missing_items=(),
        status=status,
        verified_revision=2,
        manifest_sha256="m" * 64,
        verified_at=NOW,
        details_json="{}",
    )


def _partition(data_type: str = "daily_bars") -> GenerationPartition:
    return GenerationPartition(
        generation="cand-1", data_type=data_type, partition_key=DAY.isoformat(),
        batch_id="batch-1",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "market.sqlite3")


@pytest.fixture()
def committer(repo: SQLiteRepository) -> GenerationCommitter:
    def factory() -> sqlite3.Connection:
        connection = sqlite3.connect(repo.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    return GenerationCommitter(factory, now=lambda: NOW)


@pytest.fixture()
def gate(repo: SQLiteRepository) -> ReadinessGate:
    def factory() -> sqlite3.Connection:
        connection = sqlite3.connect(repo.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    return ReadinessGate(factory)


def _seed_candidate_and_verifications(
    repo: SQLiteRepository,
    *,
    status: CandidateGenerationStatus = CandidateGenerationStatus.VERIFIED,
) -> CandidateGeneration:
    candidate = _candidate(status=status)
    repo.save_candidate_generation(candidate)
    repo.save_coverage_verification(_verification())
    return candidate


class TestGenerationCommitter:
    def test_publish_success(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _seed_candidate_and_verifications(repo)
        published = committer.publish(
            candidate,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            verifications=(_verification(),),
            partitions=(_partition(),),
        )
        assert published.generation == "cand-1"
        assert published.status is CandidateGenerationStatus.PUBLISHED
        active = repo.get_active_generation("market", AdjustmentMethod.QFQ)
        assert active is not None
        assert active.generation == "cand-1"
        partitions = repo.list_generation_partitions("cand-1")
        assert partitions == (_partition(),)
        loaded_candidate = repo.get_candidate_generation("cand-1")
        assert loaded_candidate is not None
        assert loaded_candidate.status is CandidateGenerationStatus.PUBLISHED

    def test_publish_non_verified_rejected(self, committer: GenerationCommitter) -> None:
        with pytest.raises(PublishError):
            committer.publish(
                _candidate(status=CandidateGenerationStatus.WRITING),
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(_verification(),),
                partitions=(_partition(),),
            )

    def test_publish_incomplete_verification_rejected(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _candidate()
        repo.save_candidate_generation(candidate)
        bad = _verification(status=VerificationStatus.INCOMPLETE)
        with pytest.raises(PublishError):
            committer.publish(
                candidate,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(bad,),
                partitions=(_partition(),),
            )

    def test_publish_missing_verification_rejected(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _candidate()
        repo.save_candidate_generation(candidate)
        # partitions 需要 daily_bars + fundamentals,但只有 daily_bars 验证
        with pytest.raises(PublishError):
            committer.publish(
                candidate,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(_verification(),),
                partitions=(_partition(), _partition(data_type="fundamentals")),
            )

    def test_publish_empty_partitions_rejected(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _seed_candidate_and_verifications(repo)
        with pytest.raises(PublishError):
            committer.publish(
                candidate,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(_verification(),),
                partitions=(),
            )

    def test_publish_partition_generation_mismatch(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _seed_candidate_and_verifications(repo)
        bad = GenerationPartition("other", "daily_bars", DAY.isoformat(), "batch-1")
        with pytest.raises(PublishError):
            committer.publish(
                candidate,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(_verification(),),
                partitions=(bad,),
            )

    def test_revision_mismatch_rejected_inside_txn(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _candidate(write_revision=5)  # 与落库 verification 的 2 不一致
        repo.save_candidate_generation(candidate)
        repo.save_coverage_verification(_verification())  # verified_revision=2
        with pytest.raises(PublishError):
            committer.publish(
                candidate,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(_verification(),),
                partitions=(_partition(),),
            )
        # 发布失败不得改变 active 指针
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None

    def test_status_changed_rejected_inside_txn(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _candidate()
        repo.save_candidate_generation(candidate)
        repo.save_coverage_verification(_verification())
        # 事务内状态已被改写(模拟竞态):提交前先改成 NEEDS_REPAIR
        repo.update_candidate_status("cand-1", CandidateGenerationStatus.NEEDS_REPAIR, NOW)
        with pytest.raises(PublishError):
            committer.publish(
                candidate,
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                verifications=(_verification(),),
                partitions=(_partition(),),
            )

    def test_supersede_keeps_old_generation(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        first = _seed_candidate_and_verifications(repo)
        committer.publish(
            first,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            verifications=(_verification(),),
            partitions=(_partition(),),
        )
        # 第二个 candidate:cand-2 的 partitions 必须归属 cand-2。
        second = _candidate(
            candidate_generation_id="cand-2",
            plan_id="plan-2",
            parent_generation="cand-1",
        )
        repo.save_candidate_generation(second)
        repo.save_coverage_verification(
            _verification(partition_key="2026-09-02")
        )
        committer.publish(
            second,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            verifications=(_verification(partition_key="2026-09-02"),),
            partitions=(
                GenerationPartition("cand-2", "daily_bars", DAY.isoformat(), "batch-1"),
                GenerationPartition("cand-2", "daily_bars", "2026-09-02", "batch-2"),
            ),
        )
        active = repo.get_active_generation("market", AdjustmentMethod.QFQ)
        assert active is not None
        assert active.generation == "cand-2"
        # 旧 generation 的 partitions 仍可查
        assert len(repo.list_generation_partitions("cand-1")) == 1


class TestReadinessGate:
    def _publish_ready(self, repo: SQLiteRepository, committer: GenerationCommitter) -> None:
        candidate = _seed_candidate_and_verifications(repo)
        committer.publish(
            candidate,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            verifications=(_verification(),),
            partitions=(_partition(),),
        )

    def test_ready(self, repo: SQLiteRepository, committer: GenerationCommitter, gate: ReadinessGate) -> None:
        self._publish_ready(repo, committer)
        result = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars",),
            requested_start=DAY,
            requested_end=DAY,
        )
        assert result.status is ReadinessStatus.READY
        assert result.generation == "cand-1"
        assert result.reason is None

    def test_no_generation(self, gate: ReadinessGate) -> None:
        result = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars",),
            requested_start=DAY,
            requested_end=DAY,
        )
        assert result.status is ReadinessStatus.NO_GENERATION
        assert result.generation is None

    def test_missing_data_type(self, repo: SQLiteRepository, committer: GenerationCommitter, gate: ReadinessGate) -> None:
        self._publish_ready(repo, committer)
        result = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars", "fundamentals"),
            requested_start=DAY,
            requested_end=DAY,
        )
        assert result.status is ReadinessStatus.MISSING_DATA_TYPE

    def test_out_of_range(self, repo: SQLiteRepository, committer: GenerationCommitter, gate: ReadinessGate) -> None:
        self._publish_ready(repo, committer)
        result = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars",),
            requested_start=date(2026, 8, 1),
            requested_end=date(2026, 8, 31),
        )
        assert result.status is ReadinessStatus.OUT_OF_RANGE

    def test_adjustment_mismatch(self, repo: SQLiteRepository, committer: GenerationCommitter, gate: ReadinessGate) -> None:
        self._publish_ready(repo, committer)
        result = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.HFQ,
            required_data_types=("daily_bars",),
            requested_start=DAY,
            requested_end=DAY,
        )
        # 存在 qfq active,但请求 hfq → ADJUSTMENT_MISMATCH。
        assert result.status is ReadinessStatus.ADJUSTMENT_MISMATCH
        assert result.generation is None

    def test_bad_range_rejected(self, gate: ReadinessGate) -> None:
        with pytest.raises(Exception):
            gate.evaluate(
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                required_data_types=("daily_bars",),
                requested_start=DAY,
                requested_end=date(2026, 8, 1),
            )

    def test_ready_result_contract(self) -> None:
        result = ReadinessResult(
            status=ReadinessStatus.READY,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            generation="g1",
            reason=None,
        )
        assert result.status is ReadinessStatus.READY
