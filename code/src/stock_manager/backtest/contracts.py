"""Backtest engine contracts (P5A-6): pure domain, no Backtrader types.

The engine protocol, its input market data and its result objects live in the
domain/contract layer; only the adapter implementation may import Backtrader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from stock_manager.domain import AdjustmentMethod
from stock_manager.research.models import ResearchStrategySpec


class BacktestEngineError(RuntimeError):
    """The backtest engine failed; the cause chain carries the original error."""


class BacktestInputError(ValueError):
    """The backtest input (spec, eligibility, market data) is inconsistent."""


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One day of portfolio equity (cash + holdings at close)."""

    trading_day: date
    equity: Decimal
    cash: Decimal
    holdings_value: Decimal


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    """One fill or rejected order event in the normalized result."""

    trading_day: date
    code: str
    side: str  # buy | sell
    price: Decimal
    shares: Decimal
    value: Decimal
    fee: Decimal
    status: str  # filled | rejected
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Normalized performance metrics; unavailable values carry explicit reasons."""

    initial_cash: Decimal
    final_value: Decimal
    total_return: Decimal | None
    annualized_return: Decimal | None
    max_drawdown: Decimal | None
    max_drawdown_start: date | None
    max_drawdown_end: date | None
    volatility_annual: Decimal | None
    sharpe: Decimal | None
    trade_count: int
    win_count: int
    loss_count: int
    total_fees: Decimal
    unavailable: tuple[str, ...] = ()

    @property
    def has_metrics(self) -> bool:
        return not self.unavailable


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Normalized research backtest output (never a Backtrader object)."""

    spec_id: str
    screening_plan_fingerprint: str
    adjustment: AdjustmentMethod
    score_start: date
    score_end: date
    metrics: BacktestMetrics
    equity_curve: tuple[EquityPoint, ...]
    trades: tuple[BacktestTrade, ...]
    warnings: tuple[str, ...]
    provenance: dict[str, str]


@dataclass(frozen=True, slots=True)
class BacktestMarketData:
    """Domain-level market input for one backtest run.

    bars: (code, trading_day, OHLCV) covering warmup through score end,
    ordered by (code, trading_day).
    """

    dataset_id: str
    adjustment: AdjustmentMethod
    trading_days: tuple[date, ...]
    bars: tuple[object, ...]
    stocks: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if not self.trading_days:
            raise ValueError("trading_days must not be empty")


@runtime_checkable
class BacktestEngine(Protocol):
    """Sequential portfolio backtest over one eligibility timeline."""

    def run(
        self,
        spec: ResearchStrategySpec,
        eligibility: object,
        market_data: BacktestMarketData,
    ) -> BacktestResult: ...
