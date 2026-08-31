"""SQLite implementation of the local repository contract."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from stock_manager.domain import (
    AdjustmentMethod,
    BackfillChunkV2,
    BackfillRunStatus,
    BackfillRunV2,
    DailyBar,
    DataCoverageStatus,
    DatasetCoverage,
    DatasetMetadata,
    DatasetVersion,
    DatasetVersionStatus,
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
CREATE TABLE IF NOT EXISTS backfill_chunks (
    dataset_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    codes TEXT NOT NULL,
    PRIMARY KEY (dataset_id, trading_day, adjustment, chunk_index)
);
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    coverage_start TEXT,
    coverage_end TEXT,
    PRIMARY KEY (dataset_id, generation)
);
CREATE TABLE IF NOT EXISTS dataset_coverage (
    dataset_id TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    data_type TEXT NOT NULL,
    earliest_day TEXT,
    latest_day TEXT,
    status TEXT NOT NULL,
    gap_days TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, adjustment, data_type)
);
CREATE TABLE IF NOT EXISTS backfill_runs_v2 (
    run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    target_start TEXT NOT NULL,
    target_end TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT,
    data_types TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backfill_chunks_v2 (
    run_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    codes TEXT NOT NULL,
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    bar_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, chunk_index, range_start, range_end)
);
"""


class SQLiteRepository:
    """Persist normalized market data locally using short SQLite transactions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @property
    def database_path(self) -> Path:
        """The on-disk SQLite file this repository owns (read-only)."""
        return self._database_path

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

    @staticmethod
    def _chunk_codes_key(codes: Sequence[str]) -> str:
        return ",".join(sorted(codes))

    def mark_chunk_complete(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        chunk_index: int,
        codes: Sequence[str],
    ) -> None:
        """Record that a backfill code chunk was fully persisted.

        The sorted code list is stored so a later resume only trusts the
        checkpoint when the current chunk contains exactly the same codes;
        this stays correct even if the provider's stock-list ordering shifts.
        """
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO backfill_chunks
                   (dataset_id, trading_day, adjustment, chunk_index, codes)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    dataset_id,
                    trading_day.isoformat(),
                    adjustment.value,
                    chunk_index,
                    self._chunk_codes_key(codes),
                ),
            )

    def completed_chunk_codes(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
    ) -> dict[int, tuple[str, ...]]:
        """Return backfill checkpoints as ``{chunk_index: sorted codes}``."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT chunk_index, codes FROM backfill_chunks
                   WHERE dataset_id = ? AND trading_day = ? AND adjustment = ?
                   ORDER BY chunk_index""",
                (dataset_id, trading_day.isoformat(), adjustment.value),
            ).fetchall()
        return {
            int(row["chunk_index"]): tuple(row["codes"].split(",")) for row in rows
        }

    def latest_backfill_cover_date(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> date | None:
        """Return the newest backfill as_of recorded in checkpoints, if any.

        This is the "covered through" marker for incremental tail backfills:
        a later run only needs to fetch trading days after this date.
        """
        with self._connect() as connection:
            row = connection.execute(
                """SELECT MAX(trading_day) AS cover FROM backfill_chunks
                   WHERE dataset_id = ? AND adjustment = ?""",
                (dataset_id, adjustment.value),
            ).fetchone()
        cover = row["cover"]
        return None if cover is None else date.fromisoformat(cover)

    def daily_bar_days(
        self,
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> set[date]:
        """Return the set of trading days that have daily bars in the range."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT trading_day FROM daily_bars
                   WHERE adjustment = ? AND trading_day BETWEEN ? AND ?""",
                (adjustment.value, start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(row["trading_day"]) for row in rows}

    def daily_bar_stock_counts(
        self,
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> dict[date, int]:
        """Return ``{trading_day: distinct bar code count}`` for days with bars.

        Used to decide whether a day was fully synchronised (its bar universe
        covers the stock pool) versus partially pulled.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT trading_day, COUNT(DISTINCT code) AS n FROM daily_bars
                   WHERE adjustment = ? AND trading_day BETWEEN ? AND ?
                   GROUP BY trading_day""",
                (adjustment.value, start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(row["trading_day"]): int(row["n"]) for row in rows}

    def earliest_failed_day(
        self,
        dataset_id: str,
        start: date,
        end: date,
    ) -> date | None:
        """Return the earliest sync day marked FAILED in ``[start, end]``.

        Used by the backfill incremental tail so a day that partially failed but
        was not yet re-run does not get skipped over by a later checkpoint's
        ``covered_end``.
        """
        with self._connect() as connection:
            row = connection.execute(
                """SELECT MIN(trading_day) AS day FROM sync_runs
                   WHERE dataset_id = ? AND status = ?
                     AND trading_day BETWEEN ? AND ?""",
                (
                    dataset_id,
                    SyncStatus.FAILED.value,
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchone()
        day = row["day"]
        return None if day is None else date.fromisoformat(day)

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
        matching ``sync_runs``, ``dataset_metadata`` and ``backfill_chunks`` rows are
        removed together so a pruned day is never mistaken for a successfully-synced
        day later.
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
            connection.execute(
                "DELETE FROM backfill_chunks WHERE trading_day < ?", (cutoff_text,)
            )
    # ------------------------------------------------------------------
    # P5A-1 v2 history backfill / coverage / generation bookkeeping
    # ------------------------------------------------------------------

    def save_backfill_run_v2(self, run: BackfillRunV2) -> None:
        """Upsert one v2 backfill run (range-bound identity via run_id)."""
        with self._connect() as connection:
            self._save_backfill_run_v2(connection, run)

    @staticmethod
    def _save_backfill_run_v2(
        connection: sqlite3.Connection, run: BackfillRunV2
    ) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO backfill_runs_v2
               (run_id, dataset_id, adjustment, target_start, target_end,
                status, started_at, finished_at, error_message, data_types)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id,
                run.dataset_id,
                run.adjustment.value,
                run.target_start.isoformat(),
                run.target_end.isoformat(),
                run.status.value,
                run.started_at.isoformat(),
                None if run.finished_at is None else run.finished_at.isoformat(),
                run.error_message,
                ",".join(run.data_types),
            ),
        )

    def get_backfill_run_v2(self, run_id: str) -> BackfillRunV2 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backfill_runs_v2 WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return self._backfill_run_v2_from_row(row)

    def list_backfill_runs_v2(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> Sequence[BackfillRunV2]:
        """Return all v2 runs for a dataset/adjustment, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM backfill_runs_v2
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY started_at DESC""",
                (dataset_id, adjustment.value),
            ).fetchall()
        return tuple(self._backfill_run_v2_from_row(row) for row in rows)

    @staticmethod
    def _backfill_run_v2_from_row(row: sqlite3.Row) -> BackfillRunV2:
        return BackfillRunV2(
            run_id=row["run_id"],
            dataset_id=row["dataset_id"],
            adjustment=AdjustmentMethod(row["adjustment"]),
            target_start=date.fromisoformat(row["target_start"]),
            target_end=date.fromisoformat(row["target_end"]),
            status=BackfillRunStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=(
                None if row["finished_at"] is None
                else datetime.fromisoformat(row["finished_at"])
            ),
            error_message=row["error_message"],
            data_types=tuple(row["data_types"].split(",")),
        )

    def save_backfill_chunk_v2(self, chunk: BackfillChunkV2) -> None:
        """Upsert one completed v2 code chunk checkpoint."""
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO backfill_chunks_v2
                   (run_id, chunk_index, codes, range_start, range_end,
                    bar_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk.run_id,
                    chunk.chunk_index,
                    ",".join(chunk.codes),
                    chunk.range_start.isoformat(),
                    chunk.range_end.isoformat(),
                    chunk.bar_count,
                    chunk.status.value,
                ),
            )

    def completed_chunk_codes_v2(
        self, run_id: str
    ) -> dict[int, list[BackfillChunkV2]]:
        """Return v2 checkpoints grouped by chunk index for a run.

        Each chunk may have one checkpoint per fetched gap range; the caller
        must match both codes and the exact range before skipping.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM backfill_chunks_v2
                   WHERE run_id = ? ORDER BY chunk_index, range_start""",
                (run_id,),
            ).fetchall()
        grouped: dict[int, list[BackfillChunkV2]] = {}
        for row in rows:
            index = int(row["chunk_index"])
            grouped.setdefault(index, []).append(
                BackfillChunkV2(
                    run_id=row["run_id"],
                    chunk_index=index,
                    codes=tuple(row["codes"].split(",")),
                    range_start=date.fromisoformat(row["range_start"]),
                    range_end=date.fromisoformat(row["range_end"]),
                    bar_count=int(row["bar_count"]),
                    status=BackfillRunStatus(row["status"]),
                )
            )
        return grouped

    def get_backfill_chunk_v2(
        self, run_id: str, chunk_index: int
    ) -> BackfillChunkV2 | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM backfill_chunks_v2
                   WHERE run_id = ? AND chunk_index = ?""",
                (run_id, chunk_index),
            ).fetchone()
        if row is None:
            return None
        return BackfillChunkV2(
            run_id=row["run_id"],
            chunk_index=int(row["chunk_index"]),
            codes=tuple(row["codes"].split(",")),
            range_start=date.fromisoformat(row["range_start"]),
            range_end=date.fromisoformat(row["range_end"]),
            bar_count=int(row["bar_count"]),
            status=BackfillRunStatus(row["status"]),
        )

    def actual_coverage(
        self,
        adjustment: AdjustmentMethod,
        data_type: str,
    ) -> tuple[date | None, date | None]:
        """Earliest/latest stored day for one data type (dataset-wide).

        daily_bars is filtered by adjustment; the other types carry no
        adjustment column and are read dataset-wide.
        """
        query_by_type = {
            "daily_bars": (
                """SELECT MIN(trading_day) AS earliest, MAX(trading_day) AS latest
                   FROM daily_bars WHERE adjustment = ?""",
                (adjustment.value,),
            ),
            "fundamentals": (
                """SELECT MIN(published_on) AS earliest, MAX(published_on) AS latest
                   FROM fundamentals""",
                (),
            ),
            "stocks": (
                """SELECT MIN(as_of) AS earliest, MAX(as_of) AS latest
                   FROM stocks""",
                (),
            ),
            "dividends": (
                """SELECT MIN(ex_date) AS earliest, MAX(ex_date) AS latest
                   FROM dividends""",
                (),
            ),
        }
        if data_type not in query_by_type:
            raise ValueError(f"unsupported data type for coverage: {data_type}")
        sql, params = query_by_type[data_type]
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        earliest = (
            None if row["earliest"] is None else date.fromisoformat(row["earliest"])
        )
        latest = None if row["latest"] is None else date.fromisoformat(row["latest"])
        return earliest, latest

    def save_dataset_coverage(self, item: DatasetCoverage) -> None:
        """Upsert one data type coverage row."""
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO dataset_coverage
                   (dataset_id, adjustment, data_type, earliest_day, latest_day,
                    status, gap_days, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.dataset_id,
                    item.adjustment.value,
                    item.data_type,
                    None if item.earliest_day is None else item.earliest_day.isoformat(),
                    None if item.latest_day is None else item.latest_day.isoformat(),
                    item.status.value,
                    ",".join(day.isoformat() for day in item.gap_days),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def get_dataset_coverages(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> dict[str, DatasetCoverage]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM dataset_coverage
                   WHERE dataset_id = ? AND adjustment = ?""",
                (dataset_id, adjustment.value),
            ).fetchall()
        return {
            row["data_type"]: DatasetCoverage(
                dataset_id=row["dataset_id"],
                adjustment=AdjustmentMethod(row["adjustment"]),
                data_type=row["data_type"],
                earliest_day=(
                    None if row["earliest_day"] is None
                    else date.fromisoformat(row["earliest_day"])
                ),
                latest_day=(
                    None if row["latest_day"] is None
                    else date.fromisoformat(row["latest_day"])
                ),
                status=DataCoverageStatus(row["status"]),
                gap_days=tuple(
                    date.fromisoformat(day)
                    for day in (row["gap_days"].split(",") if row["gap_days"] else [])
                ),
            )
            for row in rows
        }

    def save_dataset_version(self, version: DatasetVersion) -> None:
        """Upsert one immutable dataset generation record."""
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO dataset_versions
                   (dataset_id, generation, source, adjustment, created_at,
                    status, coverage_start, coverage_end)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version.dataset_id,
                    version.generation,
                    version.source,
                    version.adjustment.value,
                    version.created_at.isoformat(),
                    version.status.value,
                    (
                        None if version.coverage_start is None
                        else version.coverage_start.isoformat()
                    ),
                    (
                        None if version.coverage_end is None
                        else version.coverage_end.isoformat()
                    ),
                ),
            )

    def get_latest_dataset_version(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> DatasetVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM dataset_versions
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (dataset_id, adjustment.value),
            ).fetchone()
        if row is None:
            return None
        return DatasetVersion(
            dataset_id=row["dataset_id"],
            generation=row["generation"],
            source=row["source"],
            adjustment=AdjustmentMethod(row["adjustment"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            status=DatasetVersionStatus(row["status"]),
            coverage_start=(
                None if row["coverage_start"] is None
                else date.fromisoformat(row["coverage_start"])
            ),
            coverage_end=(
                None if row["coverage_end"] is None
                else date.fromisoformat(row["coverage_end"])
            ),
        )

