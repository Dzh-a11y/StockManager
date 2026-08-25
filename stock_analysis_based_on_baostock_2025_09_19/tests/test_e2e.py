"""Offline end-to-end acceptance tests for the new screening core."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import warnings
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
)
from stock_manager.providers import FixtureProvider
from stock_manager.rules import load_rules_config
from stock_manager.services import ScreeningService
from stock_manager.storage import SQLiteRepository
from stock_manager.sync import DataSyncService, SyncConfig


SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET_DAY = date(2026, 8, 25)


def _market_fixture() -> FixtureProvider:
    days = tuple(date(2026, 8, value) for value in range(20, 26))
    stock = StockIdentity("sh.600001", "Alpha", "SSE", False, date(2000, 1, 1), None)
    closes = (Decimal("100"),) * 5 + (Decimal("109"),)
    bars = tuple(
        DailyBar(
            stock.code,
            day,
            Decimal("100"),
            closes[index],
            Decimal("99"),
            closes[index],
            Decimal("100") if index == 0 else closes[index - 1],
            Decimal("400") if index == 5 else Decimal("100"),
            Decimal("1000"),
            True,
        )
        for index, day in enumerate(days)
    )
    return FixtureProvider(
        trading_days=days,
        stocks=(stock,),
        bars=bars,
        fundamentals=(
            FundamentalSnapshot(
                stock.code,
                date(2025, 12, 31),
                date(2026, 4, 1),
                Decimal("15"),
                Decimal("1"),
                "fixture",
            ),
        ),
        dividends=(
            DividendRecord(stock.code, date(2025, 6, 1), Decimal("0.1"), "fixture"),
        ),
    )


def test_startup_sync_duplicate_guard_and_offline_screening(tmp_path: Path) -> None:
    provider = _market_fixture()
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    service = DataSyncService(
        provider,
        repository,
        tmp_path / "locks",
        SyncConfig(time(17, 30), timedelta(0), 0, 45, 3),
        clock=lambda: datetime(2026, 8, 25, 18, tzinfo=SHANGHAI),
    )

    outcomes = service.sync_missing_on_startup(
        "market", date(2026, 8, 20), AdjustmentMethod.QFQ
    )
    assert tuple(outcome.trading_day for outcome in outcomes) == tuple(
        date(2026, 8, value) for value in range(20, 26)
    )
    provider_calls_after_sync = provider.calls

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        duplicate = service.sync("market", TARGET_DAY, AdjustmentMethod.QFQ)
    assert duplicate.skipped is True
    assert provider.calls == provider_calls_after_sync

    provider.set_failure("fetch_stocks")
    rules_path = Path(__file__).parents[1] / "config" / "rules.json"
    result = ScreeningService(
        repository, load_rules_config(rules_path)
    ).screen("market", TARGET_DAY, AdjustmentMethod.QFQ, ("sh.600001",))[0]

    assert result.passed is True
    assert provider.calls == provider_calls_after_sync
    assert result.metadata.source == "fixture"
