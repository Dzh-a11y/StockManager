"""Swappable read-only market data access layer (P4).

P4 introduces a database-agnostic read path (MarketDataReadService over
MarketDataReaderProtocol) so screening, CAPM, backtest and P6 research can
read local data without depending on SQLite or reimplementing concurrency.
"""

from __future__ import annotations

from stock_manager.read.contracts import (
    DatasetReadSnapshot,
    MarketDataBatch,
    MarketDataReadRequest,
)
from stock_manager.read.errors import (
    AdjustmentMismatchError,
    DatasetUnavailableError,
    ReadLayerError,
    ShardReadError,
    SnapshotConsistencyError,
)
from stock_manager.read.protocols import (
    MarketDataReaderFactoryProtocol,
    MarketDataReaderProtocol,
)
from stock_manager.read.service import (
    MarketDataReadResult,
    MarketDataReadService,
)
from stock_manager.read.sqlite_reader import (
    SQLiteMarketDataReader,
    SQLiteMarketDataReaderFactory,
)

__all__ = [
    "AdjustmentMismatchError",
    "DatasetReadSnapshot",
    "DatasetUnavailableError",
    "MarketDataBatch",
    "MarketDataReadRequest",
    "MarketDataReaderFactoryProtocol",
    "MarketDataReaderProtocol",
    "ReadLayerError",
    "ShardReadError",
    "SnapshotConsistencyError",
    "SQLiteMarketDataReader",
    "SQLiteMarketDataReaderFactory",
    "MarketDataReadResult",
    "MarketDataReadService",
]
