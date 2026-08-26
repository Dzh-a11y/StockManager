"""Offline tests for the SQLite local repository."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
from stock_manager.protocols import LocalRepositoryProtocol
from stock_manager.storage import SQLiteRepository


DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


def _metadata() -> DatasetMetadata:
    return DatasetMetadata("market", DAY, "fixture", NOW, AdjustmentMethod.QFQ)


def _stock() -> StockIdentity:
    return StockIdentity("sh.600000", "浦发银行", "SSE", False, date(1999, 11, 10), None)


def _bar() -> DailyBar:
    return DailyBar(
        "sh.600000",
        DAY,
        Decimal("10"),
        Decimal("11"),
        Decimal("9"),
        Decimal("10.5"),
        Decimal("10"),
        Decimal("1000"),
        Decimal("10500"),
        True,
    )


def _fundamental() -> FundamentalSnapshot:
    return FundamentalSnapshot(
        "sh.600000", DAY, DAY, Decimal("8.5"), Decimal("0.9"), "fixture"
    )


def _dividend() -> DividendRecord:
    return DividendRecord("sh.600000", date(2025, 6, 1), Decimal("0.3"), "fixture")


def _success() -> SyncRecord:
    return SyncRecord(
        "market", DAY, SyncStatus.SUCCESS, "fixture", AdjustmentMethod.QFQ, NOW, NOW, None
    )


def test_sqlite_repository_conforms_to_protocol(tmp_path: Path) -> None:
    assert isinstance(SQLiteRepository(tmp_path / "market.sqlite3"), LocalRepositoryProtocol)


def test_atomic_snapshot_round_trip(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    repository.save_market_snapshot(
        (_stock(),),
        (_bar(),),
        (_fundamental(),),
        (_dividend(),),
        (date(2026, 8, 24), DAY),
        _metadata(),
        _success(),
    )

    assert repository.get_stocks(DAY) == (_stock(),)
    assert repository.get_daily_bars(("sh.600000",), DAY, DAY, AdjustmentMethod.QFQ) == (_bar(),)
    assert repository.get_fundamentals(("sh.600000",), DAY) == (_fundamental(),)
    assert repository.get_dividends(
        ("sh.600000",), date(2023, 1, 1), DAY
    ) == (_dividend(),)
    assert repository.get_trading_days(date(2026, 8, 1), DAY) == (
        date(2026, 8, 24),
        DAY,
    )
    assert repository.get_sync_record("market", DAY) == _success()
    assert repository.get_dataset_metadata("market", DAY, AdjustmentMethod.QFQ) == _metadata()


def test_repository_keeps_adjustments_separate(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    repository.save_daily_bars((_bar(),), _metadata())
    assert repository.get_daily_bars(
        ("sh.600000",), DAY, DAY, AdjustmentMethod.UNADJUSTED
    ) == ()


def test_snapshot_rejects_mismatched_success_identity_without_writes(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    wrong = SyncRecord(
        "other", DAY, SyncStatus.SUCCESS, "fixture", AdjustmentMethod.QFQ, NOW, NOW, None
    )
    try:
        repository.save_market_snapshot(
            (_stock(),), (_bar(),), (), (), (DAY,), _metadata(), wrong
        )
    except ValueError as error:
        assert "same dataset" in str(error)
    else:
        raise AssertionError("mismatched success identity must fail")
    assert repository.get_stocks(DAY) == ()


def test_prune_before_removes_old_market_data_and_keeps_recent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    old_day = date(2026, 1, 1)
    old_metadata = DatasetMetadata(
        "market", old_day, "fixture", NOW, AdjustmentMethod.QFQ
    )
    old_bar = DailyBar(
        "sh.600000",
        old_day,
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("1"),
        True,
    )

    repository.save_daily_bars((old_bar,), old_metadata)
    repository.save_daily_bars((_bar(),), _metadata())
    repository.save_stocks((_stock(),), old_metadata)
    repository.save_stocks((_stock(),), _metadata())

    repository.prune_before(date(2026, 8, 1))

    assert repository.get_daily_bars(
        ("sh.600000",), old_day, old_day, AdjustmentMethod.QFQ
    ) == ()
    assert repository.get_daily_bars(
        ("sh.600000",), DAY, DAY, AdjustmentMethod.QFQ
    ) == (_bar(),)
    assert repository.get_stocks(old_day) == ()
    assert repository.get_stocks(DAY) == (_stock(),)
    assert repository.get_dataset_metadata("market", old_day, AdjustmentMethod.QFQ) is None
    assert repository.get_dataset_metadata("market", DAY, AdjustmentMethod.QFQ) == _metadata()


def test_backfill_chunk_checkpoint_round_trip_and_idempotency(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    codes = ("sz.000001", "sh.600000", "sh.600001")

    assert repository.completed_chunk_codes("market", DAY, AdjustmentMethod.QFQ) == {}

    repository.mark_chunk_complete("market", DAY, AdjustmentMethod.QFQ, 0, codes)
    repository.mark_chunk_complete("market", DAY, AdjustmentMethod.QFQ, 1, ("sz.300750",))

    completed = repository.completed_chunk_codes("market", DAY, AdjustmentMethod.QFQ)
    assert completed == {
        0: ("sh.600000", "sh.600001", "sz.000001"),  # 按代码字典序存储
        1: ("sz.300750",),
    }

    # 幂等:重复标记同批次不会产生重复行
    repository.mark_chunk_complete("market", DAY, AdjustmentMethod.QFQ, 0, codes)
    assert repository.completed_chunk_codes("market", DAY, AdjustmentMethod.QFQ) == completed

    # 不同复权方式/交易日是独立键
    assert (
        repository.completed_chunk_codes("market", DAY, AdjustmentMethod.UNADJUSTED)
        == {}
    )


def test_prune_before_removes_stale_backfill_checkpoints(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    old_day = date(2026, 1, 1)
    repository.mark_chunk_complete("market", old_day, AdjustmentMethod.QFQ, 0, ("sh.600000",))
    repository.mark_chunk_complete("market", DAY, AdjustmentMethod.QFQ, 0, ("sh.600000",))

    repository.prune_before(date(2026, 8, 1))

    assert repository.completed_chunk_codes("market", old_day, AdjustmentMethod.QFQ) == {}
    assert repository.completed_chunk_codes("market", DAY, AdjustmentMethod.QFQ) == {
        0: ("sh.600000",)
    }
