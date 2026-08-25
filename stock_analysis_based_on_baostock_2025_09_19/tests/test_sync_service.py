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
        sleep=lambda seconds: None,
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


def test_provider_calls_are_rate_limited_serially(tmp_path: Path) -> None:
    ticks = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return ticks[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        ticks[0] += seconds

    provider = _provider()
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        SyncConfig(time(17, 30), timedelta(0), 2.0, 30, 3),
        clock=MutableClock(NOW),
        monotonic=monotonic,
        sleep=sleep,
    )
    service.sync("market", DAY, AdjustmentMethod.QFQ)
    assert sleeps == [2.0, 2.0, 2.0, 2.0]


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
    assert outcome.dividend_count == 1
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


def test_default_sync_config_is_versioned_and_explicit() -> None:
    path = Path(__file__).parents[1] / "config" / "sync.json"
    config = load_sync_config(path)
    assert config.cutoff_time == time(17, 30)
    assert config.retry_cooldown == timedelta(minutes=5)
    assert config.minimum_request_interval_seconds == 0.2
