"""P5A-5 historical run store and cache tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    HistoricalRunStatus,
    HistoricalScreeningRun,
)
from stock_manager.research import EvaluationSchedule
from stock_manager.services.historical_screening_cache import historical_cache_key
from stock_manager.services.historical_screening_run_store import (
    HistoricalScreeningRunStore,
    InvalidRunTransitionError,
    RunNotFoundError,
)
from stock_manager.storage import SQLiteRepository

QFQ = AdjustmentMethod.QFQ
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, minutes: int) -> None:
        self.value = self.value + timedelta(minutes=minutes)


def _store(tmp_path: Path) -> tuple[HistoricalScreeningRunStore, SQLiteRepository]:
    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    return HistoricalScreeningRunStore(repo, clock=Clock()), repo


def _run(
    run_id: str = "run-1",
    status: HistoricalRunStatus = HistoricalRunStatus.QUEUED,
    started_at: datetime = NOW,
) -> HistoricalScreeningRun:
    return HistoricalScreeningRun(
        run_id=run_id,
        cache_key="ck",
        dataset_id="market",
        adjustment=QFQ,
        generation="gen-1",
        template_id="t",
        template_revision=1,
        plan_fingerprint="fp",
        rule_implementation_version="builtin-v1",
        universe_policy="pit_as_of",
        evaluation_start=date(2020, 1, 1),
        evaluation_end=date(2020, 1, 31),
        status=status,
        progress_completed=0,
        progress_total=10,
        started_at=started_at,
        finished_at=None,
        error_message=None,
    )


def test_lifecycle_transitions(tmp_path: Path) -> None:
    store, repo = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    store.start_building("run-1")
    store.update_progress("run-1", 5, 10)
    store.succeed("run-1", 10, 10)
    run = store.get("run-1")
    assert run is not None
    assert run.status is HistoricalRunStatus.SUCCEEDED
    assert run.finished_at == NOW
    assert run.progress_completed == 10


def test_illegal_transitions_rejected(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    with pytest.raises(InvalidRunTransitionError, match="BUILDING_SIGNALS"):
        store.start_building("run-1")
    store.start_validating("run-1")
    store.start_building("run-1")
    store.succeed("run-1", 10, 10)
    with pytest.raises(InvalidRunTransitionError):
        store.fail("run-1", "late failure")


def test_failure_records_error(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    with pytest.raises(ValueError, match="error_message"):
        store.fail("run-1", "  ")
    store.fail("run-1", "provider timeout")
    run = store.get("run-1")
    assert run is not None
    assert run.status is HistoricalRunStatus.FAILED
    assert run.error_message == "provider timeout"


def test_cancel_flow(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    store.request_cancel("run-1")
    run = store.get("run-1")
    assert run is not None
    assert run.status is HistoricalRunStatus.CANCEL_REQUESTED
    store.finish_cancelled("run-1")
    assert store.get("run-1").status is HistoricalRunStatus.CANCELLED


def test_interrupted_recovery_and_requeue(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    store.start_building("run-1")
    store.create(_run(run_id="run-2", status=HistoricalRunStatus.QUEUED))
    store.create(_run(run_id="run-3", status=HistoricalRunStatus.QUEUED))
    store.start_validating("run-3")
    store.start_building("run-3")
    store.succeed("run-3", 1, 1)  # 终态不受影响
    marked = store.recover_interrupted("market", QFQ)
    assert marked == 2  # run-1 BUILDING 与 run-2 QUEUED;run-3 SUCCEEDED 不算
    assert store.get("run-1").status is HistoricalRunStatus.INTERRUPTED
    assert store.get("run-2").status is HistoricalRunStatus.INTERRUPTED
    assert store.get("run-3").status is HistoricalRunStatus.SUCCEEDED
    store.enqueue_interrupted("run-1")
    assert store.get("run-1").status is HistoricalRunStatus.QUEUED


def test_progress_requires_building(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    with pytest.raises(InvalidRunTransitionError, match="BUILDING_SIGNALS"):
        store.update_progress("run-1", 1, 10)
    store.start_validating("run-1")
    store.start_building("run-1")
    with pytest.raises(ValueError, match="progress"):
        store.update_progress("run-1", 11, 10)


def test_eligibility_persistence_and_paging(tmp_path: Path) -> None:
    store, repo = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    store.start_building("run-1")
    from stock_manager.services.historical_screening_executor import EligibilitySnapshot

    snapshots = (
        EligibilitySnapshot(date(2020, 1, 2), ("000001.SZ", "000002.SZ"), 2),
        EligibilitySnapshot(date(2020, 1, 3), ("000001.SZ",), 1),
    )
    store.save_eligibility("run-1", snapshots)
    store.succeed("run-1", 2, 2)
    days = repo.list_eligibility_days("run-1")
    assert days == ((date(2020, 1, 2), 2), (date(2020, 1, 3), 1))
    assert repo.list_eligible_codes("run-1", date(2020, 1, 2)) == ("000001.SZ", "000002.SZ")
    assert repo.count_eligibility_days("run-1") == 2
    # 分页
    assert repo.list_eligibility_days("run-1", offset=1, limit=1) == ((date(2020, 1, 3), 1),)
    with pytest.raises(ValueError):
        repo.list_eligibility_days("run-1", offset=-1)


def test_cache_hit_and_prune(tmp_path: Path) -> None:
    store, repo = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    store.start_building("run-1")
    store.succeed("run-1", 1, 1)
    assert store.find_cached("ck") is not None
    assert store.find_cached("other-key") is None
    store.create(_run(run_id="run-2", started_at=NOW + timedelta(minutes=5)))
    store.start_validating("run-2")
    store.start_building("run-2")
    store.succeed("run-2", 1, 1)
    assert store.prune("market", QFQ, keep=1) == 1
    assert store.get("run-1") is None
    assert store.get("run-2") is not None


def test_cache_key_stability_and_sensitivity() -> None:
    base = dict(
        dataset_id="market",
        generation="gen-1",
        adjustment=QFQ,
        universe_policy="pit_as_of",
        evaluation_start=date(2020, 1, 1),
        evaluation_end=date(2020, 1, 31),
        schedule=EvaluationSchedule.DAILY,
        template_id="t",
        template_revision=1,
        plan_fingerprint="fp",
        rule_implementation_version="builtin-v1",
        calendar_fingerprint="cal-1",
    )
    key1 = historical_cache_key(**base)
    assert key1 == historical_cache_key(**base)
    # 模板 revision 变化 → 键变化
    assert key1 != historical_cache_key(**{**base, "template_revision": 2})
    # 数据 generation 变化 → 键变化
    assert key1 != historical_cache_key(**{**base, "generation": "gen-2"})
    # 规则实现版本变化 → 键变化
    assert key1 != historical_cache_key(**{**base, "rule_implementation_version": "builtin-v2"})
    # 评估窗口变化 → 键变化
    assert key1 != historical_cache_key(
        **{**base, "evaluation_end": date(2020, 2, 28)}
    )
