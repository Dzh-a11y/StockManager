"""Point-in-time read contracts and SQLite reader (P5A-2).

A PIT read answers "what was knowable through day T" for a fixed dataset
window: the stock universe, bars, fundamentals and dividends each carry an
explicit as-of boundary so a T query can never see T+1 information.

Contracts contain no SQL and no sqlite3 references; only the SQLite reader
implementation touches the database.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
)


@dataclass(frozen=True, slots=True)
class PointInTimeRequest:
    """Immutable read intent for one historical evaluation window.

    codes empty means the whole market (used by universe queries); bars,
    fundamentals and dividends are always filtered to codes when provided.
    """

    dataset_id: str
    codes: tuple[str, ...]
    start: date
    end: date
    adjustment: AdjustmentMethod

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if not isinstance(self.adjustment, AdjustmentMethod):
            raise ValueError("adjustment must be provided explicitly")
        normalized = tuple(
            dict.fromkeys(code.strip() for code in self.codes if code.strip())
        )
        object.__setattr__(self, "codes", normalized)


@runtime_checkable
class PointInTimeReaderProtocol(Protocol):
    """Reads only information available through each as-of boundary."""

    def universe_as_of(self, day: date) -> tuple[StockIdentity, ...]:
        """Stocks listed on or before day and not yet delisted through day."""
        ...

    def bars_through(self, day: date) -> tuple[DailyBar, ...]:
        """Daily bars with trading_day <= day for the requested codes."""
        ...

    def fundamentals_through(self, day: date) -> tuple[FundamentalSnapshot, ...]:
        """Fundamentals published on or before day for the requested codes."""
        ...

    def dividends_through(self, day: date) -> tuple[DividendRecord, ...]:
        """Dividends with ex_date on or before day for the requested codes."""
        ...

    def trading_days(self, start: date, end: date) -> tuple[date, ...]:
        """Known A-share trading days inside [start, end]."""
        ...

    def committed_generation(self) -> str | None:
        """Latest COMPLETE dataset generation, or None when none is committed."""
        ...

    def data_fingerprint(self) -> str:
        """Stable digest of committed generation and coverage; changes when data changes."""
        ...

    def close(self) -> None:
        """Release the underlying connection. Must be idempotent."""
        ...

    def __enter__(self) -> "PointInTimeReaderProtocol": ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


class SQLitePointInTimeReader:
    """Read-only SQLite implementation of the point-in-time contract."""

    def __init__(
        self, database_path: Path, request: PointInTimeRequest
    ) -> None:
        self._request = request
        self._database_path = database_path
        uri = f"file:{database_path}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only = ON")
        self._closed = False

    @property
    def request(self) -> PointInTimeRequest:
        return self._request

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("reader is closed")

    def _filter_codes(self) -> tuple[str, ...] | None:
        """Return codes; None means the whole market."""
        return None if not self._request.codes else self._request.codes

    def universe_as_of(self, day: date) -> tuple[StockIdentity, ...]:
        self._assert_open()
        if day < self._request.start or day > self._request.end:
            raise ValueError(
                f"universe query day {day.isoformat()} outside the request window"
            )
        day_text = day.isoformat()
        with self._connection:
            row = self._connection.execute(
                """SELECT MAX(as_of) AS latest FROM stocks WHERE as_of <= ?""",
                (day_text,),
            ).fetchone()
            if row["latest"] is None:
                return ()
            rows = self._connection.execute(
                """SELECT * FROM stocks WHERE as_of = ?
                   AND (listed_on IS NULL OR listed_on <= ?)
                   AND (delisted_on IS NULL OR delisted_on > ?)
                   ORDER BY code""",
                (row["latest"], day_text, day_text),
            ).fetchall()
        return tuple(
            StockIdentity(
                code=r["code"],
                name=r["name"],
                exchange=r["exchange"],
                is_st=bool(r["is_st"]),
                listed_on=(
                    None if r["listed_on"] is None else date.fromisoformat(r["listed_on"])
                ),
                delisted_on=(
                    None if r["delisted_on"] is None else date.fromisoformat(r["delisted_on"])
                ),
            )
            for r in rows
        )

    def bars_through(self, day: date) -> tuple[DailyBar, ...]:
        self._assert_open()
        day_text = day.isoformat()
        codes = self._filter_codes()
        if codes is None:
            sql = """SELECT * FROM daily_bars
                     WHERE adjustment = ? AND trading_day <= ? AND trading_day >= ?
                     ORDER BY code, trading_day"""
            params: tuple[object, ...] = (
                self._request.adjustment.value,
                day_text,
                self._request.start.isoformat(),
            )
        else:
            placeholders = ",".join("?" for _ in codes)
            sql = f"""SELECT * FROM daily_bars
                      WHERE adjustment = ? AND trading_day <= ? AND trading_day >= ?
                      AND code IN ({placeholders})
                      ORDER BY code, trading_day"""
            params = (
                self._request.adjustment.value,
                day_text,
                self._request.start.isoformat(),
                *codes,
            )
        with self._connection:
            rows = self._connection.execute(sql, params).fetchall()
        return tuple(_bar_from_row(r) for r in rows)

    def fundamentals_through(self, day: date) -> tuple[FundamentalSnapshot, ...]:
        self._assert_open()
        codes = self._filter_codes()
        day_text = day.isoformat()
        if codes is None:
            sql = """SELECT * FROM fundamentals
                     WHERE published_on <= ? ORDER BY code, report_date"""
            params: tuple[object, ...] = (day_text,)
        else:
            placeholders = ",".join("?" for _ in codes)
            sql = f"""SELECT * FROM fundamentals
                      WHERE published_on <= ? AND code IN ({placeholders})
                      ORDER BY code, report_date"""
            params = (day_text, *codes)
        with self._connection:
            rows = self._connection.execute(sql, params).fetchall()
        return tuple(
            FundamentalSnapshot(
                code=r["code"],
                report_date=date.fromisoformat(r["report_date"]),
                published_on=date.fromisoformat(r["published_on"]),
                pe_ttm=None if r["pe_ttm"] is None else Decimal(r["pe_ttm"]),
                pb=None if r["pb"] is None else Decimal(r["pb"]),
                source=r["source"],
            )
            for r in rows
        )

    def dividends_through(self, day: date) -> tuple[DividendRecord, ...]:
        self._assert_open()
        codes = self._filter_codes()
        day_text = day.isoformat()
        if codes is None:
            sql = """SELECT * FROM dividends
                     WHERE ex_date <= ? ORDER BY code, ex_date"""
            params: tuple[object, ...] = (day_text,)
        else:
            placeholders = ",".join("?" for _ in codes)
            sql = f"""SELECT * FROM dividends
                      WHERE ex_date <= ? AND code IN ({placeholders})
                      ORDER BY code, ex_date"""
            params = (day_text, *codes)
        with self._connection:
            rows = self._connection.execute(sql, params).fetchall()
        return tuple(
            DividendRecord(
                code=r["code"],
                ex_date=date.fromisoformat(r["ex_date"]),
                cash_dividend_per_share=Decimal(r["cash_dividend_per_share"]),
                source=r["source"],
            )
            for r in rows
        )

    def trading_days(self, start: date, end: date) -> tuple[date, ...]:
        self._assert_open()
        with self._connection:
            rows = self._connection.execute(
                """SELECT trading_day FROM trading_days
                   WHERE trading_day BETWEEN ? AND ? ORDER BY trading_day""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return tuple(date.fromisoformat(r["trading_day"]) for r in rows)

    def committed_generation(self) -> str | None:
        self._assert_open()
        with self._connection:
            row = self._connection.execute(
                """SELECT generation FROM dataset_versions
                   WHERE dataset_id = ? AND adjustment = ? AND status = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (self._request.dataset_id, self._request.adjustment.value, "COMPLETE"),
            ).fetchone()
        return None if row is None else row["generation"]

    def data_fingerprint(self) -> str:
        """Digest of committed generation plus per-type coverage boundaries."""
        self._assert_open()
        digest = hashlib.sha256()
        generation = self.committed_generation()
        digest.update((generation or "no-generation").encode("utf-8"))
        with self._connection:
            rows = self._connection.execute(
                """SELECT data_type, earliest_day, latest_day, status
                   FROM dataset_coverage
                   WHERE dataset_id = ? AND adjustment = ?
                   ORDER BY data_type""",
                (self._request.dataset_id, self._request.adjustment.value),
            ).fetchall()
        for row in rows:
            digest.update(
                f"|{row['data_type']}:{row['earliest_day']}:{row['latest_day']}:{row['status']}".encode(
                    "utf-8"
                )
            )
        return digest.hexdigest()[:16]

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "SQLitePointInTimeReader":
        self._assert_open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _bar_from_row(row: sqlite3.Row) -> DailyBar:
    return DailyBar(
        code=row["code"],
        trading_day=date.fromisoformat(row["trading_day"]),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        preclose=Decimal(row["preclose"]),
        volume=Decimal(row["volume"]),
        amount=Decimal(row["amount"]),
        is_trading=bool(row["is_trading"]),
    )
