"""SQLite implementation of the read-only MarketDataReaderProtocol (P4-2).

Each reader owns exactly one connection opened in read-only URI mode with
``query_only`` enabled, so a read can never create tables, write rows or
modify the database. Readers are never shared across threads or processes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
)
from stock_manager.read.contracts import (
    DatasetReadSnapshot,
    MarketDataBatch,
    MarketDataReadRequest,
)
from stock_manager.read.errors import (
    AdjustmentMismatchError,
    DatasetUnavailableError,
    ShardReadError,
)


class SQLiteMarketDataReader:
    """Deterministic read-only access to one shard of the local SQLite store."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._connection = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteMarketDataReader":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def read_snapshot(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: str,
    ) -> DatasetReadSnapshot:
        row = self._connection.execute(
            """SELECT * FROM dataset_metadata
               WHERE dataset_id = ? AND trading_day = ? AND adjustment = ?""",
            (dataset_id, trading_day.isoformat(), adjustment),
        ).fetchone()
        if row is None:
            raise DatasetUnavailableError(
                f"local dataset {dataset_id!r} is unavailable for "
                f"{trading_day.isoformat()} with adjustment {adjustment}"
            )
        return DatasetReadSnapshot(
            row["dataset_id"],
            date.fromisoformat(row["trading_day"]),
            AdjustmentMethod(row["adjustment"]),
            row["source"],
            datetime.fromisoformat(row["synced_at"]),
        )

    def read_batch(self, request: MarketDataReadRequest) -> MarketDataBatch:
        """Read one shard: bars, optional fundamentals and optional dividends.

        Bars are ordered ``code ASC, trading_day ASC``; fundamentals carry the
        latest published snapshot per code; dividends are ordered
        ``code ASC, ex_date ASC``. Reads are split into bounded IN queries by
        ``batch_size`` so a request never builds an oversized parameter list.
        """
        try:
            snapshot = self.read_snapshot(
                request.dataset_id,
                request.end,
                request.adjustment.value,
            )
        except DatasetUnavailableError as error:
            # 数据集可能以其他复权方式存在：明确复权不一致，而不是普通缺失。
            if self._dataset_has_any_adjustment(request.dataset_id, request.end):
                raise AdjustmentMismatchError(
                    f"dataset {request.dataset_id} on {request.end.isoformat()} "
                    f"does not provide adjustment {request.adjustment.value}"
                ) from error
            raise
        bars = self._read_bars(request)
        if request.include_fundamentals:
            fundamentals = self._read_fundamentals(request)
        else:
            fundamentals = ()
        if request.dividends_start is not None:
            dividends = self._read_dividends(request)
        else:
            dividends = ()
        return MarketDataBatch(snapshot, request.codes, bars, fundamentals, dividends)

    def _dataset_has_any_adjustment(
        self, dataset_id: str, trading_day: date
    ) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM dataset_metadata
               WHERE dataset_id = ? AND trading_day = ? LIMIT 1""",
            (dataset_id, trading_day.isoformat()),
        ).fetchone()
        return row is not None

    def _read_bars(self, request: MarketDataReadRequest) -> tuple[DailyBar, ...]:
        chunks = self._chunks(request.codes, request.batch_size)
        if not chunks:
            return ()
        collected: list[DailyBar] = []
        try:
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                query = f"""SELECT * FROM daily_bars
                    WHERE code IN ({placeholders})
                      AND trading_day BETWEEN ? AND ?
                      AND adjustment = ?
                    ORDER BY code, trading_day"""
                parameters = (
                    *chunk,
                    request.start.isoformat(),
                    request.end.isoformat(),
                    request.adjustment.value,
                )
                rows = self._connection.execute(query, parameters).fetchall()
                collected.extend(
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
        except sqlite3.Error as error:
            raise ShardReadError(
                0,
                f"daily bar shard query failed for dataset {request.dataset_id}",
                error,
            ) from error
        return tuple(sorted(collected, key=lambda item: (item.code, item.trading_day)))

    def _read_fundamentals(
        self,
        request: MarketDataReadRequest,
    ) -> tuple[FundamentalSnapshot, ...]:
        chunks = self._chunks(request.codes, request.batch_size)
        if not chunks:
            return ()
        collected: list[FundamentalSnapshot] = []
        try:
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                query = f"""SELECT * FROM fundamentals
                    WHERE code IN ({placeholders})
                      AND published_on <= ?
                    ORDER BY code, published_on, report_date"""
                rows = self._connection.execute(
                    query,
                    (*chunk, request.end.isoformat()),
                ).fetchall()
                latest: dict[str, sqlite3.Row] = {}
                for row in rows:
                    latest[row["code"]] = row
                collected.extend(
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
        except sqlite3.Error as error:
            raise ShardReadError(
                0,
                f"fundamental shard query failed for dataset {request.dataset_id}",
                error,
            ) from error
        return tuple(sorted(collected, key=lambda item: item.code))

    def _read_dividends(
        self,
        request: MarketDataReadRequest,
    ) -> tuple[DividendRecord, ...]:
        assert request.dividends_start is not None
        chunks = self._chunks(request.codes, request.batch_size)
        if not chunks:
            return ()
        collected: list[DividendRecord] = []
        try:
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                query = f"""SELECT * FROM dividends
                    WHERE code IN ({placeholders})
                      AND ex_date BETWEEN ? AND ?
                    ORDER BY code, ex_date, source"""
                parameters = (
                    *chunk,
                    request.dividends_start.isoformat(),
                    request.end.isoformat(),
                )
                rows = self._connection.execute(query, parameters).fetchall()
                collected.extend(
                    DividendRecord(
                        row["code"],
                        date.fromisoformat(row["ex_date"]),
                        Decimal(row["cash_dividend_per_share"]),
                        row["source"],
                    )
                    for row in rows
                )
        except sqlite3.Error as error:
            raise ShardReadError(
                0,
                f"dividend shard query failed for dataset {request.dataset_id}",
                error,
            ) from error
        return tuple(sorted(collected, key=lambda item: (item.code, item.ex_date)))

    @staticmethod
    def _chunks(codes: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
        """Split codes into bounded chunks for safe IN queries."""
        size = max(1, size)
        return tuple(
            tuple(codes[index : index + size])
            for index in range(0, len(codes), size)
        )


class SQLiteMarketDataReaderFactory:
    """Creates an independent read-only reader per call."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def create(self) -> SQLiteMarketDataReader:
        return SQLiteMarketDataReader(self._database_path)