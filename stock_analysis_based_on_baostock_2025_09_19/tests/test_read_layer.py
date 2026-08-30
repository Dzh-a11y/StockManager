"""Offline tests for P4-1 (read contracts/protocols) and P4-2 (SQLite reader)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
)
from stock_manager.read import (
    AdjustmentMismatchError,
    DatasetReadSnapshot,
    DatasetUnavailableError,
    MarketDataBatch,
    MarketDataReadRequest,
    MarketDataReaderFactoryProtocol,
    MarketDataReaderProtocol,
    ShardReadError,
    SQLiteMarketDataReader,
    SQLiteMarketDataReaderFactory,
)
from stock_manager.read.errors import ReadLayerError


DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
QFQ = AdjustmentMethod.QFQ


def _metadata() -> DatasetMetadata:
    return DatasetMetadata("market", DAY, "fixture", NOW, QFQ)


def _snapshot() -> DatasetReadSnapshot:
    return DatasetReadSnapshot.from_metadata(_metadata())


def _bar(code: str = "sh.600000", day: date = DAY) -> DailyBar:
    return DailyBar(
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


def _fundamental(code: str = "sh.600000") -> FundamentalSnapshot:
    return FundamentalSnapshot(code, DAY, DAY, Decimal("8.5"), Decimal("0.9"), "fixture")


def _dividend(code: str = "sh.600000") -> DividendRecord:
    return DividendRecord(code, date(2025, 6, 1), Decimal("0.3"), "fixture")


# ---------- P4-1: request validation ----------


def test_read_request_normalizes_and_deduplicates_codes() -> None:
    request = MarketDataReadRequest(
        "market",
        (" sh.600000 ", "sh.600000", "sz.000001"),
        DAY,
        DAY,
        QFQ,
        500,
    )
    assert request.codes == ("sh.600000", "sz.000001")


def test_read_request_rejects_empty_dataset_id() -> None:
    with pytest.raises(ValueError, match="dataset_id"):
        MarketDataReadRequest(" ", ("sh.600000",), DAY, DAY, QFQ, 500)


def test_read_request_rejects_reversed_dates() -> None:
    with pytest.raises(ValueError, match="start"):
        MarketDataReadRequest("market", ("sh.600000",), DAY, date(2026, 8, 20), QFQ, 500)


def test_read_request_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        MarketDataReadRequest("market", ("sh.600000",), DAY, DAY, QFQ, 0)


def test_read_request_rejects_missing_adjustment() -> None:
    with pytest.raises(ValueError, match="adjustment"):
        MarketDataReadRequest("market", ("sh.600000",), DAY, DAY, None, 500)  # type: ignore[arg-type]


def test_read_request_rejects_dividend_window_after_end() -> None:
    with pytest.raises(ValueError, match="dividends_start"):
        MarketDataReadRequest(
            "market", ("sh.600000",), DAY, DAY, QFQ, 500, dividends_start=date(2026, 8, 26),
        )


def test_snapshot_requires_aware_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DatasetReadSnapshot("market", DAY, QFQ, "fixture", datetime(2026, 8, 25, 18))


def test_batch_rejects_bar_outside_shard_codes() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="outside the shard"):
        MarketDataBatch(snapshot, ("sh.600000",), (_bar("sz.000001"),), (), ())


# ---------- P4-2: SQLite reader ----------


def _seed(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE dataset_metadata (
            dataset_id TEXT NOT NULL,
            trading_day TEXT NOT NULL,
            source TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            adjustment TEXT NOT NULL,
            PRIMARY KEY (dataset_id, trading_day, adjustment)
        );
        CREATE TABLE daily_bars (
            code TEXT NOT NULL,
            trading_day TEXT NOT NULL,
            adjustment TEXT NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            preclose TEXT NOT NULL,
            volume TEXT NOT NULL,
            amount TEXT NOT NULL,
            is_trading INTEGER NOT NULL,
            PRIMARY KEY (code, trading_day, adjustment)
        );
        CREATE TABLE fundamentals (
            code TEXT NOT NULL,
            report_date TEXT NOT NULL,
            published_on TEXT NOT NULL,
            pe_ttm TEXT,
            pb TEXT,
            source TEXT NOT NULL,
            PRIMARY KEY (code, report_date, published_on)
        );
        CREATE TABLE dividends (
            code TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            cash_dividend_per_share TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (code, ex_date, source)
        );
        """
    )
    connection.execute(
        "INSERT INTO dataset_metadata VALUES (?, ?, ?, ?, ?)",
        ("market", DAY.isoformat(), "fixture", NOW.isoformat(), "qfq"),
    )
    for code in ("sh.600000", "sz.000001"):
        connection.execute(
            "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                DAY.isoformat(),
                "qfq",
                "10", "11", "9", "10.5", "10", "1000", "10500", 1,
            ),
        )
        connection.execute(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?)",
            (code, DAY.isoformat(), DAY.isoformat(), "8.5", "0.9", "fixture"),
        )
        connection.execute(
            "INSERT INTO dividends VALUES (?, ?, ?, ?)",
            (code, "2025-06-01", "0.3", "fixture"),
        )
    connection.commit()
    connection.close()


def test_reader_reads_ordered_bars_and_optional_data(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    factory = SQLiteMarketDataReaderFactory(database)
    assert isinstance(factory, MarketDataReaderFactoryProtocol)
    with factory.create() as reader:
        assert isinstance(reader, MarketDataReaderProtocol)
        request = MarketDataReadRequest(
            "market",
            ("sz.000001", "sh.600000"),
            DAY,
            DAY,
            QFQ,
            500,
            include_fundamentals=True,
            dividends_start=date(2025, 1, 1),
        )
        batch = reader.read_batch(request)
        assert batch.codes == ("sz.000001", "sh.600000")
        assert [bar.code for bar in batch.bars] == ["sh.600000", "sz.000001"]
        assert len(batch.fundamentals) == 2
        assert len(batch.dividends) == 2


def test_reader_skips_unrequested_data(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        request = MarketDataReadRequest(
            "market", ("sh.600000",), DAY, DAY, QFQ, 500,
        )
        batch = reader.read_batch(request)
        assert batch.fundamentals == ()
        assert batch.dividends == ()


def test_reader_empty_codes_returns_empty_batch(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        request = MarketDataReadRequest(
            "market", (), DAY, DAY, QFQ, 500,
        )
        batch = reader.read_batch(request)
        assert batch.bars == ()
        assert batch.fundamentals == ()
        assert batch.dividends == ()


def test_reader_raises_dataset_unavailable_for_missing_metadata(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        with pytest.raises(DatasetUnavailableError):
            reader.read_snapshot("market", date(2099, 1, 1), "qfq")


def test_reader_adjustment_mismatch_raises(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        request = MarketDataReadRequest(
            "market", ("sh.600000",), DAY, DAY, AdjustmentMethod.HFQ, 500,
        )
        with pytest.raises(AdjustmentMismatchError):
            reader.read_batch(request)


def test_reader_fundamental_keeps_latest_published(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?)",
        ("sh.600000", "2025-12-31", "2026-04-01", "12.0", "1.2", "fixture"),
    )
    connection.commit()
    connection.close()
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        request = MarketDataReadRequest(
            "market", ("sh.600000",), DAY, DAY, QFQ, 500, include_fundamentals=True,
        )
        batch = reader.read_batch(request)
        assert len(batch.fundamentals) == 1
        assert batch.fundamentals[0].report_date == date(2026, 8, 25)


def test_reader_chunks_large_code_lists(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    codes = tuple(f"sz.{200000 + index:06d}" for index in range(1200))
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        request = MarketDataReadRequest(
            "market", codes, DAY, DAY, QFQ, 200,
        )
        batch = reader.read_batch(request)
        assert batch.bars == ()  # 种子数据库没有这些代码


def test_reader_is_strictly_read_only(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    before = database.read_bytes()
    with SQLiteMarketDataReaderFactory(database).create() as reader:
        reader.read_batch(
            MarketDataReadRequest("market", ("sh.600000",), DAY, DAY, QFQ, 500),
        )
    after = database.read_bytes()
    assert before == after


def test_sqlite_reader_is_database_agnostic_surface() -> None:
    import inspect

    source = inspect.getsource(SQLiteMarketDataReader)
    assert "sqlite3.Connection" not in [
        item.annotation for item in inspect.signature(
            SQLiteMarketDataReader.read_batch
        ).parameters.values()
    ]
    assert isinstance(ReadLayerError, type)
