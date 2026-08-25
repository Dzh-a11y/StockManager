"""SQLite implementation of the local repository contract."""

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

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


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT NOT NULL,
    as_of TEXT NOT NULL,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    is_st INTEGER NOT NULL CHECK (is_st IN (0, 1)),
    listed_on TEXT,
    delisted_on TEXT,
    PRIMARY KEY (code, as_of)
);
CREATE TABLE IF NOT EXISTS daily_bars (
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
    is_trading INTEGER NOT NULL CHECK (is_trading IN (0, 1)),
    PRIMARY KEY (code, trading_day, adjustment)
);
CREATE TABLE IF NOT EXISTS fundamentals (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    published_on TEXT NOT NULL,
    pe_ttm TEXT,
    pb TEXT,
    source TEXT NOT NULL,
    PRIMARY KEY (code, report_date, published_on)
);
CREATE TABLE IF NOT EXISTS dividends (
    code TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    cash_dividend_per_share TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (code, ex_date, source)
);
CREATE TABLE IF NOT EXISTS trading_days (
    trading_day TEXT PRIMARY KEY,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    dataset_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT,
    PRIMARY KEY (dataset_id, trading_day)
);
CREATE TABLE IF NOT EXISTS dataset_metadata (
    dataset_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    PRIMARY KEY (dataset_id, trading_day, adjustment)
);
"""


class SQLiteRepository:
    """Persist normalized market data locally using short SQLite transactions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _save_metadata(connection: sqlite3.Connection, metadata: DatasetMetadata) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO dataset_metadata
               (dataset_id, trading_day, source, synced_at, adjustment)
               VALUES (?, ?, ?, ?, ?)""",
            (
                metadata.dataset_id,
                metadata.trading_day.isoformat(),
                metadata.source,
                metadata.synced_at.isoformat(),
                metadata.adjustment.value,
            ),
        )

    @staticmethod
    def _save_stocks(
        connection: sqlite3.Connection,
        stocks: Sequence[StockIdentity],
        metadata: DatasetMetadata,
    ) -> None:
        connection.executemany(
            """INSERT OR REPLACE INTO stocks
               (code, as_of, name, exchange, is_st, listed_on, delisted_on)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    stock.code,
                    metadata.trading_day.isoformat(),
                    stock.name,
                    stock.exchange,
                    int(stock.is_st),
                    None if stock.listed_on is None else stock.listed_on.isoformat(),
                    None if stock.delisted_on is None else stock.delisted_on.isoformat(),
                )
                for stock in stocks
            ],
        )

    @staticmethod
    def _save_bars(
        connection: sqlite3.Connection,
        bars: Sequence[DailyBar],
        metadata: DatasetMetadata,
    ) -> None:
        connection.executemany(
            """INSERT OR REPLACE INTO daily_bars
               (code, trading_day, adjustment, open, high, low, close, preclose,
                volume, amount, is_trading)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    bar.code,
                    bar.trading_day.isoformat(),
                    metadata.adjustment.value,
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.preclose),
                    str(bar.volume),
                    str(bar.amount),
                    int(bar.is_trading),
                )
                for bar in bars
            ],
        )

    @staticmethod
    def _save_fundamentals(
        connection: sqlite3.Connection,
        items: Sequence[FundamentalSnapshot],
    ) -> None:
        connection.executemany(
            """INSERT OR REPLACE INTO fundamentals
               (code, report_date, published_on, pe_ttm, pb, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    item.code,
                    item.report_date.isoformat(),
                    item.published_on.isoformat(),
                    None if item.pe_ttm is None else str(item.pe_ttm),
                    None if item.pb is None else str(item.pb),
                    item.source,
                )
                for item in items
            ],
        )

    @staticmethod
    def _save_dividends(
        connection: sqlite3.Connection,
        items: Sequence[DividendRecord],
    ) -> None:
        connection.executemany(
            """INSERT OR REPLACE INTO dividends
               (code, ex_date, cash_dividend_per_share, source)
               VALUES (?, ?, ?, ?)""",
            [
                (item.code, item.ex_date.isoformat(), str(item.cash_dividend_per_share), item.source)
                for item in items
            ],
        )

    @staticmethod
    def _save_trading_days(
        connection: sqlite3.Connection,
        days: Sequence[date],
        source: str,
    ) -> None:
        connection.executemany(
            "INSERT OR REPLACE INTO trading_days (trading_day, source) VALUES (?, ?)",
            [(day.isoformat(), source) for day in days],
        )

    @staticmethod
    def _save_sync_record(connection: sqlite3.Connection, record: SyncRecord) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO sync_runs
               (dataset_id, trading_day, status, source, adjustment, started_at,
                finished_at, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.dataset_id,
                record.trading_day.isoformat(),
                record.status.value,
                record.source,
                record.adjustment.value,
                record.started_at.isoformat(),
                None if record.finished_at is None else record.finished_at.isoformat(),
                record.error_message,
            ),
        )

    def save_stocks(self, stocks: Sequence[StockIdentity], metadata: DatasetMetadata) -> None:
        with self._connect() as connection:
            self._save_stocks(connection, stocks, metadata)
            self._save_metadata(connection, metadata)

    def save_daily_bars(self, bars: Sequence[DailyBar], metadata: DatasetMetadata) -> None:
        with self._connect() as connection:
            self._save_bars(connection, bars, metadata)
            self._save_metadata(connection, metadata)

    def save_fundamentals(
        self, items: Sequence[FundamentalSnapshot], metadata: DatasetMetadata
    ) -> None:
        with self._connect() as connection:
            self._save_fundamentals(connection, items)
            self._save_metadata(connection, metadata)

    def save_dividends(
        self, items: Sequence[DividendRecord], metadata: DatasetMetadata
    ) -> None:
        with self._connect() as connection:
            self._save_dividends(connection, items)
            self._save_metadata(connection, metadata)

    def save_trading_days(self, days: Sequence[date], metadata: DatasetMetadata) -> None:
        with self._connect() as connection:
            self._save_trading_days(connection, days, metadata.source)
            self._save_metadata(connection, metadata)

    def save_sync_record(self, record: SyncRecord) -> None:
        with self._connect() as connection:
            self._save_sync_record(connection, record)

    def save_market_snapshot(
        self,
        stocks: Sequence[StockIdentity],
        bars: Sequence[DailyBar],
        fundamentals: Sequence[FundamentalSnapshot],
        dividends: Sequence[DividendRecord],
        trading_days: Sequence[date],
        metadata: DatasetMetadata,
        success_record: SyncRecord,
    ) -> None:
        if success_record.status is not SyncStatus.SUCCESS:
            raise ValueError("save_market_snapshot requires a SUCCESS record")
        if (
            success_record.dataset_id != metadata.dataset_id
            or success_record.trading_day != metadata.trading_day
            or success_record.adjustment is not metadata.adjustment
        ):
            raise ValueError("success record and metadata must identify the same dataset")
        with self._connect() as connection:
            self._save_stocks(connection, stocks, metadata)
            self._save_bars(connection, bars, metadata)
            self._save_fundamentals(connection, fundamentals)
            self._save_dividends(connection, dividends)
            self._save_trading_days(connection, trading_days, metadata.source)
            self._save_metadata(connection, metadata)
            self._save_sync_record(connection, success_record)

    def save_trading_calendar(
        self,
        days: Sequence[date],
        metadata: DatasetMetadata,
        success_record: SyncRecord,
    ) -> None:
        if success_record.status is not SyncStatus.SUCCESS:
            raise ValueError("save_trading_calendar requires a SUCCESS record")
        if (
            success_record.dataset_id != metadata.dataset_id
            or success_record.trading_day != metadata.trading_day
            or metadata.adjustment is not AdjustmentMethod.UNADJUSTED
        ):
            raise ValueError("calendar record and metadata must identify the same dataset")
        with self._connect() as connection:
            self._save_trading_days(connection, days, metadata.source)
            self._save_metadata(connection, metadata)
            self._save_sync_record(connection, success_record)

    def get_stocks(self, as_of: date) -> Sequence[StockIdentity]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(as_of) AS snapshot_day FROM stocks WHERE as_of <= ?",
                (as_of.isoformat(),),
            ).fetchone()
            if row is None or row["snapshot_day"] is None:
                return ()
            rows = connection.execute(
                "SELECT * FROM stocks WHERE as_of = ? ORDER BY code", (row["snapshot_day"],)
            ).fetchall()
        return tuple(
            StockIdentity(
                row["code"],
                row["name"],
                row["exchange"],
                bool(row["is_st"]),
                None if row["listed_on"] is None else date.fromisoformat(row["listed_on"]),
                None if row["delisted_on"] is None else date.fromisoformat(row["delisted_on"]),
            )
            for row in rows
        )

    def get_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]:
        if not codes:
            return ()
        placeholders = ",".join("?" for _ in codes)
        query = f"""SELECT * FROM daily_bars WHERE code IN ({placeholders})
                    AND trading_day BETWEEN ? AND ? AND adjustment = ?
                    ORDER BY code, trading_day"""
        parameters = (*codes, start.isoformat(), end.isoformat(), adjustment.value)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            DailyBar(
                row["code"],
                date.fromisoformat(row["trading_day"]),
                Decimal(row["open"]),
                Decimal(row["high"]),
                Decimal(row["low"]),
                Decimal(row["close"]),
                Decimal(row["preclose"]),
                Decimal(row["volume"]),
                Decimal(row["amount"]),
                bool(row["is_trading"]),
            )
            for row in rows
        )

    def get_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]:
        if not codes:
            return ()
        placeholders = ",".join("?" for _ in codes)
        query = f"""SELECT * FROM fundamentals WHERE code IN ({placeholders})
                    AND published_on <= ? ORDER BY code, published_on"""
        with self._connect() as connection:
            rows = connection.execute(query, (*codes, as_of.isoformat())).fetchall()
        latest: dict[str, sqlite3.Row] = {row["code"]: row for row in rows}
        return tuple(
            FundamentalSnapshot(
                row["code"],
                date.fromisoformat(row["report_date"]),
                date.fromisoformat(row["published_on"]),
                None if row["pe_ttm"] is None else Decimal(row["pe_ttm"]),
                None if row["pb"] is None else Decimal(row["pb"]),
                row["source"],
            )
            for row in latest.values()
        )

    def get_dividends(
        self, codes: Sequence[str], start: date, end: date
    ) -> Sequence[DividendRecord]:
        if not codes:
            return ()
        placeholders = ",".join("?" for _ in codes)
        query = f"""SELECT * FROM dividends WHERE code IN ({placeholders})
                    AND ex_date BETWEEN ? AND ? ORDER BY code, ex_date"""
        with self._connect() as connection:
            rows = connection.execute(query, (*codes, start.isoformat(), end.isoformat())).fetchall()
        return tuple(
            DividendRecord(
                row["code"],
                date.fromisoformat(row["ex_date"]),
                Decimal(row["cash_dividend_per_share"]),
                row["source"],
            )
            for row in rows
        )

    def get_sync_record(self, dataset_id: str, trading_day: date) -> SyncRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_runs WHERE dataset_id = ? AND trading_day = ?",
                (dataset_id, trading_day.isoformat()),
            ).fetchone()
        return None if row is None else self._sync_record_from_row(row)

    def get_latest_sync_record(self, dataset_id: str) -> SyncRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM sync_runs WHERE dataset_id = ?
                   ORDER BY trading_day DESC LIMIT 1""",
                (dataset_id,),
            ).fetchone()
        return None if row is None else self._sync_record_from_row(row)

    @staticmethod
    def _sync_record_from_row(row: sqlite3.Row) -> SyncRecord:
        return SyncRecord(
            row["dataset_id"],
            date.fromisoformat(row["trading_day"]),
            SyncStatus(row["status"]),
            row["source"],
            AdjustmentMethod(row["adjustment"]),
            datetime.fromisoformat(row["started_at"]),
            None if row["finished_at"] is None else datetime.fromisoformat(row["finished_at"]),
            row["error_message"],
        )

    def get_dataset_metadata(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
    ) -> DatasetMetadata | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM dataset_metadata
                   WHERE dataset_id = ? AND trading_day = ? AND adjustment = ?""",
                (dataset_id, trading_day.isoformat(), adjustment.value),
            ).fetchone()
        return None if row is None else self._metadata_from_row(row)

    def get_latest_dataset_metadata(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> DatasetMetadata | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM dataset_metadata
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY trading_day DESC LIMIT 1""",
                (dataset_id, adjustment.value),
            ).fetchone()
        return None if row is None else self._metadata_from_row(row)

    @staticmethod
    def _metadata_from_row(row: sqlite3.Row) -> DatasetMetadata:
        return DatasetMetadata(
            row["dataset_id"],
            date.fromisoformat(row["trading_day"]),
            row["source"],
            datetime.fromisoformat(row["synced_at"]),
            AdjustmentMethod(row["adjustment"]),
        )

    def get_trading_days(self, start: date, end: date) -> Sequence[date]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT trading_day FROM trading_days
                   WHERE trading_day BETWEEN ? AND ? ORDER BY trading_day""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return tuple(date.fromisoformat(row["trading_day"]) for row in rows)

    def prune_before(self, cutoff: date) -> None:
        """Delete market data and its sync bookkeeping strictly older than ``cutoff``.

        ``daily_bars`` and ``stocks`` grow one row per stock per trading day, so they
        are the unbounded tables this retention policy is designed to bound. The
        matching ``sync_runs`` and ``dataset_metadata`` rows are removed together so a
        pruned day is never mistaken for a successfully-synced day later.
        """
        cutoff_text = cutoff.isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM daily_bars WHERE trading_day < ?", (cutoff_text,)
            )
            connection.execute(
                "DELETE FROM stocks WHERE as_of < ?", (cutoff_text,)
            )
            connection.execute(
                "DELETE FROM sync_runs WHERE trading_day < ?", (cutoff_text,)
            )
            connection.execute(
                "DELETE FROM dataset_metadata WHERE trading_day < ?", (cutoff_text,)
            )
