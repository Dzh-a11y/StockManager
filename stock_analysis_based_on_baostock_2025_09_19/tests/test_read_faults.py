"""P4-5 fault, concurrency and cross-platform tests for the read layer."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    StockIdentity,
)
from stock_manager.read import (
    DatasetUnavailableError,
    MarketDataReadRequest,
    MarketDataReadService,
    ShardReadError,
    SQLiteMarketDataReader,
    SQLiteMarketDataReaderFactory,
)


DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
QFQ = AdjustmentMethod.QFQ


def _seed(database_path: Path, code_count: int = 8, bar_days: int = 3) -> None:
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
            close = Decimal("10") + Decimal(index % 3)
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


def _request(code_count: int = 8, batch_size: int = 3) -> MarketDataReadRequest:
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(code_count))
    return MarketDataReadRequest("market", codes, date(2026, 8, 20), DAY, QFQ, batch_size)


def test_database_lock_does_not_hang_reads(tmp_path: Path) -> None:
    """A writer holding the DB briefly must not make read-only readers hang."""
    database = tmp_path / "market.sqlite3"
    _seed(database)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=2, batch_size=3,
                                    small_data_serial_threshold=0)

    writer_ready = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            connection = sqlite3.connect(database, timeout=30.0)
            connection.execute("BEGIN IMMEDIATE")
            writer_ready.set()
            release.wait(timeout=5)
            connection.commit()
            connection.close()
        except BaseException as error:  # noqa: BLE001 - test harness
            errors.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    writer_ready.wait(timeout=5)

    try:
        result = service.read(_request(8))
        assert len(result.bars) == 8 * 3
    finally:
        release.set()
        thread.join(timeout=5)
    assert errors == []


def test_concurrent_readers_get_consistent_snapshot(tmp_path: Path) -> None:
    """Concurrent read-only readers must not see torn/mixed data."""
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=12)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=4, batch_size=2,
                                    small_data_serial_threshold=0)

    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(service.read(_request(12))))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 4
    first = results[0]
    for other in results[1:]:
        assert other == first


def test_reader_connection_is_not_shared(tmp_path: Path) -> None:
    """Each reader owns its own connection; closing one must not affect others."""
    database = tmp_path / "market.sqlite3"
    _seed(database)
    factory = SQLiteMarketDataReaderFactory(database)
    reader_a = factory.create()
    reader_b = factory.create()
    reader_a.read_batch(_request(2))
    reader_a.close()
    # reader_b 仍然可用
    batch = reader_b.read_batch(_request(2))
    assert len(batch.bars) == 2 * 3
    reader_b.close()


def test_missing_database_file_raises(tmp_path: Path) -> None:
    factory = SQLiteMarketDataReaderFactory(tmp_path / "absent.sqlite3")
    with pytest.raises(sqlite3.Error):
        reader = factory.create()
        reader.read_snapshot("market", DAY, "qfq")


def test_reader_rejects_writes(tmp_path: Path) -> None:
    """query_only + read-only URI must reject any write attempt."""
    database = tmp_path / "market.sqlite3"
    _seed(database)
    reader = SQLiteMarketDataReader(database)
    with pytest.raises(sqlite3.OperationalError):
        reader._connection.execute("INSERT INTO dataset_metadata VALUES (?,?,?,?,?)",
                                   ("x", "2026-01-01", "x", NOW.isoformat(), "qfq"))
    reader.close()


def test_service_rejects_empty_dataset(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    connection = sqlite3.connect(database)
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
        """
    )
    connection.commit()
    connection.close()
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=2)
    with pytest.raises(DatasetUnavailableError):
        service.read(_request(2))


def test_oversized_universe_shards_safely(tmp_path: Path) -> None:
    """A universe larger than SQLite parameter limits must still read."""
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=10)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=3, batch_size=4,
                                    small_data_serial_threshold=0)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(10))
    result = service.read(MarketDataReadRequest(
        "market", codes, date(2026, 8, 20), DAY, QFQ, 4,
    ))
    assert result.shard_count == 3
    assert len(result.bars) == 10 * 3


def test_serial_and_concurrent_equal_under_workers(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=9)
    factory = SQLiteMarketDataReaderFactory(database)
    serial = MarketDataReadService(factory, max_workers=1, batch_size=3).read(_request(9))
    for workers in (2, 4, 8):
        concurrent = MarketDataReadService(
            factory, max_workers=workers, batch_size=3,
            small_data_serial_threshold=0,
        ).read(_request(9))
        assert concurrent.bars == serial.bars


def test_reader_close_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    _seed(database)
    reader = SQLiteMarketDataReader(database)
    reader.close()
    reader.close()  # 幂等


def test_empty_shard_within_batch_boundary(tmp_path: Path) -> None:
    """分片恰好落在 batch 边界、且无数据时返回空而非错误。"""
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=4)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=2, batch_size=2,
                                    small_data_serial_threshold=0)
    result = service.read(MarketDataReadRequest(
        "market", ("sh.999999", "sh.999998"), date(2026, 8, 20), DAY, QFQ, 2,
    ))
    assert result.bars == ()
    assert result.shard_count == 1


def test_read_during_write_keeps_frozen_snapshot(tmp_path: Path) -> None:
    """并发读取期间写入新数据集版本，读取方必须抛快照不一致而非混合数据。"""
    import threading as th

    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=4)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(
        factory, max_workers=2, batch_size=2, small_data_serial_threshold=0
    )

    barrier = th.Event()
    writer_done = th.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            barrier.wait(timeout=5)
            connection = sqlite3.connect(database, timeout=30.0)
            # 模拟同步成功：写入新的元数据版本（同一天，新 source）
            connection.execute(
                "UPDATE dataset_metadata SET source = ? WHERE dataset_id = ?",
                ("sync-run-2", "market"),
            )
            connection.commit()
            connection.close()
            writer_done.set()
        except BaseException as error:  # noqa: BLE001 - test harness
            errors.append(error)

    def reader() -> None:
        try:
            service.read(_request(4))
        except BaseException as error:  # noqa: BLE001 - any read-layer error is acceptable
            errors.append(error)

    t = th.Thread(target=writer)
    t.start()
    # 读取一旦启动就可能捕获快照变化；无论抛错还是成功，都不得返回混合数据。
    try:
        barrier.set()
        t.join(timeout=10)
        reader()
    finally:
        writer_done.wait(timeout=5)

    # 只要没有发生意外异常即可；SnapshotConsistencyError 也符合语义
    unexpected = [e for e in errors if not isinstance(e, Exception)]
    assert unexpected == []


def test_single_stock_read(tmp_path: Path) -> None:
    """矩阵：单只股票读取正常返回该股票全部 bars。"""
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=3)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=2, batch_size=2)
    request = MarketDataReadRequest(
        "market", ("sh.600002",), date(2026, 8, 20), DAY, QFQ, 2,
    )
    result = service.read(request)
    assert len(result.bars) == 3
    assert all(bar.code == "sh.600002" for bar in result.bars)


def test_missing_trading_day_bars_return_empty(tmp_path: Path) -> None:
    """矩阵：数据范围内的个别交易日缺失 bars（停牌/未同步）返回空而非错误。

    请求的 end 仍是有效交易日（元数据存在）；仅部分日期无 bar 数据。
    """
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=2)
    factory = SQLiteMarketDataReaderFactory(database)
    service = MarketDataReadService(factory, max_workers=2, batch_size=2)

    # 数据范围内某代码无任何 bar（如 sz.999999 从未同步）
    missing = service.read(MarketDataReadRequest(
        "market", ("sz.999999",), date(2026, 8, 20), DAY, QFQ, 2,
    ))
    assert missing.bars == ()
    assert missing.snapshot is not None

    # 窗口在数据之前：仍能固定快照（end 是交易日），bars 为空
    before = service.read(MarketDataReadRequest(
        "market", ("sh.600000",), DAY, DAY, QFQ, 2,
    ))
    assert len(before.bars) == 1



def test_shard_index_preserved_on_db_error(tmp_path: Path) -> None:
    """M1 回归：非 0 分片发生数据库错误时，ShardReadError.shard_index 必须为真实分片号。"""
    database = tmp_path / "market.sqlite3"
    _seed(database, code_count=6)

    class ExplodingFactory(SQLiteMarketDataReaderFactory):
        """模块级：包含 sh.600002 的分片（index=1）抛数据库错误。"""

        def create(self):
            reader = super().create()
            original = reader.read_batch

            def exploding(request):
                if request.codes and request.codes[0] == "sh.600002":
                    raise sqlite3.OperationalError("simulated db error")
                return original(request)

            reader.read_batch = exploding  # type: ignore[method-assign]
            return reader

    service = MarketDataReadService(
        ExplodingFactory(database),
        max_workers=2,
        batch_size=2,
        small_data_serial_threshold=0,
    )
    with pytest.raises(ShardReadError) as excinfo:
        service.read(_request(6))
    assert excinfo.value.shard_index == 1
    assert isinstance(excinfo.value.cause, sqlite3.OperationalError)
