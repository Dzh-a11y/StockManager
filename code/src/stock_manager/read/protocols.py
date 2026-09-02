"""Database-agnostic reader protocols (P4-1).

These protocols deliberately expose no sqlite3.Connection, no SQL text and no
SQLite PRAGMA. A SQLite reader implements them today; a future DuckDB or
PostgreSQL reader implements the same contracts unchanged.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from stock_manager.read.contracts import (
    DatasetReadSnapshot,
    MarketDataBatch,
    MarketDataReadRequest,
)


@runtime_checkable
class MarketDataReaderProtocol(Protocol):
    """A deterministic reader for ONE shard of data.

    A single reader owns its own connection and must only be used from the
    thread/process that created it. Reading is strictly read-only. The
    protocol surface is database-agnostic: it returns domain objects only and
    never leaks connections, SQL or pragmas.
    """

    def read_snapshot(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: str,
    ) -> DatasetReadSnapshot:
        """Return the frozen dataset snapshot for the given dataset/day."""
        ...

    def read_batch(self, request: MarketDataReadRequest) -> MarketDataBatch:
        """Read one shard (``request.codes``) deterministically.

        Bars are ordered ``code ASC, trading_day ASC``. Fundamentals (when
        ``include_fundamentals``) and dividends (when ``dividends_start`` is
        set) are read as part of the same shard.
        """
        ...

    def close(self) -> None:
        """Release the underlying connection. Must be idempotent."""
        ...

    def __enter__(self) -> "MarketDataReaderProtocol":
        """Context-manager support so readers are always closed."""
        ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Close the reader on context exit."""
        ...


@runtime_checkable
class MarketDataReaderFactoryProtocol(Protocol):
    """Creates an independent reader (and therefore an independent connection).

    Every concurrent task calls ``create()`` to obtain its own reader; readers
    are never shared across threads or processes.
    """

    def create(self) -> MarketDataReaderProtocol:
        ...
