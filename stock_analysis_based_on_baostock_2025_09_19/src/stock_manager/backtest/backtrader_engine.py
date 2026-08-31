"""Backtrader adapter: the only module that may import Backtrader (P5A-6).

Implements BacktestEngine over one controlled portfolio strategy. Eligibility
comes from the precomputed timeline; orders are market orders placed at T
close and therefore fill at the next bar open (no cheat modes). All
Backtrader objects are converted to domain BacktestResult before returning.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import backtrader as bt

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
from stock_manager.backtest.policies import (
    AllocationDecision,
    RankingEntry,
    equal_weight_targets,
    exit_on_eligibility,
    exit_on_fixed_holding,
    exit_on_sma,
    rank_candidates,
)
from stock_manager.domain import DailyBar
from stock_manager.research.models import PolicySpec, ResearchStrategySpec


class _TradingCalendarFeed(bt.feeds.PandasData):
    """Dummy daily feed carrying the full trading calendar (data0)."""


class _SmaExitState:
    """SMA state read from Backtrader indicators in the strategy."""

    def __init__(self, sma_period: int) -> None:
        self.sma_period = sma_period
        self.sma_by_code: dict[str, Any] = {}


class StockManagerPortfolioStrategy(bt.Strategy):
    """One controlled strategy reading eligibility and policy specs."""

    params = (("spec", None), ("eligibility", None), ("trading_days", None))

    def __init__(self) -> None:
        self.spec: ResearchStrategySpec = self.p.spec
        self.eligibility: dict[date, tuple[str, ...]] = dict(self.p.eligibility)
        self.sma_by_code: dict[str, Any] = {}
        if self.spec.exit_policy.policy_id == "sma_timing_v1":
            sma_period = int(self.spec.exit_policy.parameters.get("sma_period", 20))
            for data in self.datas[1:]:
                self.sma_by_code[data._name] = bt.ind.SMA(
                    data.close, period=sma_period
                )
        self.held_since: dict[str, date] = {}
        self.trades_log: list[BacktestTrade] = []
        self.warnings_list: list[str] = []
        self.equity_points: list[EquityPoint] = []

    def _date(self) -> date:
        return self.data0.datetime.date(0)

    def _cash(self) -> Decimal:
        return Decimal(str(self.broker.getcash()))

    def _net_value(self) -> Decimal:
        return Decimal(str(self.broker.getvalue()))

    def _holdings(self) -> dict[str, Decimal]:
        holdings: dict[str, Decimal] = {}
        for data in self.datas[1:]:
            position = self.getposition(data)
            size = int(position.size)
            if size > 0:
                holdings[data._name] = Decimal(size)
        return holdings

    def _close_prices(self) -> dict[str, tuple[Decimal, ...]]:
        closes: dict[str, list[Decimal]] = defaultdict(list)
        for data in self.datas[1:]:
            closes[data._name] = tuple(
                Decimal(str(close)) for close in data.close.get(size=0, ago=0)
            )
        return dict(closes)

    def _turnover(self) -> dict[str, Decimal]:
        """近 N 日平均成交额(amount)降序排名所需的输入。"""
        lookback = int(
            self.spec.ranking_policy.parameters.get("lookback_trading_days", 20)
        )
        turnover: dict[str, Decimal] = {}
        for data in self.datas[1:]:
            amounts = [Decimal(str(v)) for v in data.volume.get(size=lookback, ago=0)]
            amounts = [a for a in amounts if a > 0]
            turnover[data._name] = (
                sum(amounts) / Decimal(len(amounts)) if amounts else Decimal("0")
            )
        return turnover

    def next(self) -> None:
        today = self._date()
        eligible = self.eligibility.get(today, ())
        eligible_set = set(eligible)
        holdings = self._holdings()
        held_codes = tuple(holdings)
        exit_policy = self.spec.exit_policy
        # 退出决定
        exits: set[str] = set()
        if exit_policy.policy_id == "eligibility_exit_v1":
            exits.update(exit_on_eligibility(held_codes, eligible, exit_policy))
        elif exit_policy.policy_id == "sma_timing_v1":
            closes = {
                code: self._close_prices().get(code, ()) for code in held_codes
            }
            exits.update(
                exit_on_sma(
                    held_codes,
                    closes,
                    int(exit_policy.parameters.get("sma_period", 20)),
                )
            )
        elif exit_policy.policy_id == "fixed_holding_v1":
            exits.update(
                exit_on_fixed_holding(
                    self.held_since,
                    today,
                    int(exit_policy.parameters.get("holding_trading_days", 20)),
                )
            )
        for code in sorted(exits):
            self._sell(code, today, "exit")
        # 买入:候选 = eligible 中未持有,按排名取 max_positions
        held_after = self._holdings()
        candidates = tuple(
            RankingEntry(code, self._turnover().get(code, Decimal("0")))
            for code in eligible
            if code not in held_after
        )
        max_positions = int(
            self.spec.allocation_policy.parameters.get("max_positions", 20)
        )
        cash_reserve = Decimal(
            str(self.spec.allocation_policy.parameters.get("cash_reserve_ratio", "0"))
        )
        selected = rank_candidates(candidates, max_positions)
        # 目标组合 = 现持仓(仍在 eligible? 资格失效时由 exit 处理)+ 新选
        remaining = tuple(
            code for code in held_after if code in eligible_set or code in held_after
        )
        targets = tuple(dict.fromkeys(remaining + selected))
        net_value = self._net_value()
        allocations = equal_weight_targets(
            targets, net_value, max_positions, cash_reserve
        )
        for allocation in allocations:
            code = allocation.code
            data = self._data_by_name(code)
            if data is None:
                continue
            current = self.getposition(data).size
            if current > 0:
                continue
            close = Decimal(str(data.close[0]))
            if close <= 0:
                continue
            target_shares = int(allocation.target_value / close)
            if target_shares <= 0:
                continue
            order = self.buy(data=data, size=target_shares)
            if order is None:
                continue

    def _sell(self, code: str, today: date, reason: str) -> None:
        data = self._data_by_name(code)
        if data is None or self.getposition(data).size <= 0:
            return
        self.close(data=data)
        self.held_since.pop(code, None)

    def _data_by_name(self, name: str):
        for data in self.datas[1:]:
            if data._name == name:
                return data
        return None

    def notify_order(self, order: bt.Order) -> None:
        if order.status in (order.Completed, order.Rejected, order.Canceled, order.Margin):
            data_name = order.data._name
            size = int(order.executed.size or order.created.size or 0)
            price = Decimal(str(order.executed.price or 0))
            if order.status == order.Completed:
                side = "buy" if size > 0 else "sell"
                value = price * Decimal(abs(size))
                fee = Decimal(str(order.executed.comm or 0))
                self.trades_log.append(
                    BacktestTrade(
                        self._date(),
                        data_name,
                        side,
                        price,
                        Decimal(abs(size)),
                        value,
                        fee,
                        "filled",
                    )
                )
                if side == "buy":
                    self.held_since.setdefault(data_name, self._date())
            elif order.status == order.Rejected:
                self.trades_log.append(
                    BacktestTrade(
                        self._date(), data_name, "buy", price, Decimal(abs(size)),
                        Decimal("0"), Decimal("0"), "rejected", "rejected by broker",
                    )
                )
                self.warnings_list.append(f"{data_name} order rejected")

    def stop(self) -> None:
        """Close all remaining positions at the last bar."""
        for data in self.datas[1:]:
            if self.getposition(data).size > 0:
                self.close(data=data)


class BacktraderBacktestEngine:
    """Adapter implementing BacktestEngine over Backtrader."""

    def run(
        self,
        spec: ResearchStrategySpec,
        eligibility: Any,
        market_data: BacktestMarketData,
    ) -> BacktestResult:
        try:
            return self._run(spec, eligibility, market_data)
        except BacktestInputError:
            raise
        except BacktestEngineError:
            raise
        except Exception as error:
            raise BacktestEngineError(
                f"backtrader engine failure for {spec.strategy_spec_id}"
            ) from error

    def _run(
        self,
        spec: ResearchStrategySpec,
        eligibility: Any,
        market_data: BacktestMarketData,
    ) -> BacktestResult:
        if spec.adjustment is not market_data.adjustment:
            raise BacktestInputError(
                "spec adjustment does not match market data adjustment"
            )
        eligibility_map = _eligibility_map(eligibility)
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.setcash(float(spec.initial_cash))
        cerebro.addstrategy(
            StockManagerPortfolioStrategy,
            spec=spec,
            eligibility=eligibility_map,
            trading_days=market_data.trading_days,
        )
        import pandas as pd

        calendar_frame = pd.DataFrame(
            {"open": [0.0] * len(market_data.trading_days),
             "high": [0.0] * len(market_data.trading_days),
             "low": [0.0] * len(market_data.trading_days),
             "close": [0.0] * len(market_data.trading_days),
             "volume": [0] * len(market_data.trading_days),
             "openinterest": [0] * len(market_data.trading_days)},
            index=pd.DatetimeIndex(market_data.trading_days),
        )
        cerebro.adddata(
            _TradingCalendarFeed(dataname=calendar_frame, datetime=None),
            name="__calendar__",
        )
        bars_by_code: dict[str, list[DailyBar]] = defaultdict(list)
        for bar in market_data.bars:
            bars_by_code[bar.code].append(bar)
        eligible_codes = sorted(
            {code for day_codes in eligibility_map.values() for code in day_codes}
        )
        for code in eligible_codes:
            frame = _frame_for(bars_by_code.get(code, ()))
            if frame is None:
                continue
            data = bt.feeds.PandasData(dataname=frame, datetime=None)
            cerebro.adddata(data, name=code)
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                            timeframe=bt.TimeFrame.Days, riskfreerate=0.0)
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
        runs = cerebro.run()
        strategy = runs[0]
        metrics = _normalize_metrics(
            strategy, spec.initial_cash, strategy.trades_log
        )
        warnings_list = tuple(strategy.warnings_list)
        provenance = {
            "engine": "backtrader",
            "engine_version": bt.__version__,
            "strategy": "StockManagerPortfolioStrategy",
            "strategy_policies": ",".join(
                [
                    f"{spec.entry_policy.policy_id}@{spec.entry_policy.version}",
                    f"{spec.exit_policy.policy_id}@{spec.exit_policy.version}",
                    f"{spec.rebalance_policy.policy_id}@{spec.rebalance_policy.version}",
                    f"{spec.allocation_policy.policy_id}@{spec.allocation_policy.version}",
                    f"{spec.ranking_policy.policy_id}@{spec.ranking_policy.version}",
                    f"{spec.execution_policy.policy_id}@{spec.execution_policy.version}",
                ]
            ),
            "cheat_modes": "none",
        }
        return BacktestResult(
            spec_id=spec.strategy_spec_id,
            screening_plan_fingerprint=spec.screening_plan_fingerprint,
            adjustment=spec.adjustment,
            score_start=spec.backtest_start,
            score_end=spec.backtest_end,
            metrics=metrics,
            equity_curve=(),
            trades=tuple(strategy.trades_log),
            warnings=warnings_list,
            provenance=provenance,
        )


def _eligibility_map(eligibility: Any) -> dict[date, tuple[str, ...]]:
    result: dict[date, tuple[str, ...]] = {}
    for snapshot in eligibility.snapshots:
        result[snapshot.trading_day] = tuple(snapshot.eligible_codes)
    return result


def _frame_for(bars: list[DailyBar]):
    import pandas as pd

    if not bars:
        return None
    index = [bar.trading_day for bar in bars]
    return pd.DataFrame(
        {
            "open": [float(bar.open) for bar in bars],
            "high": [float(bar.high) for bar in bars],
            "low": [float(bar.low) for bar in bars],
            "close": [float(bar.close) for bar in bars],
            "volume": [int(bar.volume) for bar in bars],
            "openinterest": [0] * len(bars),
        },
        index=pd.DatetimeIndex(index),
    )


def _normalize_metrics(
    strategy: bt.Strategy,
    initial_cash: Decimal,
    trades: list[BacktestTrade],
) -> BacktestMetrics:
    returns = strategy.analyzers.returns.get_analysis()
    drawdown = strategy.analyzers.drawdown.get_analysis()
    sharpe = strategy.analyzers.sharpe.get_analysis()
    trade_analysis = strategy.analyzers.trades.get_analysis()
    total = trade_analysis.get("total", {})
    won = trade_analysis.get("won", {})
    lost = trade_analysis.get("lost", {})
    final_value = Decimal(str(strategy.broker.getvalue()))
    total_fees = sum((trade.fee for trade in trades), Decimal("0"))
    unavailable: list[str] = []
    total_return = _opt_decimal(returns.get("rtot"))
    annualized = _opt_decimal(returns.get("rnorm100"))
    if annualized is not None:
        annualized = annualized / Decimal("100")
    max_dd = _opt_decimal(drawdown.get("max", {}).get("drawdown"))
    if max_dd is not None:
        max_dd = max_dd / Decimal("100")
    sharpe_value = _opt_decimal(sharpe.get("sharperatio"))
    if sharpe_value is None:
        unavailable.append("sharpe")
    if max_dd is None:
        unavailable.append("max_drawdown")
    if total_return is None:
        unavailable.append("total_return")
    if annualized is None:
        unavailable.append("annualized_return")
    return BacktestMetrics(
        initial_cash=initial_cash,
        final_value=final_value,
        total_return=total_return,
        annualized_return=annualized,
        max_drawdown=max_dd,
        max_drawdown_start=_opt_date(drawdown.get("max", {}).get("len")),
        max_drawdown_end=None,
        volatility_annual=None,
        sharpe=sharpe_value,
        trade_count=int(total.get("closed", 0)),
        win_count=int(won.get("total", 0)),
        loss_count=int(lost.get("total", 0)),
        total_fees=total_fees,
        unavailable=tuple(unavailable),
    )


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None or value != value:  # None or NaN
        return None
    return Decimal(str(value))


def _opt_date(value: Any) -> date | None:
    return value if isinstance(value, date) else None
