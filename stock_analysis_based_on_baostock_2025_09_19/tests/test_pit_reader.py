"""P5A-2 point-in-time reader tests: no lookahead, universe boundaries."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DatasetVersion,
    DatasetVersionStatus,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
)
from stock_manager.read.historical import (
    PointInTimeRequest,
    SQLitePointInTimeReader,
)
from stock_manager.storage import SQLiteRepository

QFQ = AdjustmentMethod.QFQ
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)


def _metadata(day: date) -> DatasetMetadata:
    return DatasetMetadata("market", day, "fixture", NOW, QFQ)


def _bar(code: str, day: date, close: str = "10.5") -> DailyBar:
    return DailyBar(
        code,
        day,
        Decimal("10"),
        Decimal("11"),
        Decimal("9"),
        Decimal(close),
        Decimal("10"),
        Decimal("1000"),
        Decimal("10500"),
        True,
    )


def _seed(tmp_path: Path) -> Path:
    """A, B (listed later), C (delisted earlier); late fundamentals/bars/dividends."""
    db = tmp_path / "market.sqlite3"
    repo = SQLiteRepository(db)
    snapshot_day = date(2020, 1, 10)
    stock_a = StockIdentity("000001.SZ", "平安银行", "SZSE", False, date(2019, 1, 2), None)
    stock_b = StockIdentity("000002.SZ", "万科A", "SZSE", False, date(2020, 1, 15), None)
    stock_c = StockIdentity("000003.SZ", "旧股", "SZSE", False, date(2019, 6, 3), date(2019, 12, 31))
    repo.save_stocks((stock_a, stock_b, stock_c), _metadata(snapshot_day))
    # 更早快照(2019-12-01):B 尚未上市,快照只有 A 与 C
    repo.save_stocks(
        (
            StockIdentity("000001.SZ", "平安银行", "SZSE", False, date(2019, 1, 2), None),
            StockIdentity("000003.SZ", "旧股", "SZSE", False, date(2019, 6, 3), date(2019, 12, 31)),
        ),
        _metadata(date(2019, 12, 1)),
    )
    repo.save_trading_days(
        (
            date(2020, 1, 2),
            date(2020, 1, 3),
            date(2020, 1, 6),
            date(2020, 1, 7),
            date(2020, 1, 8),
            date(2020, 1, 9),
            date(2020, 1, 10),
            date(2020, 1, 13),
            date(2020, 1, 14),
            date(2020, 1, 15),
            date(2020, 1, 16),
            date(2020, 1, 17),
            date(2020, 1, 20),
        ),
        _metadata(snapshot_day),
    )
    repo.save_daily_bars(
        (_bar("000001.SZ", date(2020, 1, 15)), _bar("000001.SZ", date(2020, 1, 16))),
        _metadata(snapshot_day),
    )
    repo.save_fundamentals(
        (
            FundamentalSnapshot(
                "000001.SZ",
                date(2019, 12, 31),
                date(2020, 1, 16),  # published after the 15th
                Decimal("8"),
                Decimal("1"),
                "fixture",
            ),
        ),
        _metadata(snapshot_day),
    )
    repo.save_dividends(
        (
            DividendRecord(
                "000001.SZ", date(2020, 1, 20), Decimal("0.2"), "fixture"
            ),
        ),
        _metadata(snapshot_day),
    )
    return db


def _reader(db: Path, start: date, end: date) -> SQLitePointInTimeReader:
    request = PointInTimeRequest("market", (), start, end, QFQ)
    return SQLitePointInTimeReader(db, request)


def test_later_listed_stock_is_invisible_before_listing(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        universe = reader.universe_as_of(date(2020, 1, 14))
        codes = {stock.code for stock in universe}
        assert "000002.SZ" not in codes  # listed 2020-01-15
        assert "000001.SZ" in codes
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        universe = reader.universe_as_of(date(2020, 1, 16))
        assert "000002.SZ" in {stock.code for stock in universe}


def test_delisted_stock_visible_before_but_not_after_delisting(
    tmp_path: Path,
) -> None:
    db = _seed(tmp_path)
    with _reader(db, date(2019, 1, 1), date(2020, 3, 31)) as reader:
        codes_before = {
            stock.code for stock in reader.universe_as_of(date(2019, 12, 1))
        }
        codes_after = {
            stock.code for stock in reader.universe_as_of(date(2020, 1, 14))
        }
        assert "000003.SZ" in codes_before
        assert "000003.SZ" not in codes_after


def test_late_published_fundamentals_invisible_before_publish(
    tmp_path: Path,
) -> None:
    db = _seed(tmp_path)
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        assert reader.fundamentals_through(date(2020, 1, 15)) == ()
        published = reader.fundamentals_through(date(2020, 1, 16))
        assert len(published) == 1
        assert published[0].published_on == date(2020, 1, 16)


def test_future_bars_and_dividends_invisible(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        assert reader.bars_through(date(2020, 1, 14)) == ()
        assert len(reader.bars_through(date(2020, 1, 15))) == 1
        assert reader.dividends_through(date(2020, 1, 19)) == ()
        assert len(reader.dividends_through(date(2020, 1, 20))) == 1


def test_universe_query_outside_window_rejected(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        with pytest.raises(ValueError, match="outside the request window"):
            reader.universe_as_of(date(2020, 2, 1))


def test_codes_filtering(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    request = PointInTimeRequest("market", ("000001.SZ",), date(2020, 1, 1), date(2020, 1, 31), QFQ)
    with SQLitePointInTimeReader(db, request) as reader:
        bars = reader.bars_through(date(2020, 1, 16))
        assert {bar.code for bar in bars} == {"000001.SZ"}


def test_request_validation() -> None:
    with pytest.raises(ValueError, match="dataset_id must not be empty"):
        PointInTimeRequest("  ", (), date(2020, 1, 1), date(2020, 1, 31), QFQ)
    with pytest.raises(ValueError, match="start must not be after end"):
        PointInTimeRequest("market", (), date(2020, 2, 1), date(2020, 1, 31), QFQ)


def test_committed_generation_and_fingerprint(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    repo = SQLiteRepository(db)
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        assert reader.committed_generation() is None
        fingerprint_before = reader.data_fingerprint()
    version = DatasetVersion(
        "market",
        "market-2020-01-31-abc12345",
        "fixture",
        QFQ,
        NOW,
        DatasetVersionStatus.COMPLETE,
        date(2020, 1, 1),
        date(2020, 1, 31),
    )
    repo.save_dataset_version(version)
    with _reader(db, date(2020, 1, 1), date(2020, 1, 31)) as reader:
        assert reader.committed_generation() == "market-2020-01-31-abc12345"
        fingerprint_after = reader.data_fingerprint()
        assert fingerprint_after != fingerprint_before
        # 同一状态指纹稳定
        assert reader.data_fingerprint() == fingerprint_after


def test_closed_reader_rejects_use(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    reader = _reader(db, date(2020, 1, 1), date(2020, 1, 31))
    reader.close()
    reader.close()  # idempotent
    with pytest.raises(RuntimeError, match="reader is closed"):
        reader.universe_as_of(date(2020, 1, 14))
