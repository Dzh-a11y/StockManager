"""Offline synchronization, idempotency, locking, and retry tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import warnings
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
    SyncRecord,
    SyncStatus,
)
from stock_manager.cli.main import main
from stock_manager.providers import FixtureProvider
from stock_manager.storage import SQLiteRepository
from stock_manager.sync import (
    CooldownActiveError,
    DataSyncService,
    RetryRequiredError,
    SyncConfig,
    SyncFailedError,
    latest_completed_trading_day,
    load_sync_config,
)


DAY = date(2026, 8, 25)
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _provider(*, fail_method: str | None = None) -> FixtureProvider:
    stock = StockIdentity("sh.600000", "浦发银行", "SSE", False, None, None)
    bars = tuple(
        DailyBar(
            stock.code,
            trading_day,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.5"),
            Decimal("10"),
            Decimal("1000"),
            Decimal("10500"),
            True,
        )
        for trading_day in (date(2026, 8, 24), DAY)
    )
    fundamental = FundamentalSnapshot(
        stock.code, DAY, DAY, Decimal("8"), Decimal("1"), "fixture"
    )
    dividend = DividendRecord(stock.code, date(2025, 6, 1), Decimal("0.2"), "fixture")
    return FixtureProvider(
        trading_days=(date(2026, 8, 24), DAY, date(2026, 8, 26)),
        stocks=(stock,),
        bars=bars,
        fundamentals=(fundamental,),
        dividends=(dividend,),
        fail_method=fail_method,
    )


def _config(*, cooldown: timedelta = timedelta(minutes=5)) -> SyncConfig:
    return SyncConfig(time(17, 30), cooldown, 0, 30, 3)


def _service(
    tmp_path: Path,
    provider: FixtureProvider,
    clock: MutableClock,
    *,
    cooldown: timedelta = timedelta(minutes=5),
) -> tuple[DataSyncService, SQLiteRepository]:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        _config(cooldown=cooldown),
        clock=clock,
    )
    return service, repository


def test_sync_persists_success_and_second_call_skips_without_provider_access(
    tmp_path: Path,
) -> None:
    provider = _provider()
    service, repository = _service(tmp_path, provider, MutableClock(NOW))
    first = service.sync("market", DAY, AdjustmentMethod.QFQ)
    calls_after_first = provider.calls
    with pytest.warns(UserWarning, match="数据已存在，跳过拉取"):
        second = service.sync("market", DAY, AdjustmentMethod.QFQ)

    assert first.skipped is False
    assert second.skipped is True
    assert provider.calls == calls_after_first
    assert all(count == 1 for count in calls_after_first.values())
    record = repository.get_sync_record("market", DAY)
    assert record is not None and record.status is SyncStatus.SUCCESS
    assert repository.get_daily_bars(("sh.600000",), DAY, DAY, AdjustmentMethod.QFQ)


def test_concurrent_sync_calls_fetch_provider_only_once(tmp_path: Path) -> None:
    provider = _provider()
    service, _ = _service(tmp_path, provider, MutableClock(NOW))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda _: service.sync("market", DAY, AdjustmentMethod.QFQ),
                    range(2),
                )
            )

    assert sorted(outcome.skipped for outcome in outcomes) == [False, True]
    assert provider.calls["fetch_daily_bars"] == 1
    assert (tmp_path / "locks" / "market.2026-08-25.lock").exists()


def test_failure_records_failed_and_requires_explicit_cooled_retry(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    provider = _provider(fail_method="fetch_daily_bars")
    service, repository = _service(tmp_path, provider, clock)
    with pytest.raises(SyncFailedError):
        service.sync("market", DAY, AdjustmentMethod.QFQ)
    record = repository.get_sync_record("market", DAY)
    assert record is not None and record.status is SyncStatus.FAILED
    assert repository.get_stocks(DAY) == ()

    with pytest.raises(RetryRequiredError):
        service.sync("market", DAY, AdjustmentMethod.QFQ)
    with pytest.raises(CooldownActiveError):
        service.sync("market", DAY, AdjustmentMethod.QFQ, retry=True)

    provider.set_failure(None)
    clock.value += timedelta(minutes=5)
    outcome = service.sync("market", DAY, AdjustmentMethod.QFQ, retry=True)
    assert outcome.status is SyncStatus.SUCCESS


def test_non_trading_target_is_failed_not_success(tmp_path: Path) -> None:
    provider = _provider()
    service, repository = _service(tmp_path, provider, MutableClock(NOW))
    weekend = date(2026, 8, 23)
    with pytest.raises(SyncFailedError):
        service.sync("market", weekend, AdjustmentMethod.QFQ)
    record = repository.get_sync_record("market", weekend)
    assert record is not None and record.status is SyncStatus.FAILED


def test_incomplete_daily_payload_is_failed_and_not_persisted(tmp_path: Path) -> None:
    provider = FixtureProvider(
        trading_days=(DAY,),
        stocks=(StockIdentity("sh.600000", "浦发银行", "SSE", False, None, None),),
        bars=(),
    )
    service, repository = _service(tmp_path, provider, MutableClock(NOW))
    with pytest.raises(SyncFailedError):
        service.sync("market", DAY, AdjustmentMethod.QFQ)
    assert repository.get_daily_bars(("sh.600000",), DAY, DAY, AdjustmentMethod.QFQ) == ()
    record = repository.get_sync_record("market", DAY)
    assert record is not None and record.status is SyncStatus.FAILED


def test_latest_completed_trading_day_respects_shanghai_cutoff_and_weekend() -> None:
    days = (date(2026, 8, 21), date(2026, 8, 24), DAY)
    before = datetime(2026, 8, 25, 17, 0, tzinfo=SHANGHAI)
    after = datetime(2026, 8, 25, 18, 0, tzinfo=SHANGHAI)
    weekend = datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI)
    assert latest_completed_trading_day(before, days, time(17, 30)) == date(2026, 8, 24)
    assert latest_completed_trading_day(after, days, time(17, 30)) == DAY
    assert latest_completed_trading_day(weekend, days, time(17, 30)) == DAY


def test_startup_fills_missing_local_trading_days(tmp_path: Path) -> None:
    provider = _provider()
    service, repository = _service(tmp_path, provider, MutableClock(NOW))
    calendar_metadata = DatasetMetadata(
        "calendar",
        DAY,
        "fixture",
        NOW,
        AdjustmentMethod.UNADJUSTED,
    )
    repository.save_trading_days((date(2026, 8, 24), DAY), calendar_metadata)
    outcomes = service.sync_missing_on_startup(
        "market", date(2026, 8, 24), AdjustmentMethod.QFQ
    )
    assert [outcome.trading_day for outcome in outcomes] == [date(2026, 8, 24), DAY]
    assert all(outcome.status is SyncStatus.SUCCESS for outcome in outcomes)


def test_startup_bootstraps_calendar_once_per_coverage_end(tmp_path: Path) -> None:
    provider = _provider()
    service, repository = _service(tmp_path, provider, MutableClock(NOW))
    outcomes = service.sync_missing_on_startup(
        "market", DAY, AdjustmentMethod.QFQ
    )
    calls = provider.calls["fetch_trading_days"]
    assert [outcome.trading_day for outcome in outcomes] == [DAY]
    assert repository.get_sync_record("trading_calendar", date(2026, 9, 24)) is not None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        assert service.sync_missing_on_startup(
            "market", DAY, AdjustmentMethod.QFQ
        ) == ()
    assert provider.calls["fetch_trading_days"] == calls


def test_successful_dataset_cannot_be_reused_with_another_adjustment(tmp_path: Path) -> None:
    provider = _provider()
    service, _ = _service(tmp_path, provider, MutableClock(NOW))
    service.sync("market", DAY, AdjustmentMethod.QFQ)
    with pytest.raises(ValueError, match="different adjustment"):
        service.sync("market", DAY, AdjustmentMethod.UNADJUSTED)


def test_cli_sync_builds_provider_with_configured_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync.json interval is the single pacing knob, passed to the provider."""
    intervals: list[float] = []
    provider = _provider()
    monkeypatch.setattr(
        "stock_manager.cli.main.BaostockProvider",
        lambda request_interval_seconds: (
            intervals.append(request_interval_seconds) or provider
        ),
    )
    stdout = StringIO()

    exit_code = main(
        (
            "sync",
            "--config",
            str(Path(__file__).parents[1] / "config" / "sync.json"),
            "--db",
            str(tmp_path / "market.sqlite3"),
            "--lock-dir",
            str(tmp_path / "locks"),
            "--date",
            DAY.isoformat(),
            "--adjustment",
            "qfq",
        ),
        stdout=stdout,
        stderr=StringIO(),
    )

    assert exit_code == 0
    assert intervals == [0.2]


def test_provider_smoke_test_exercises_all_endpoints_without_persistence(
    tmp_path: Path,
) -> None:
    provider = _provider()
    service, repository = _service(tmp_path, provider, MutableClock(NOW))

    outcome = service.smoke_test_provider(
        "sh.600000", DAY, AdjustmentMethod.QFQ
    )

    assert outcome.source == "fixture"
    assert outcome.bar_count == 1
    assert outcome.fundamental_count == 1
    assert outcome.dividend_count == 0
    assert all(count == 1 for count in provider.calls.values())
    assert repository.get_sync_record("market", DAY) is None
    assert repository.get_stocks(DAY) == ()


def test_cli_smoke_uses_bounded_provider_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider()
    monkeypatch.setattr(
        "stock_manager.cli.main.BaostockProvider",
        lambda request_interval_seconds: provider,
    )
    stdout = StringIO()

    exit_code = main(
        (
            "smoke",
            "--config",
            str(Path(__file__).parents[1] / "config" / "sync.json"),
            "--code",
            "sh.600000",
            "--date",
            DAY.isoformat(),
            "--adjustment",
            "qfq",
        ),
        stdout=stdout,
        stderr=StringIO(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["source"] == "fixture"
    assert payload["bar_count"] == 1


def test_sync_config_rejects_invalid_rate_and_cutoff() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        SyncConfig(time(17, 30), timedelta(minutes=5), -1, 30, 3)
    with pytest.raises(ValueError, match="tzinfo"):
        SyncConfig(time(17, 30, tzinfo=SHANGHAI), timedelta(minutes=5), 0, 30, 3)
    with pytest.raises(ValueError, match="retention_days"):
        SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=0)


def test_default_sync_config_is_versioned_and_explicit() -> None:
    path = Path(__file__).parents[1] / "config" / "sync.json"
    config = load_sync_config(path)
    assert config.cutoff_time == time(17, 30)
    assert config.retry_cooldown == timedelta(minutes=5)
    assert config.minimum_request_interval_seconds == 0.2
    assert config.retention_days == 360


def _range_provider() -> tuple[FixtureProvider, StockIdentity, tuple[date, ...]]:
    stock = StockIdentity("sh.600000", "浦发银行", "SSE", False, None, None)
    days = tuple(date(2026, 8, day) for day in range(15, 26))
    bars = tuple(
        DailyBar(
            stock.code,
            day,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.5"),
            Decimal("10"),
            Decimal("1000"),
            Decimal("10500"),
            True,
        )
        for day in days
    )
    provider = FixtureProvider(
        trading_days=days, stocks=(stock,), bars=bars, fundamentals=(), dividends=()
    )
    return provider, stock, days


def test_backfill_history_fetches_retention_window_and_prunes_old(
    tmp_path: Path,
) -> None:
    provider, stock, _days = _range_provider()
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
    )

    old_day = date(2026, 8, 1)
    repository.save_daily_bars(
        (
            DailyBar(
                stock.code,
                old_day,
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                True,
            ),
        ),
        DatasetMetadata("market", old_day, "fixture", NOW, AdjustmentMethod.QFQ),
    )

    outcome = service.backfill_history("market", date(2026, 8, 25), AdjustmentMethod.QFQ)

    assert outcome.status is SyncStatus.SUCCESS
    assert len(
        repository.get_daily_bars(
            ("sh.600000",),
            date(2026, 8, 15),
            date(2026, 8, 25),
            AdjustmentMethod.QFQ,
        )
    ) == 11
    assert repository.get_daily_bars(
        ("sh.600000",), old_day, old_day, AdjustmentMethod.QFQ
    ) == ()
    assert provider.calls["fetch_daily_bars"] == 1


def test_backfill_on_startup_first_run_backfills_then_increments(tmp_path: Path) -> None:
    provider, _stock, _days = _range_provider()
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
    )

    first = service.backfill_on_startup("market", AdjustmentMethod.QFQ)
    assert first is not None and first.status is SyncStatus.SUCCESS
    assert len(
        repository.get_daily_bars(
            ("sh.600000",),
            date(2026, 8, 15),
            date(2026, 8, 25),
            AdjustmentMethod.QFQ,
        )
    ) == 11

    calls_before = provider.calls["fetch_daily_bars"]
    second = service.backfill_on_startup("market", AdjustmentMethod.QFQ)
    assert second is None
    assert provider.calls["fetch_daily_bars"] == calls_before


def test_backfill_on_startup_reruns_full_history_after_interrupted_backfill(
    tmp_path: Path,
) -> None:
    provider = _many_codes_provider(5, fail_method="fetch_fundamentals")
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
    )

    with pytest.raises(SyncFailedError):
        service.backfill_on_startup("market", AdjustmentMethod.QFQ)

    provider.set_failure(None)
    outcome = service.backfill_on_startup("market", AdjustmentMethod.QFQ)
    assert outcome is not None and outcome.status is SyncStatus.SUCCESS
    bars = repository.get_daily_bars(
        ("sh.600000",),
        date(2026, 8, 15),
        date(2026, 8, 25),
        AdjustmentMethod.QFQ,
    )
    # 中断后重启必须重跑完整历史回补（全窗口），而不是单日增量同步
    assert len(bars) == 11


def _many_codes_provider(
    count: int, *, fail_method: str | None = None
) -> FixtureProvider:
    stocks = tuple(
        StockIdentity(f"sh.{600000 + i:06d}", f"股{i}", "SSE", False, None, None)
        for i in range(count)
    )
    days = tuple(date(2026, 8, day) for day in range(15, 26))
    bars = tuple(
        DailyBar(
            stock.code,
            day,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.5"),
            Decimal("10"),
            Decimal("1000"),
            Decimal("10500"),
            True,
        )
        for stock in stocks
        for day in days
    )
    return FixtureProvider(
        trading_days=days,
        stocks=stocks,
        bars=bars,
        fundamentals=(),
        dividends=(),
        fail_method=fail_method,
    )


def test_backfill_history_saves_all_chunks_on_success(tmp_path: Path) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
    )

    outcome = service.backfill_history(
        "market", date(2026, 8, 25), AdjustmentMethod.QFQ, batch_size=100
    )

    assert outcome.status is SyncStatus.SUCCESS
    assert provider.calls["fetch_daily_bars"] == 3  # 250 codes / 100 per batch
    codes = tuple(f"sh.{600000 + i:06d}" for i in range(250))
    all_bars = repository.get_daily_bars(
        codes, date(2026, 8, 15), date(2026, 8, 25), AdjustmentMethod.QFQ
    )
    assert len(all_bars) == 250 * 11


def test_backfill_history_keeps_fetched_chunks_on_failure(tmp_path: Path) -> None:
    provider = _many_codes_provider(250, fail_method="fetch_fundamentals")
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
    )

    with pytest.raises(SyncFailedError):
        service.backfill_history(
            "market", date(2026, 8, 25), AdjustmentMethod.QFQ, batch_size=100
        )

    # 第一块（sh.600000..sh.600099）的 bars 已落库，第二块未拉到
    assert len(repository.get_stocks(date(2026, 8, 25))) == 250
    assert len(
        repository.get_daily_bars(
            ("sh.600000",), date(2026, 8, 15), date(2026, 8, 25), AdjustmentMethod.QFQ
        )
    ) == 11
    assert repository.get_daily_bars(
        ("sh.600100",), date(2026, 8, 15), date(2026, 8, 25), AdjustmentMethod.QFQ
    ) == ()
    record = repository.get_sync_record("market", date(2026, 8, 25))
    assert record is not None and record.status is SyncStatus.FAILED


def test_backfill_history_reruns_after_failed_marker(tmp_path: Path) -> None:
    provider = _many_codes_provider(250, fail_method="fetch_fundamentals")
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
    )

    with pytest.raises(SyncFailedError):
        service.backfill_history(
            "market", date(2026, 8, 25), AdjustmentMethod.QFQ, batch_size=100
        )
    record = repository.get_sync_record("market", date(2026, 8, 25))
    assert record is not None and record.status is SyncStatus.FAILED

    # 失败后无需显式 retry 即可重跑，且之前已落库的块被保留
    provider.set_failure(None)
    outcome = service.backfill_history(
        "market", date(2026, 8, 25), AdjustmentMethod.QFQ, batch_size=100
    )
    assert outcome.status is SyncStatus.SUCCESS
    final = repository.get_sync_record("market", date(2026, 8, 25))
    assert final is not None and final.status is SyncStatus.SUCCESS
    codes = tuple(f"sh.{600000 + i:06d}" for i in range(250))
    assert len(
        repository.get_daily_bars(
            codes, date(2026, 8, 15), date(2026, 8, 25), AdjustmentMethod.QFQ
        )
    ) == 250 * 11


def test_backfill_history_reports_overall_progress(tmp_path: Path) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10
    )
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    events: list[dict[str, object]] = []
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        config,
        clock=MutableClock(NOW),
        progress=events.append,
    )

    outcome = service.backfill_history(
        "market", date(2026, 8, 25), AdjustmentMethod.QFQ, batch_size=100
    )

    assert outcome.status is SyncStatus.SUCCESS
    assert len(events) == 6  # 250 / 100 = 3 块 × 2 个阶段
    assert events[0]["phase"] == "daily_bars"
    assert events[0]["total"] == 500  # 2 阶段 × 250 只
    assert events[0]["completed"] == 100
    assert events[-1]["phase"] == "fundamentals"
    assert events[-1]["completed"] == 500
    assert all(event["total"] == 500 for event in events)


def _seed_chunk(
    repository: SQLiteRepository,
    codes: Sequence[str],
    days: Sequence[date],
    as_of: date,
    *,
    checkpoint: bool = True,
    short_code: str | None = None,
    short_days: int = 3,
) -> None:
    """Simulate a chunk fully persisted by a previous backfill run.

    ``short_code`` mimics a stock listed inside the retention window: it only
    has bars for the last ``short_days`` trading days. The chunk-level
    checkpoint is what resume trusts, so short history must not matter.
    """
    metadata = DatasetMetadata("market", as_of, "fixture", NOW, AdjustmentMethod.QFQ)
    bars = []
    for code in codes:
        code_days = days[-short_days:] if code == short_code else days
        for day in code_days:
            bars.append(
                DailyBar(
                    code,
                    day,
                    Decimal("10"),
                    Decimal("11"),
                    Decimal("9"),
                    Decimal("10.5"),
                    Decimal("10"),
                    Decimal("1000"),
                    Decimal("10500"),
                    True,
                )
            )
    repository.save_daily_bars(tuple(bars), metadata)
    repository.save_fundamentals(
        tuple(
            FundamentalSnapshot(code, as_of, as_of, Decimal("8"), Decimal("1"), "fixture")
            for code in codes
        ),
        metadata,
    )
    if checkpoint:
        repository.mark_chunk_complete("market", as_of, AdjustmentMethod.QFQ, 0, codes)


def test_backfill_history_resumes_from_persisted_chunks(tmp_path: Path) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider, repository, tmp_path / "locks", config, clock=MutableClock(NOW)
    )

    as_of = date(2026, 8, 25)
    start = as_of - timedelta(days=10)
    days = tuple(date(2026, 8, day) for day in range(15, 26))
    first_chunk = tuple(f"sh.{600000 + i:06d}" for i in range(100))
    _seed_chunk(repository, first_chunk, days, as_of)

    outcome = service.backfill_history("market", as_of, AdjustmentMethod.QFQ, batch_size=100)

    assert outcome.status is SyncStatus.SUCCESS
    # 第一块有 checkpoint，被跳过，只补第二、三块
    assert provider.calls["fetch_daily_bars"] == 2
    assert provider.calls["fetch_fundamentals"] == 2
    codes = tuple(f"sh.{600000 + i:06d}" for i in range(250))
    assert len(
        repository.get_daily_bars(codes, start, as_of, AdjustmentMethod.QFQ)
    ) == 250 * 11


def test_backfill_history_refetches_chunk_without_checkpoint(tmp_path: Path) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider, repository, tmp_path / "locks", config, clock=MutableClock(NOW)
    )

    as_of = date(2026, 8, 25)
    days = tuple(date(2026, 8, day) for day in range(15, 26))
    first_chunk = tuple(f"sh.{600000 + i:06d}" for i in range(100))
    # 数据在库里但没写 checkpoint（旧版本落的数据）：整块必须重拉
    _seed_chunk(repository, first_chunk, days, as_of, checkpoint=False)

    outcome = service.backfill_history("market", as_of, AdjustmentMethod.QFQ, batch_size=100)

    assert outcome.status is SyncStatus.SUCCESS
    assert provider.calls["fetch_daily_bars"] == 3
    assert provider.calls["fetch_fundamentals"] == 3


def test_backfill_history_skips_chunk_with_short_history_stock(tmp_path: Path) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider, repository, tmp_path / "locks", config, clock=MutableClock(NOW)
    )

    as_of = date(2026, 8, 25)
    days = tuple(date(2026, 8, day) for day in range(15, 26))
    first_chunk = tuple(f"sh.{600000 + i:06d}" for i in range(100))
    # 批次内含一只"次新股"（只下了最后 3 天），但批次有 checkpoint → 仍跳过
    _seed_chunk(
        repository, first_chunk, days, as_of, short_code=first_chunk[0], short_days=3
    )

    outcome = service.backfill_history("market", as_of, AdjustmentMethod.QFQ, batch_size=100)

    assert outcome.status is SyncStatus.SUCCESS
    assert provider.calls["fetch_daily_bars"] == 2
    assert provider.calls["fetch_fundamentals"] == 2


def test_backfill_history_refetches_chunk_when_checkpoint_codes_differ(
    tmp_path: Path,
) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider, repository, tmp_path / "locks", config, clock=MutableClock(NOW)
    )

    as_of = date(2026, 8, 25)
    # checkpoint 里记录的代码与当前批次不一致（股票列表顺序/构成变化）→ 不能跳过
    wrong_codes = tuple(f"sh.{700000 + i:06d}" for i in range(100))
    repository.mark_chunk_complete("market", as_of, AdjustmentMethod.QFQ, 0, wrong_codes)

    outcome = service.backfill_history("market", as_of, AdjustmentMethod.QFQ, batch_size=100)

    assert outcome.status is SyncStatus.SUCCESS
    assert provider.calls["fetch_daily_bars"] == 3
    assert provider.calls["fetch_fundamentals"] == 3


def test_backfill_history_warns_over_stale_running_record(tmp_path: Path) -> None:
    provider = _many_codes_provider(250)
    config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider, repository, tmp_path / "locks", config, clock=MutableClock(NOW)
    )
    as_of = date(2026, 8, 25)
    repository.save_sync_record(
        SyncRecord(
            "market", as_of, SyncStatus.RUNNING, "fixture", AdjustmentMethod.QFQ, NOW, None, None
        )
    )

    with pytest.warns(UserWarning, match="RUNNING"):
        outcome = service.backfill_history("market", as_of, AdjustmentMethod.QFQ, batch_size=100)

    assert outcome.status is SyncStatus.SUCCESS
    record = repository.get_sync_record("market", as_of)
    assert record is not None and record.status is SyncStatus.SUCCESS


def test_sync_warns_over_stale_running_record(tmp_path: Path) -> None:
    provider = _provider()
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider, repository, tmp_path / "locks", _config(), clock=MutableClock(NOW)
    )
    repository.save_sync_record(
        SyncRecord(
            "market", DAY, SyncStatus.RUNNING, "fixture", AdjustmentMethod.QFQ, NOW, None, None
        )
    )

    with pytest.warns(UserWarning, match="RUNNING"):
        outcome = service.sync("market", DAY, AdjustmentMethod.QFQ)

    assert outcome.status is SyncStatus.SUCCESS
    record = repository.get_sync_record("market", DAY)
    assert record is not None and record.status is SyncStatus.SUCCESS


class _RecordingProvider(FixtureProvider):
    """Fixture provider that records every fetch_daily_bars range."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bar_ranges: list[tuple[date, date]] = []

    def fetch_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]:
        self.bar_ranges.append((start, end))
        return super().fetch_daily_bars(codes, start, end, adjustment)


def test_backfill_history_incremental_tail_only_fetches_new_days(
    tmp_path: Path,
) -> None:
    provider = _many_codes_provider(250)
    recording = _RecordingProvider(
        trading_days=provider._trading_days,
        stocks=provider._stocks,
        bars=provider._bars,
        fundamentals=provider._fundamentals,
        dividends=provider._dividends,
    )
    config = SyncConfig(time(17, 30), timedelta(minutes=5), 0, 30, 3, retention_days=10)
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        recording, repository, tmp_path / "locks", config, clock=MutableClock(NOW)
    )

    first = service.backfill_history("market", date(2026, 8, 25), AdjustmentMethod.QFQ, batch_size=100)
    assert first.status is SyncStatus.SUCCESS

    before = len(recording.bar_ranges)
    second = service.backfill_history("market", date(2026, 8, 26), AdjustmentMethod.QFQ, batch_size=100)
    assert second.status is SyncStatus.SUCCESS

    # 第二次只拉 08-26 起的尾部,而不是整个保留窗口
    assert recording.bar_ranges[-1] == (date(2026, 8, 26), date(2026, 8, 26))
    assert all(rng[0] == date(2026, 8, 26) for rng in recording.bar_ranges[before:])
    assert repository.latest_backfill_cover_date("market", AdjustmentMethod.QFQ) == date(2026, 8, 26)


def test_sync_missing_auto_retries_failed_day_after_cooldown(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    provider = _provider()
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        _config(),
        clock=clock,
    )
    # DAY 上次同步失败,finished_at 在 10 分钟前(冷却 5 分钟已过)
    finished = NOW - timedelta(minutes=10)
    repository.save_sync_record(
        SyncRecord("market", DAY, SyncStatus.FAILED, "fixture", AdjustmentMethod.QFQ, finished, finished, "boom")
    )

    outcomes = service.sync_missing_on_startup("market", DAY, AdjustmentMethod.QFQ)

    assert tuple(o.trading_day for o in outcomes) == (DAY,)
    assert outcomes[0].status is SyncStatus.SUCCESS


def test_sync_missing_skips_failed_day_inside_cooldown(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    provider = _provider()
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        _config(),
        clock=clock,
    )
    repository.save_sync_record(
        SyncRecord(
            "market", DAY, SyncStatus.FAILED, "fixture", AdjustmentMethod.QFQ, NOW, NOW, "boom"
        )
    )

    with pytest.warns(UserWarning, match="冷却"):
        outcomes = service.sync_missing_on_startup("market", DAY, AdjustmentMethod.QFQ)

    assert outcomes == ()
