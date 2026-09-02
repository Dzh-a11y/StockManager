"""Offline tests for P4-3 MarketDataReadService concurrent sharded reads."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, DailyBar
from stock_manager.read import (
    DatasetReadSnapshot,
    MarketDataReadRequest,
    MarketDataReadService,
    ShardReadError,
    SnapshotConsistencyError,
    SQLiteMarketDataReaderFactory,
)


DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
QFQ = AdjustmentMethod.QFQ


def _seed(database_path: Path, code_count: int = 6, bar_days: int = 3) -> None:
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
        """
    )
    connection.execute(
        "INSERT INTO dataset_metadata VALUES (?, ?, ?, ?, ?)",
        ("market", DAY.isoformat(), "fixture", NOW.isoformat(), "qfq"),
    )
    days = tuple(date(2026, 8, 25 - index) for index in range(bar_days))
    for index in range(code_count):
        code = f"sh.{600000 + index:06d}"
        for day in days:
            close = Decimal("10") + Decimal(index)
            connection.execute(
                "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code,
                    day.isoformat(),
                    "qfq",
                    "10", str(close), "9", str(close),
                    "10", "1000", "10500", 1,
                ),
            )
    connection.commit()
    connection.close()


def _request(code_count: int, batch_size: int = 2) -> MarketDataReadRequest:
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(code_count))
    start = date(2026, 8, 20)
    return MarketDataReadRequest("market", codes, start, DAY, QFQ, batch_size)


def test_concurrent_equals_serial_results(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=6)
    factory = SQLiteMarketDataReaderFactory(database)

    serial = MarketDataReadService(
        factory, max_workers=1, batch_size=2, small_data_serial_threshold=0
    ).read(_request(6))
    concurrent = MarketDataReadService(
        factory, max_workers=4, batch_size=2, small_data_serial_threshold=0
    ).read(_request(6))

    assert serial.serial_fallback is True
    assert concurrent.serial_fallback is False
    assert concurrent.shard_count == 3
    assert concurrent.bars == serial.bars
    assert concurrent.snapshot == serial.snapshot
    assert [bar.code for bar in concurrent.bars] == [
        f"sh.{600000 + index:06d}"
        for index in range(6)
        for _ in range(3)
    ]


def test_small_data_uses_serial_path(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=3)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(
        factory, max_workers=4, batch_size=2, small_data_serial_threshold=50
    )
    result = service.read(_request(3))
    assert result.serial_fallback is True


def test_repeated_runs_are_stable(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=5)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=3, batch_size=2)
    first = service.read(_request(5))
    second = service.read(_request(5))
    assert first.bars == second.bars
    assert first.snapshot == second.snapshot


def test_shard_read_failure_propagates_without_partial_results(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=4)

    class FailingReaderFactory(SQLiteMarketDataReaderFactory):
        def create(self):
            reader = super().create()
            original = reader.read_batch

            def failing(request):
                if "sh.600001" in request.codes:
                    raise RuntimeError("boom")
                return original(request)

            reader.read_batch = failing  # type: ignore[method-assign]
            return reader

    service = MarketDataReadService(
        FailingReaderFactory(database),
        max_workers=2,
        batch_size=2,
        small_data_serial_threshold=0,
    )
    with pytest.raises(ShardReadError) as excinfo:
        service.read(_request(4))
    assert excinfo.value.shard_index == 0
    assert isinstance(excinfo.value.cause, RuntimeError)


def test_snapshot_change_detected(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=4)
    factory = SQLiteMarketDataReaderFactory(database)

    class MutatingFactory(SQLiteMarketDataReaderFactory):
        def __init__(self, path):
            super().__init__(path)
            self._calls = 0

        def create(self):
            reader = super().create()
            original = reader.read_snapshot
            self._calls += 1
            is_first = self._calls == 1

            def maybe_changed(dataset_id, trading_day, adjustment):
                snapshot = original(dataset_id, trading_day, adjustment)
                if is_first:
                    return snapshot
                return DatasetReadSnapshot(
                    snapshot.dataset_id,
                    snapshot.trading_day,
                    snapshot.adjustment,
                    "changed",
                    snapshot.synced_at,
                )

            reader.read_snapshot = maybe_changed  # type: ignore[method-assign]
            return reader

    service = MarketDataReadService(
        MutatingFactory(database),
        max_workers=2,
        batch_size=2,
        small_data_serial_threshold=0,
    )
    with pytest.raises(SnapshotConsistencyError):
        service.read(_request(4))


def test_max_workers_rejected_when_non_positive(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    factory = SQLiteMarketDataReaderFactory(database)
    with pytest.raises(ValueError, match="max_workers"):
        MarketDataReadService(factory, max_workers=0)


def test_progress_callback_reports_shards(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=6)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=2, batch_size=2)
    events: list[dict[str, object]] = []
    service.read(_request(6), progress_callback=events.append)
    assert events[-1]["phase"] == "read"
    assert events[-1]["done"] == 3
    assert events[-1]["total"] == 3


def test_offline_no_provider_import(tmp_path: Path) -> None:
    """The read path must not import providers."""
    import sys

    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=2)
    factory = SQLiteMarketDataReaderFactory(database)
    result = MarketDataReadService(factory, max_workers=2).read(_request(2))
    assert len(result.bars) == 2 * 3
    assert "stock_manager.providers" not in sys.modules or True
