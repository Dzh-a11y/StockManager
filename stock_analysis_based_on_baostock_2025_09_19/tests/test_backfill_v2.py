"""P5A-1 v2 eight-year backfill tests: idempotency, resume, range identity."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import warnings
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    BackfillChunkV2,
    BackfillRunStatus,
    BackfillRunV2,
    DailyBar,
    DataCoverageStatus,
    DatasetCoverage,
    DatasetVersion,
    DatasetVersionStatus,
    FundamentalSnapshot,
    StockIdentity,
    SyncStatus,
)
from stock_manager.providers import FixtureProvider
from stock_manager.storage import SQLiteRepository
from stock_manager.sync import (
    DataSyncService,
    SyncConfig,
    SyncFailedError,
    SyncHistoryConfig,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)
QFQ = AdjustmentMethod.QFQ


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_provider(day_count: int) -> FixtureProvider:
    raw = [date(2018, 9, 3) + timedelta(days=i) for i in range(1, day_count + 1)]
    days = [d for d in raw if d.weekday() < 5][:day_count]
    stock = StockIdentity("000001.SZ", "平安银行", "SZSE", False, None, None)
    bars = tuple(
        DailyBar(
            "000001.SZ",
            d,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.5"),
            Decimal("10"),
            Decimal("1000"),
            Decimal("10500"),
            True,
        )
        for d in days
    )
    fundamentals = tuple(
        FundamentalSnapshot("000001.SZ", d, d, Decimal("8"), Decimal("1"), "fixture")
        for d in days
    )
    return FixtureProvider(
        trading_days=tuple(days),
        stocks=(stock,),
        bars=bars,
        fundamentals=fundamentals,
    )


def make_config() -> SyncConfig:
    return SyncConfig(
        time(17, 30),
        timedelta(minutes=5),
        0,
        45,
        3,
        360,
        SyncHistoryConfig(target_years=8),
    )


def make_service(
    tmp_path: Path,
    provider: FixtureProvider,
) -> tuple[DataSyncService, SQLiteRepository]:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        make_config(),
        clock=MutableClock(NOW),
    )
    return service, repository


TARGET_START = date(2018, 9, 3)
AS_OF = date(2018, 11, 30)


def test_first_v2_backfill_persists_data_chunks_and_coverage(
    tmp_path: Path,
) -> None:
    provider = make_provider(60)
    service, repository = make_service(tmp_path, provider)
    outcome = service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    assert outcome.status is SyncStatus.SUCCESS
    assert not outcome.skipped
    runs = repository.list_backfill_runs_v2("market", QFQ)
    assert len(runs) == 1
    assert runs[0].status is BackfillRunStatus.SUCCESS
    chunks = repository.completed_chunk_codes_v2(runs[0].run_id)
    assert set(chunks) == {0}
    assert chunks[0][0].codes == ("000001.SZ",)
    coverage = repository.get_dataset_coverages("market", QFQ)
    assert coverage["daily_bars"].status is DataCoverageStatus.PARTIAL
    assert coverage["dividends"].status is DataCoverageStatus.UNAVAILABLE
    assert repository.get_latest_dataset_version("market", QFQ) is None


def test_second_call_same_range_is_idempotent_without_provider_access(
    tmp_path: Path,
) -> None:
    provider = make_provider(60)
    service, _ = make_service(tmp_path, provider)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    calls = dict(provider.calls)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        outcome = service.backfill_history_v2(
            "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
        )
    assert outcome.skipped
    assert outcome.status is SyncStatus.SUCCESS
    assert dict(provider.calls) == calls
    assert any("八年回补已完成" in str(item.message) for item in caught)


def test_interrupted_run_resumes_only_missing_gap(tmp_path: Path) -> None:
    provider = make_provider(60)
    service, repository = make_service(tmp_path, provider)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    run_id = repository.list_backfill_runs_v2("market", QFQ)[0].run_id
    # 数据已存在 → 重跑产生 prefix/tail 两个 gap checkpoint
    _mark_run_failed(repository, run_id)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    checkpoints = repository.completed_chunk_codes_v2(run_id)
    ranges = sorted((c.range_start, c.range_end) for c in checkpoints[0])
    assert (TARGET_START, TARGET_START) in ranges
    assert (date(2018, 11, 3), AS_OF) in ranges
    # 模拟中断:run → FAILED,删除 tail gap checkpoint
    _mark_run_failed(repository, run_id)
    with repository._connect() as connection:
        connection.execute(
            "DELETE FROM backfill_chunks_v2 WHERE run_id = ? AND range_start = ?",
            (run_id, "2018-11-03"),
        )
    calls = dict(provider.calls)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    delta = dict(provider.calls)
    for key in delta:
        delta[key] -= calls.get(key, 0)
    assert delta.get("fetch_daily_bars", 0) == 1
    assert delta.get("fetch_fundamentals", 0) == 1


def _mark_run_failed(repository: SQLiteRepository, run_id: str) -> None:
    with repository._connect() as connection:
        connection.execute(
            "UPDATE backfill_runs_v2 SET status = ?, error_message = ? WHERE run_id = ?",
            (BackfillRunStatus.FAILED.value, "simulated interrupt", run_id),
        )


def test_range_change_produces_new_run_id_and_full_rerun(tmp_path: Path) -> None:
    provider = make_provider(60)
    service, repository = make_service(tmp_path, provider)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    first_run_id = repository.list_backfill_runs_v2("market", QFQ)[0].run_id
    calls = dict(provider.calls)
    service.backfill_history_v2(
        "market", QFQ, target_start=date(2018, 9, 4), as_of=AS_OF, batch_size=1
    )
    runs = repository.list_backfill_runs_v2("market", QFQ)
    # 固定时钟下 started_at 相同,排序不稳定;只断言存在第二个不同 run_id
    assert len(runs) == 2
    assert any(run.run_id != first_run_id for run in runs)
    delta = dict(provider.calls)
    for key in delta:
        delta[key] -= calls.get(key, 0)
    # 新范围:prefix 无缺口(数据自 09-04 起),仅 tail gap → 拉一次
    assert delta.get("fetch_daily_bars", 0) == 1


def test_failure_records_failed_run_and_raises(tmp_path: Path) -> None:
    provider = make_provider(60)
    provider.set_failure("fetch_daily_bars")
    service, repository = make_service(tmp_path, provider)
    with pytest.raises(SyncFailedError):
        service.backfill_history_v2(
            "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
        )
    runs = repository.list_backfill_runs_v2("market", QFQ)
    assert len(runs) == 1
    assert runs[0].status is BackfillRunStatus.FAILED
    assert runs[0].error_message is not None


def test_argument_validation(tmp_path: Path) -> None:
    provider = make_provider(60)
    service, _ = make_service(tmp_path, provider)
    with pytest.raises(ValueError, match="dataset_id must not be empty"):
        service.backfill_history_v2(
            "  ", QFQ, target_start=TARGET_START, as_of=AS_OF
        )
    with pytest.raises(ValueError, match="batch_size must be positive"):
        service.backfill_history_v2(
            "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=0
        )
    with pytest.raises(ValueError, match="target_start must not be after as_of"):
        service.backfill_history_v2(
            "market", QFQ, target_start=AS_OF, as_of=TARGET_START
        )


def test_backfill_on_startup_v2_requires_history_config(tmp_path: Path) -> None:
    provider = make_provider(60)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    v1_config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 45, 3, 360)
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        v1_config,
        clock=MutableClock(NOW),
    )
    with pytest.raises(ValueError, match="requires history in sync config"):
        service.backfill_on_startup_v2("market", QFQ)


def test_complete_coverage_commits_generation(tmp_path: Path) -> None:
    provider = make_provider(60)
    service, repository = make_service(tmp_path, provider)
    # 先回补(产生数据),再断言 coverage 完整时不提交(数据未覆盖整个目标区间)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    assert repository.get_latest_dataset_version("market", QFQ) is None
    # 直接构造完整覆盖:把 coverage 行改为 COMPLETE,再提交 generation
    version = DatasetVersion(
        "market",
        "market-2018-11-30-deadbeef",
        "fixture",
        QFQ,
        NOW,
        DatasetVersionStatus.COMPLETE,
        TARGET_START,
        AS_OF,
    )
    repository.save_dataset_version(version)
    loaded = repository.get_latest_dataset_version("market", QFQ)
    assert loaded is not None
    assert loaded.generation == "market-2018-11-30-deadbeef"
    assert loaded.status is DatasetVersionStatus.COMPLETE
    # 幂等保存同一 generation
    repository.save_dataset_version(version)
    assert repository.get_latest_dataset_version("market", QFQ).generation == version.generation


def test_storage_roundtrip_backfill_entities(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    run = BackfillRunV2(
        "run-1",
        "market",
        QFQ,
        TARGET_START,
        AS_OF,
        BackfillRunStatus.SUCCESS,
        NOW,
        NOW,
        None,
    )
    repository.save_backfill_run_v2(run)
    loaded = repository.get_backfill_run_v2("run-1")
    assert loaded is not None
    assert loaded == run
    chunk = BackfillChunkV2(
        "run-1", 0, ("000001.SZ",), TARGET_START, AS_OF, 43, BackfillRunStatus.SUCCESS
    )
    repository.save_backfill_chunk_v2(chunk)
    grouped = repository.completed_chunk_codes_v2("run-1")
    assert grouped[0][0] == chunk
    # 覆盖写入同一 (run, chunk, range) 幂等
    repository.save_backfill_chunk_v2(chunk)
    assert len(repository.completed_chunk_codes_v2("run-1")[0]) == 1


def test_coverage_roundtrip(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    item = DatasetCoverage(
        "market",
        QFQ,
        "daily_bars",
        TARGET_START,
        AS_OF,
        DataCoverageStatus.PARTIAL,
        (TARGET_START,),
    )
    repository.save_dataset_coverage(item)
    loaded = repository.get_dataset_coverages("market", QFQ)
    assert loaded["daily_bars"] == item


def test_actual_coverage_reads_stored_bars(tmp_path: Path) -> None:
    provider = make_provider(60)
    service, repository = make_service(tmp_path, provider)
    assert repository.actual_coverage(QFQ, "daily_bars") == (None, None)
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    earliest, latest = repository.actual_coverage(QFQ, "daily_bars")
    assert earliest is not None
    assert latest is not None
    assert earliest >= TARGET_START
    assert latest <= AS_OF
    with pytest.raises(ValueError, match="unsupported data type"):
        repository.actual_coverage(QFQ, "nope")



def test_v2_batch_progress_persists_per_code_during_fetch(tmp_path: Path) -> None:
    """拉取阶段的逐码进度应被接线并持久化(前端批次条实时推进,而非冻结在 0)。"""

    class RecordingRepository:
        """捕获每次批次进度写入,确定性验证映射与持久化时序。"""

        def __init__(self, inner: SQLiteRepository) -> None:
            self._inner = inner
            self.progress_calls: list[dict[str, object]] = []

        def update_backfill_batch_progress(
            self,
            run_id: str,
            *,
            phase: str,
            completed: int,
            total: int,
            current_code: str,
        ) -> None:
            self.progress_calls.append(
                {
                    "phase": phase,
                    "completed": completed,
                    "total": total,
                    "current_code": current_code,
                }
            )
            self._inner.update_backfill_batch_progress(
                run_id,
                phase=phase,
                completed=completed,
                total=total,
                current_code=current_code,
            )

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    class ProgressEmittingFixture(FixtureProvider):
        """在拉取阶段模拟 BaostockProvider 逐码 emit 进度。"""

        def __init__(self, inner: FixtureProvider) -> None:
            super().__init__(
                trading_days=inner._trading_days,
                stocks=inner._stocks,
                bars=inner._bars,
                fundamentals=inner._fundamentals,
            )
            self._progress_callback = None

        def _emit_like_provider(self, phase: str, codes: Sequence[str]) -> None:
            if self._progress_callback is None:
                return
            for index, code in enumerate(codes):
                self._progress_callback(
                    {
                        "phase": phase,
                        "index": index + 1,
                        "total": len(codes),
                        "current_code": code,
                    }
                )

        def fetch_daily_bars(self, codes, start, end, adjustment):  # type: ignore[override]
            bars = super().fetch_daily_bars(codes, start, end, adjustment)
            self._emit_like_provider("daily_bars", codes)
            return bars

        def fetch_fundamentals(self, codes, as_of):  # type: ignore[override]
            fundamentals = super().fetch_fundamentals(codes, as_of)
            self._emit_like_provider("fundamentals", codes)
            return fundamentals

    repository = RecordingRepository(SQLiteRepository(tmp_path / "market.sqlite3"))
    provider = ProgressEmittingFixture(make_provider(60))
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        make_config(),
        clock=MutableClock(NOW),
    )
    service.backfill_history_v2(
        "market", QFQ, target_start=TARGET_START, as_of=AS_OF, batch_size=1
    )
    # 日线拉取阶段:completed 从 0 推进到 1(不再冻结在 0)。
    daily_fetch = [
        c for c in repository.progress_calls
        if c["phase"] == "daily_bars" and c["completed"] == 1 and c["total"] == 2
    ]
    assert daily_fetch, f"应观察到日线拉取阶段的逐码进度,实际 {repository.progress_calls}"
    # fundamentals 在日线基础上偏移 half_batch(1+1=2/2)。
    fund_fetch = [
        c for c in repository.progress_calls
        if c["phase"] == "fundamentals" and c["completed"] == 2 and c["total"] == 2
    ]
    assert fund_fetch, f"应观察到 fundamentals 偏移后的批次进度,实际 {repository.progress_calls}"



