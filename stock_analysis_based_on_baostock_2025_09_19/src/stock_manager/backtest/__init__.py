"""Research backtest engine contracts and the Backtrader adapter (P5A-6)."""

from __future__ import annotations

from stock_manager.backtest.contracts import (
    BacktestEngine,
    BacktestEngineError,
    BacktestInputError,
    BacktestMarketData,
    BacktestMetrics,
    BacktestResult,
    BacktestTrade,
    EquityPoint,
)

__all__ = [
    "BacktestEngine",
    "BacktestEngineError",
    "BacktestInputError",
    "BacktestMarketData",
    "BacktestMetrics",
    "BacktestResult",
    "BacktestTrade",
    "EquityPoint",
]
