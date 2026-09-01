"""SQLite implementation of the local repository contract."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from stock_manager.domain import (
    ActiveGeneration,
    AdjustmentMethod,
    HistoricalRunStatus,
    HistoricalScreeningRun,
    BackfillChunkV2,
    BackfillRunStatus,
    BackfillRunV2,
    CandidateGeneration,
    CandidateGenerationStatus,
    CoverageVerification,
    DailyBar,
    DataCoverageStatus,
    DatasetCoverage,
    DatasetMetadata,
    DatasetVersion,
    DatasetVersionStatus,
    DividendRecord,
    FundamentalSnapshot,
    GenerationPartition,
    IngestBatch,
    PublishedGeneration,
    StockIdentity,
    SyncPlan,
    SyncPlanMode,
    SyncPlanStatus,
    SyncRecord,
    SyncSource,
    SyncStatus,
    SyncTask,
    SyncTaskStatus,
    VerificationStatus,
)
from stock_manager.storage.migrations import migrate_database


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
    data_types TEXT NOT NULL,
    progress_json TEXT
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
CREATE TABLE IF NOT EXISTS historical_screening_runs (
    run_id TEXT PRIMARY KEY,
    cache_key TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    generation TEXT,
    template_id TEXT NOT NULL,
    template_revision INTEGER NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    rule_implementation_version TEXT NOT NULL,
    universe_policy TEXT NOT NULL,
    evaluation_start TEXT NOT NULL,
    evaluation_end TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_completed INTEGER NOT NULL,
    progress_total INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS eligibility_days (
    run_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    selected_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, trading_day)
);
CREATE TABLE IF NOT EXISTS eligibility_members (
    run_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY (run_id, trading_day, code)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    score_start TEXT NOT NULL,
    score_end TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_orders (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    trading_day TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    shares TEXT NOT NULL,
    value TEXT NOT NULL,
    fee TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS backtest_equity (
    run_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    holdings_value TEXT NOT NULL,
    PRIMARY KEY (run_id, trading_day)
);
"""


class SQLiteRepository:
    """Persist normalized market data locally using short SQLite transactions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            # 迁移:旧库补 progress_json 列(幂等)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(backfill_runs_v2)").fetchall()
            }
            if "progress_json" not in columns:
                connection.execute(
                    "ALTER TABLE backfill_runs_v2 ADD COLUMN progress_json TEXT"
                )
            # P5-RD-1:版本化幂等迁移(batch_id 绑定、书签表、user_version)。
            migrate_database(connection)

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

    def daily_bar_code_counts(
        self,
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> dict[str, int]:
        """Return ``{code: distinct bar trading-day count}`` in the window.

        Used to compute stock-based backfill progress: a code counts as fully
        ingested only when it has bars on (almost) every trading day of the
        target window, instead of judging by chunk bookkeeping.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT code, COUNT(DISTINCT trading_day) AS n FROM daily_bars
                   WHERE adjustment = ? AND trading_day BETWEEN ? AND ?
                   GROUP BY code""",
                (adjustment.value, start.isoformat(), end.isoformat()),
            ).fetchall()
        return {str(row["code"]): int(row["n"]) for row in rows}

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

    def update_backfill_batch_progress(
        self,
        run_id: str,
        *,
        phase: str,
        completed: int,
        total: int,
        current_code: str,
    ) -> None:
        """Persist the in-batch progress of a running v2 backfill."""
        import json as _json

        payload = _json.dumps(
            {
                "phase": phase,
                "completed": completed,
                "total": total,
                "current_code": current_code,
            },
            ensure_ascii=False,
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE backfill_runs_v2 SET progress_json = ? WHERE run_id = ?",
                (payload, run_id),
            )

    def get_backfill_batch_progress(
        self, run_id: str
    ) -> dict[str, object] | None:
        import json as _json

        with self._connect() as connection:
            row = connection.execute(
                "SELECT progress_json FROM backfill_runs_v2 WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None or row["progress_json"] is None:
            return None
        try:
            return _json.loads(row["progress_json"])
        except ValueError:
            return None

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
    # ------------------------------------------------------------------
    # P5A-5 historical screening runs / eligibility storage
    # ------------------------------------------------------------------

    def save_historical_run(self, run: HistoricalScreeningRun) -> None:
        """Upsert one historical screening run row."""
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO historical_screening_runs
                   (run_id, cache_key, dataset_id, adjustment, generation,
                    template_id, template_revision, plan_fingerprint,
                    rule_implementation_version, universe_policy,
                    evaluation_start, evaluation_end, status,
                    progress_completed, progress_total, started_at,
                    finished_at, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    run.cache_key,
                    run.dataset_id,
                    run.adjustment.value,
                    run.generation,
                    run.template_id,
                    run.template_revision,
                    run.plan_fingerprint,
                    run.rule_implementation_version,
                    run.universe_policy,
                    run.evaluation_start.isoformat(),
                    run.evaluation_end.isoformat(),
                    run.status.value,
                    run.progress_completed,
                    run.progress_total,
                    run.started_at.isoformat(),
                    (
                        None if run.finished_at is None
                        else run.finished_at.isoformat()
                    ),
                    run.error_message,
                ),
            )

    def get_historical_run(self, run_id: str) -> HistoricalScreeningRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM historical_screening_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else self._historical_run_from_row(row)

    def find_successful_run_by_cache_key(
        self, cache_key: str
    ) -> HistoricalScreeningRun | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM historical_screening_runs
                   WHERE cache_key = ? AND status = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (cache_key, HistoricalRunStatus.SUCCEEDED.value),
            ).fetchone()
        return None if row is None else self._historical_run_from_row(row)

    def list_historical_runs(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[HistoricalScreeningRun, ...]:
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit positive")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM historical_screening_runs
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY started_at DESC LIMIT ? OFFSET ?""",
                (dataset_id, adjustment.value, limit, offset),
            ).fetchall()
        return tuple(self._historical_run_from_row(row) for row in rows)

    @staticmethod
    def _historical_run_from_row(row: sqlite3.Row) -> HistoricalScreeningRun:
        return HistoricalScreeningRun(
            run_id=row["run_id"],
            cache_key=row["cache_key"],
            dataset_id=row["dataset_id"],
            adjustment=AdjustmentMethod(row["adjustment"]),
            generation=row["generation"],
            template_id=row["template_id"],
            template_revision=int(row["template_revision"]),
            plan_fingerprint=row["plan_fingerprint"],
            rule_implementation_version=row["rule_implementation_version"],
            universe_policy=row["universe_policy"],
            evaluation_start=date.fromisoformat(row["evaluation_start"]),
            evaluation_end=date.fromisoformat(row["evaluation_end"]),
            status=HistoricalRunStatus(row["status"]),
            progress_completed=int(row["progress_completed"]),
            progress_total=int(row["progress_total"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=(
                None if row["finished_at"] is None
                else datetime.fromisoformat(row["finished_at"])
            ),
            error_message=row["error_message"],
        )

    def save_eligibility_day(
        self, run_id: str, trading_day: date, selected_count: int
    ) -> None:
        if selected_count < 0:
            raise ValueError("selected_count must be non-negative")
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO eligibility_days
                   (run_id, trading_day, selected_count) VALUES (?, ?, ?)""",
                (run_id, trading_day.isoformat(), selected_count),
            )

    def save_eligibility_members(
        self, run_id: str, trading_day: date, codes: Sequence[str]
    ) -> None:
        with self._connect() as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO eligibility_members
                   (run_id, trading_day, code) VALUES (?, ?, ?)""",
                ((run_id, trading_day.isoformat(), code) for code in codes),
            )

    def list_eligibility_days(
        self, run_id: str, *, offset: int = 0, limit: int = 100
    ) -> tuple[tuple[date, int], ...]:
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit positive")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT trading_day, selected_count FROM eligibility_days
                   WHERE run_id = ? ORDER BY trading_day LIMIT ? OFFSET ?""",
                (run_id, limit, offset),
            ).fetchall()
        return tuple(
            (date.fromisoformat(row["trading_day"]), int(row["selected_count"]))
            for row in rows
        )

    def list_eligible_codes(
        self, run_id: str, trading_day: date
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT code FROM eligibility_members
                   WHERE run_id = ? AND trading_day = ? ORDER BY code""",
                (run_id, trading_day.isoformat()),
            ).fetchall()
        return tuple(row["code"] for row in rows)

    def count_eligibility_days(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM eligibility_days WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["c"])

    def mark_interrupted_runs(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        *,
        finished_at: datetime,
    ) -> int:
        """Mark non-terminal runs INTERRUPTED after a process restart."""
        active = (
            HistoricalRunStatus.QUEUED.value,
            HistoricalRunStatus.VALIDATING.value,
            HistoricalRunStatus.BUILDING_SIGNALS.value,
            HistoricalRunStatus.CANCEL_REQUESTED.value,
        )
        placeholders = ",".join("?" for _ in active)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE historical_screening_runs
                    SET status = ?, finished_at = ?, error_message = ?
                    WHERE dataset_id = ? AND adjustment = ?
                    AND status IN ({placeholders})""",
                (
                    HistoricalRunStatus.INTERRUPTED.value,
                    finished_at.isoformat(),
                    "interrupted by process restart",
                    dataset_id,
                    adjustment.value,
                    *active,
                ),
            )
        return cursor.rowcount

    def prune_historical_runs(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        keep_newest: int,
    ) -> int:
        """Delete runs (and their eligibility rows) beyond the newest N."""
        if keep_newest < 0:
            raise ValueError("keep_newest must be non-negative")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT run_id FROM historical_screening_runs
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY started_at DESC LIMIT -1 OFFSET ?""",
                (dataset_id, adjustment.value, keep_newest),
            ).fetchall()
        old_run_ids = tuple(row["run_id"] for row in rows)
        if not old_run_ids:
            return 0
        placeholders = ",".join("?" for _ in old_run_ids)
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM eligibility_members WHERE run_id IN ({placeholders})",
                old_run_ids,
            )
            connection.execute(
                f"DELETE FROM eligibility_days WHERE run_id IN ({placeholders})",
                old_run_ids,
            )
            cursor = connection.execute(
                f"DELETE FROM historical_screening_runs WHERE run_id IN ({placeholders})",
                old_run_ids,
            )
        return cursor.rowcount
    # ------------------------------------------------------------------
    # P5A-8 backtest result storage
    # ------------------------------------------------------------------

    def save_backtest_result(
        self,
        *,
        run_id: str,
        spec_id: str,
        plan_fingerprint: str,
        adjustment: AdjustmentMethod,
        score_start: date,
        score_end: date,
        metrics_json: str,
        warnings_json: str,
        provenance_json: str,
        created_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO backtest_runs
                   (run_id, spec_id, plan_fingerprint, adjustment, score_start,
                    score_end, metrics_json, warnings_json, provenance_json,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    spec_id,
                    plan_fingerprint,
                    adjustment.value,
                    score_start.isoformat(),
                    score_end.isoformat(),
                    metrics_json,
                    warnings_json,
                    provenance_json,
                    created_at.isoformat(),
                ),
            )

    def get_backtest_result(self, run_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "spec_id": row["spec_id"],
            "plan_fingerprint": row["plan_fingerprint"],
            "adjustment": row["adjustment"],
            "score_start": row["score_start"],
            "score_end": row["score_end"],
            "metrics_json": row["metrics_json"],
            "warnings_json": row["warnings_json"],
            "provenance_json": row["provenance_json"],
            "created_at": row["created_at"],
        }

    def save_backtest_orders(
        self, run_id: str, orders: Sequence[object]
    ) -> None:
        """orders: BacktestTrade-like with .trading_day/.code/.side/.price/
        .shares/.value/.fee/.status/.reason."""
        with self._connect() as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO backtest_orders
                   (run_id, seq, trading_day, code, side, price, shares, value,
                    fee, status, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        run_id,
                        index,
                        order.trading_day.isoformat(),
                        order.code,
                        order.side,
                        str(order.price),
                        str(order.shares),
                        str(order.value),
                        str(order.fee),
                        order.status,
                        order.reason,
                    )
                    for index, order in enumerate(orders)
                ),
            )

    def list_backtest_orders(
        self, run_id: str, *, offset: int = 0, limit: int = 100
    ) -> tuple[dict[str, object], ...]:
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit positive")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM backtest_orders
                   WHERE run_id = ? ORDER BY seq LIMIT ? OFFSET ?""",
                (run_id, limit, offset),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def count_backtest_orders(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM backtest_orders WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["c"])

    def save_backtest_equity(
        self, run_id: str, points: Sequence[object]
    ) -> None:
        """points: EquityPoint-like with .trading_day/.equity/.cash/.holdings_value."""
        with self._connect() as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO backtest_equity
                   (run_id, trading_day, equity, cash, holdings_value)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (
                        run_id,
                        point.trading_day.isoformat(),
                        str(point.equity),
                        str(point.cash),
                        str(point.holdings_value),
                    )
                    for point in points
                ),
            )

    def list_backtest_equity(
        self, run_id: str, *, offset: int = 0, limit: int = 500
    ) -> tuple[dict[str, object], ...]:
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit positive")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM backtest_equity
                   WHERE run_id = ? ORDER BY trading_day LIMIT ? OFFSET ?""",
                (run_id, limit, offset),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def count_backtest_equity(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM backtest_equity WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["c"])


    # ------------------------------------------------------------------
    # P5-RD-1 DataSync reconstruction bookkeeping
    # ------------------------------------------------------------------

    def save_sync_plan(self, plan: SyncPlan) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO sync_plans
                   (plan_id, plan_version, mode, source, dataset_id, adjustment,
                    universe_policy, target_start, target_end,
                    latest_completed_trading_day, parent_generation,
                    candidate_generation_id, required_data_types, task_count,
                    plan_fingerprint, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.plan_version,
                    plan.mode.value,
                    plan.source.value,
                    plan.dataset_id,
                    plan.adjustment.value,
                    plan.universe_policy,
                    plan.target_start.isoformat(),
                    plan.target_end.isoformat(),
                    (
                        None if plan.latest_completed_trading_day is None
                        else plan.latest_completed_trading_day.isoformat()
                    ),
                    plan.parent_generation,
                    plan.candidate_generation_id,
                    ",".join(plan.required_data_types),
                    plan.task_count,
                    plan.plan_fingerprint,
                    plan.status.value,
                    plan.created_at.isoformat(),
                    plan.updated_at.isoformat(),
                ),
            )

    def get_sync_plan(self, plan_id: str) -> SyncPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return None if row is None else self._sync_plan_from_row(row)

    def list_sync_plans(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> Sequence[SyncPlan]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM sync_plans
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY created_at DESC""",
                (dataset_id, adjustment.value),
            ).fetchall()
        return tuple(self._sync_plan_from_row(row) for row in rows)

    def update_sync_plan_status(
        self, plan_id: str, status: object, updated_at: datetime
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sync_plans SET status = ?, updated_at = ? WHERE plan_id = ?",
                (status.value, updated_at.isoformat(), plan_id),
            )

    @staticmethod
    def _sync_plan_from_row(row: sqlite3.Row) -> SyncPlan:
        return SyncPlan(
            plan_id=row["plan_id"],
            plan_version=int(row["plan_version"]),
            mode=SyncPlanMode(row["mode"]),
            source=SyncSource(row["source"]),
            dataset_id=row["dataset_id"],
            adjustment=AdjustmentMethod(row["adjustment"]),
            universe_policy=row["universe_policy"],
            target_start=date.fromisoformat(row["target_start"]),
            target_end=date.fromisoformat(row["target_end"]),
            latest_completed_trading_day=(
                None if row["latest_completed_trading_day"] is None
                else date.fromisoformat(row["latest_completed_trading_day"])
            ),
            parent_generation=row["parent_generation"],
            candidate_generation_id=row["candidate_generation_id"],
            required_data_types=tuple(
                row["required_data_types"].split(",")
                if row["required_data_types"]
                else ()
            ),
            task_count=int(row["task_count"]),
            plan_fingerprint=row["plan_fingerprint"],
            status=SyncPlanStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_sync_task(self, task: SyncTask) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO sync_tasks
                   (task_id, plan_id, sequence_no, data_type, partition_key,
                    codes, range_start, range_end, dependencies, status,
                    attempt_count, not_before, row_count, error_code,
                    error_message, started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    task.plan_id,
                    task.sequence_no,
                    task.data_type,
                    task.partition_key,
                    ",".join(task.codes),
                    task.range_start.isoformat(),
                    task.range_end.isoformat(),
                    ",".join(task.dependencies),
                    task.status.value,
                    task.attempt_count,
                    (
                        None if task.not_before is None
                        else task.not_before.isoformat()
                    ),
                    task.row_count,
                    task.error_code,
                    task.error_message,
                    (
                        None if task.started_at is None
                        else task.started_at.isoformat()
                    ),
                    (
                        None if task.finished_at is None
                        else task.finished_at.isoformat()
                    ),
                ),
            )

    def get_sync_task(self, task_id: str) -> SyncTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else self._sync_task_from_row(row)

    def list_sync_tasks(self, plan_id: str) -> Sequence[SyncTask]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sync_tasks WHERE plan_id = ? ORDER BY sequence_no",
                (plan_id,),
            ).fetchall()
        return tuple(self._sync_task_from_row(row) for row in rows)

    def update_sync_task_status(self, task: SyncTask) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE sync_tasks SET status = ?, attempt_count = ?,
                   not_before = ?, row_count = ?, error_code = ?,
                   error_message = ?, started_at = ?, finished_at = ?
                   WHERE task_id = ?""",
                (
                    task.status.value,
                    task.attempt_count,
                    (
                        None if task.not_before is None
                        else task.not_before.isoformat()
                    ),
                    task.row_count,
                    task.error_code,
                    task.error_message,
                    (
                        None if task.started_at is None
                        else task.started_at.isoformat()
                    ),
                    (
                        None if task.finished_at is None
                        else task.finished_at.isoformat()
                    ),
                    task.task_id,
                ),
            )

    def tasks_by_status(
        self, plan_id: str, statuses: Sequence[object]
    ) -> Sequence[SyncTask]:
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM sync_tasks WHERE plan_id = ?
                    AND status IN ({placeholders}) ORDER BY sequence_no""",
                (plan_id, *(s.value for s in statuses)),
            ).fetchall()
        return tuple(self._sync_task_from_row(row) for row in rows)

    @staticmethod
    def _sync_task_from_row(row: sqlite3.Row) -> SyncTask:
        return SyncTask(
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            sequence_no=int(row["sequence_no"]),
            data_type=row["data_type"],
            partition_key=row["partition_key"],
            codes=tuple(row["codes"].split(",")) if row["codes"] else (),
            range_start=date.fromisoformat(row["range_start"]),
            range_end=date.fromisoformat(row["range_end"]),
            dependencies=(
                tuple(row["dependencies"].split(",")) if row["dependencies"] else ()
            ),
            status=SyncTaskStatus(row["status"]),
            attempt_count=int(row["attempt_count"]),
            not_before=(
                None if row["not_before"] is None
                else datetime.fromisoformat(row["not_before"])
            ),
            row_count=(
                None if row["row_count"] is None else int(row["row_count"])
            ),
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=(
                None if row["started_at"] is None
                else datetime.fromisoformat(row["started_at"])
            ),
            finished_at=(
                None if row["finished_at"] is None
                else datetime.fromisoformat(row["finished_at"])
            ),
        )

    def save_candidate_generation(self, candidate: CandidateGeneration) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO candidate_generations
                   (candidate_generation_id, plan_id, parent_generation,
                    write_revision, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.candidate_generation_id,
                    candidate.plan_id,
                    candidate.parent_generation,
                    candidate.write_revision,
                    candidate.status.value,
                    candidate.created_at.isoformat(),
                    candidate.updated_at.isoformat(),
                ),
            )

    def get_candidate_generation(
        self, candidate_generation_id: str
    ) -> CandidateGeneration | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidate_generations WHERE candidate_generation_id = ?",
                (candidate_generation_id,),
            ).fetchone()
        if row is None:
            return None
        return CandidateGeneration(
            candidate_generation_id=row["candidate_generation_id"],
            plan_id=row["plan_id"],
            parent_generation=row["parent_generation"],
            write_revision=int(row["write_revision"]),
            status=CandidateGenerationStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def update_candidate_status(
        self,
        candidate_generation_id: str,
        status: CandidateGenerationStatus,
        updated_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE candidate_generations SET status = ?, updated_at = ?
                   WHERE candidate_generation_id = ?""",
                (status.value, updated_at.isoformat(), candidate_generation_id),
            )

    def save_ingest_batch(self, batch: IngestBatch) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO ingest_batches
                   (batch_id, candidate_generation_id, data_type, partition_key,
                    codes, range_start, range_end, row_count, source,
                    batch_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch.batch_id,
                    batch.candidate_generation_id,
                    batch.data_type,
                    batch.partition_key,
                    ",".join(batch.codes),
                    batch.range_start.isoformat(),
                    batch.range_end.isoformat(),
                    batch.row_count,
                    batch.source,
                    batch.batch_sha256,
                    batch.created_at.isoformat(),
                ),
            )

    def get_ingest_batch(self, batch_id: str) -> IngestBatch | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ingest_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return None if row is None else self._ingest_batch_from_row(row)

    def list_ingest_batches(
        self, candidate_generation_id: str
    ) -> Sequence[IngestBatch]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM ingest_batches
                   WHERE candidate_generation_id = ?
                   ORDER BY data_type, partition_key, range_start""",
                (candidate_generation_id,),
            ).fetchall()
        return tuple(self._ingest_batch_from_row(row) for row in rows)

    @staticmethod
    def _ingest_batch_from_row(row: sqlite3.Row) -> IngestBatch:
        return IngestBatch(
            batch_id=row["batch_id"],
            candidate_generation_id=row["candidate_generation_id"],
            data_type=row["data_type"],
            partition_key=row["partition_key"],
            codes=tuple(row["codes"].split(",")) if row["codes"] else (),
            range_start=date.fromisoformat(row["range_start"]),
            range_end=date.fromisoformat(row["range_end"]),
            row_count=int(row["row_count"]),
            source=row["source"],
            batch_sha256=row["batch_sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def save_coverage_verification(
        self, verification: CoverageVerification
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO coverage_verifications
                   (candidate_generation_id, data_type, partition_key,
                    expected_count, actual_count, distinct_count,
                    duplicate_count, invalid_count, coverage_ratio,
                    missing_items, status, verified_revision,
                    manifest_sha256, verified_at, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verification.candidate_generation_id,
                    verification.data_type,
                    verification.partition_key,
                    verification.expected_count,
                    verification.actual_count,
                    verification.distinct_count,
                    verification.duplicate_count,
                    verification.invalid_count,
                    str(verification.coverage_ratio),
                    ",".join(verification.missing_items),
                    verification.status.value,
                    verification.verified_revision,
                    verification.manifest_sha256,
                    verification.verified_at.isoformat(),
                    verification.details_json,
                ),
            )

    def list_coverage_verifications(
        self, candidate_generation_id: str
    ) -> Sequence[CoverageVerification]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM coverage_verifications
                   WHERE candidate_generation_id = ?
                   ORDER BY data_type, partition_key""",
                (candidate_generation_id,),
            ).fetchall()
        return tuple(self._coverage_verification_from_row(row) for row in rows)

    @staticmethod
    def _coverage_verification_from_row(
        row: sqlite3.Row,
    ) -> CoverageVerification:
        from decimal import Decimal as _Decimal

        return CoverageVerification(
            candidate_generation_id=row["candidate_generation_id"],
            data_type=row["data_type"],
            partition_key=row["partition_key"],
            expected_count=int(row["expected_count"]),
            actual_count=int(row["actual_count"]),
            distinct_count=int(row["distinct_count"]),
            duplicate_count=int(row["duplicate_count"]),
            invalid_count=int(row["invalid_count"]),
            coverage_ratio=_Decimal(row["coverage_ratio"]),
            missing_items=(
                tuple(row["missing_items"].split(","))
                if row["missing_items"]
                else ()
            ),
            status=VerificationStatus(row["status"]),
            verified_revision=int(row["verified_revision"]),
            manifest_sha256=row["manifest_sha256"],
            verified_at=datetime.fromisoformat(row["verified_at"]),
            details_json=row["details_json"],
        )

    def save_generation_partition(self, partition: GenerationPartition) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO generation_partitions
                   (generation, data_type, partition_key, batch_id)
                   VALUES (?, ?, ?, ?)""",
                (
                    partition.generation,
                    partition.data_type,
                    partition.partition_key,
                    partition.batch_id,
                ),
            )

    def list_generation_partitions(
        self, generation: str
    ) -> Sequence[GenerationPartition]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM generation_partitions
                   WHERE generation = ? ORDER BY data_type, partition_key""",
                (generation,),
            ).fetchall()
        return tuple(
            GenerationPartition(
                generation=row["generation"],
                data_type=row["data_type"],
                partition_key=row["partition_key"],
                batch_id=row["batch_id"],
            )
            for row in rows
        )

    def save_published_generation(self, published: PublishedGeneration) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO dataset_versions
                   (dataset_id, generation, source, adjustment, created_at,
                    status, coverage_start, coverage_end, manifest_sha256,
                    parent_generation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    published.dataset_id,
                    published.generation,
                    "published",
                    published.adjustment.value,
                    published.published_at.isoformat(),
                    published.status.value,
                    None,
                    None,
                    published.manifest_sha256,
                    published.parent_generation,
                ),
            )

    def get_latest_published_generation(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> PublishedGeneration | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM dataset_versions
                   WHERE dataset_id = ? AND adjustment = ?
                     AND status IN (?, ?)
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (
                    dataset_id,
                    adjustment.value,
                    CandidateGenerationStatus.PUBLISHED.value,
                    CandidateGenerationStatus.SUPERSEDED.value,
                ),
            ).fetchone()
        if row is None:
            return None
        return PublishedGeneration(
            generation=row["generation"],
            dataset_id=row["dataset_id"],
            adjustment=AdjustmentMethod(row["adjustment"]),
            parent_generation=row["parent_generation"],
            manifest_sha256=row["manifest_sha256"] or "",
            published_at=datetime.fromisoformat(row["created_at"]),
            status=CandidateGenerationStatus(row["status"]),
        )

    def save_active_generation(self, active: ActiveGeneration) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO active_generations
                   (dataset_id, adjustment, generation, activated_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    active.dataset_id,
                    active.adjustment.value,
                    active.generation,
                    active.activated_at.isoformat(),
                ),
            )

    def get_active_generation(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> ActiveGeneration | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM active_generations
                   WHERE dataset_id = ? AND adjustment = ?""",
                (dataset_id, adjustment.value),
            ).fetchone()
        if row is None:
            return None
        return ActiveGeneration(
            dataset_id=row["dataset_id"],
            adjustment=AdjustmentMethod(row["adjustment"]),
            generation=row["generation"],
            activated_at=datetime.fromisoformat(row["activated_at"]),
        )
