"""Backtrader adapter: the only module that may import Backtrader (P5A-6/7).

Implements BacktestEngine over one controlled portfolio strategy with the
A-share execution model: orders are market orders placed at T close and fill
at the next bar open; buys are gated by lot size, suspension, limit-up, cash
sufficiency and fees; sells are gated by T+1, suspension and limit-down.
Every execution restriction produces an explicit warning.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import backtrader as bt

from stock_manager.backtest.contracts import (
    BacktestEngineError,
    BacktestInputError,
    BacktestMarketData,
    BacktestMetrics,
    BacktestResult,
    BacktestTrade,
    EquityPoint,
)
from stock_manager.backtest.execution import (
    ExecutionDecision,
    ExecutionParameters,
    decide_buy,
    decide_sell,
    is_suspended,
)
from stock_manager.backtest.policies import (
    RankingEntry,
    equal_weight_targets,
    exit_on_sma,
    rank_candidates,
    should_add_on_dip,
    should_pullback_entry,
    should_sma_above_exit,
    should_sma_below_entry,
    should_take_profit,
)
from stock_manager.domain import AdjustmentMethod, DailyBar, StockIdentity
from stock_manager.research.models import (
    PolicyOperator,
    PolicySpec,
    ResearchStrategySpec,
    TakeProfitTierSpec,
)


class _TradingCalendarFeed(bt.feeds.PandasData):
    """Dummy daily feed carrying the full trading calendar (data0)."""


class AShareCommission(bt.CommInfoBase):
    """Broker commission: min commission, stamp duty on sell, transfer fee."""

    params = (
        ("commission", 0.0003),
        ("min_commission", 5.0),
        ("stamp_duty", 0.0005),
        ("transfer", 0.00001),
    )

    def _getcommission(self, size: int, price: float, pseudoexec: bool) -> float:
        value = abs(size) * price
        comm = max(value * self.p.commission, self.p.min_commission)
        if size < 0:
            comm += value * self.p.stamp_duty
        comm += value * self.p.transfer
        return comm


class StockManagerPortfolioStrategy(bt.Strategy):
    """One controlled strategy reading eligibility and policy specs."""

    params = (("spec", None), ("eligibility", None), ("is_st_map", None))

    def __init__(self) -> None:
        self.spec: ResearchStrategySpec = self.p.spec
        self.eligibility: dict[date, tuple[str, ...]] = dict(self.p.eligibility)
        self.is_st: dict[str, bool] = dict(self.p.is_st_map or {})
        self.execution = self._execution_parameters()
        self.sma_by_code: dict[str, Any] = {}
        if self.spec.exit_policy.policy_id == "sma_timing_v1":
            sma_period = int(self.spec.exit_policy.parameters.get("sma_period", 20))
            for data in self.datas[1:]:
                self.sma_by_code[data._name] = bt.ind.SMA(
                    data.close, period=sma_period
                )
        self.held_since: dict[str, date] = {}
        self.prev_close: dict[str, Decimal] = {}
        self.cost_by_code: dict[str, Decimal] = {}
        self.add_count: dict[str, int] = {}
        self.trades_log: list[BacktestTrade] = []
        self.warnings_list: list[str] = []
        self.equity_points: list[EquityPoint] = []

    def _execution_parameters(self) -> ExecutionParameters:
        return ExecutionParameters(
            lot_size=_run_level_lot(self.spec),
            commission_rate=_run_level_fee(self.spec, "commission_rate", "0.0003"),
            min_commission=_run_level_fee(self.spec, "min_commission", "5"),
            stamp_duty_rate=_run_level_fee(self.spec, "stamp_duty_rate", "0.0005"),
            transfer_fee_rate=_run_level_fee(
                self.spec, "transfer_fee_rate", "0.00001"
            ),
            slippage_rate=Decimal(
                str(self.spec.execution_policy.parameters.get("slippage_rate", "0"))
            ),
        )

    def _date(self) -> date:
        return self.data0.datetime.date(0)

    def _bar_for(self, code: str) -> DailyBar | None:
        data = self._data_by_name(code)
        if data is None:
            return None
        volume = int(data.volume[0] or 0)
        return DailyBar(
            code=code,
            trading_day=self._date(),
            open=Decimal(str(data.open[0])),
            high=Decimal(str(data.high[0])),
            low=Decimal(str(data.low[0])),
            close=Decimal(str(data.close[0])),
            preclose=self.prev_close.get(code, Decimal(str(data.open[0]))),
            volume=Decimal(volume),
            amount=Decimal(str(data.volume[0] or 0)),
            is_trading=volume > 0,
        )

    def _cash(self) -> Decimal:
        return Decimal(str(self.broker.getcash()))

    def _net_value(self) -> Decimal:
        return Decimal(str(self.broker.getvalue()))

    def _holdings(self) -> dict[str, int]:
        holdings: dict[str, int] = {}
        for data in self.datas[1:]:
            size = int(self.getposition(data).size)
            if size > 0:
                holdings[data._name] = size
        return holdings

    def _turnover(self) -> dict[str, Decimal]:
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
        eligible = set(self.eligibility.get(today, ()))
        holdings = self._holdings()
        held_codes = tuple(holdings)
        # 旧版兼容:退出组仅含 take_profit_partial_v1 时,等价于把它转成止盈档
        # (全额退出组为空),避免 P5A 旧配置/测试语义消失。
        exit_policies = self.spec.exit_policies
        tiers = list(self.spec.take_profit_tiers)
        legacy_tp = _legacy_take_profit_policy(exit_policies)
        if legacy_tp is not None:
            exit_policies = ()
            tiers.append(
                TakeProfitTierSpec(
                    Decimal(
                        str(legacy_tp.parameters.get("take_profit_ratio", "0.10"))
                    ),
                    Decimal(str(legacy_tp.parameters.get("partial_ratio", "0.50"))),
                )
            )
            tiers.sort(key=lambda item: item.take_profit_ratio)
        # 1) 退出决定:退出组按操作符对持仓逐股求值,命中即整仓退出(P5C)
        full_exits: set[str] = set()
        for code in held_codes:
            hits = tuple(
                self._exit_hit(code, policy, eligible, today)
                for policy in exit_policies
            )
            if self._group_hits(hits, self.spec.exit_operator):
                full_exits.add(code)
        for code in sorted(full_exits):
            self._sell(code, today, "exit")
        # 1b) 止盈减仓:独立档位区,仅对当日未全额退出的持仓执行(全额退出优先)
        self._apply_take_profit(full_exits, today, tuple(tiers))
        # 2) 买入:候选按排名取 max_positions(入场组按操作符过滤)
        held_after = self._holdings()
        candidates = tuple(
            RankingEntry(code, self._turnover().get(code, Decimal("0")))
            for code in eligible
            if code not in held_after
        )
        candidates = tuple(
            c
            for c in candidates
            if self._group_hits(
                tuple(self._entry_hit(c.code, policy) for policy in self.spec.entry_policies),
                self.spec.entry_operator,
            )
        )
        max_positions = int(
            self.spec.allocation_policy.parameters.get("max_positions", 20)
        )
        cash_reserve = Decimal(
            str(self.spec.allocation_policy.parameters.get("cash_reserve_ratio", "0"))
        )
        selected = rank_candidates(candidates, max_positions)
        remaining = tuple(code for code in held_after)
        targets = tuple(dict.fromkeys(remaining + selected))
        net_value = self._net_value()
        allocations = equal_weight_targets(
            targets, net_value, max_positions, cash_reserve
        )
        for allocation in allocations:
            code = allocation.code
            if code in held_after:
                continue
            self._buy(code, today, allocation.target_value)
        # 2b) 补仓:买入后回调到阈值且有现金则加仓
        if self.spec.allocation_policy.policy_id == "add_position_on_dip_v1":
            alloc = self.spec.allocation_policy.parameters
            add_ratio = Decimal(str(alloc.get("add_drawdown_ratio", "0.10")))
            add_fraction = Decimal(str(alloc.get("add_fraction", "0.50")))
            max_add = int(alloc.get("max_additions", 2))
            for code in list(held_after):
                cost = self.cost_by_code.get(code)
                closes = self._closes(code)
                if not cost or not closes:
                    continue
                if should_add_on_dip(
                    cost, closes[-1], add_ratio,
                    self.add_count.get(code, 0), max_add,
                ):
                    self._add_position(code, today, add_fraction)
        # 3) 记录逐日净值与 prev_close
        for code in self._codes():
            data = self._data_by_name(code)
            if data is not None and len(data) > 0:
                self.prev_close[code] = Decimal(str(data.close[0]))
        cash = self._cash()
        net = self._net_value()
        self.equity_points.append(
            EquityPoint(today, net, cash, net - cash)
        )

    @staticmethod
    def _group_hits(hits: tuple[bool, ...], operator: PolicyOperator) -> bool:
        """Apply the group operator: ALL requires every hit, ANY at least one."""
        if operator is PolicyOperator.ALL:
            return bool(hits) and all(hits)
        return any(hits)

    def _exit_hit(
        self,
        code: str,
        policy: PolicySpec,
        eligible: set[str],
        today: date,
    ) -> bool:
        """Whether one full-exit policy fires for a held code today (P5C)."""
        policy_id = policy.policy_id
        if policy_id == "eligibility_exit_v1":
            return code not in eligible
        if policy_id == "sma_timing_v1":
            closes = self._closes(code)
            period = int(policy.parameters.get("sma_period", 20))
            return code in exit_on_sma((code,), {code: closes}, period)
        if policy_id == "fixed_holding_v1":
            entered = self.held_since.get(code)
            if entered is None:
                return False
            days = int(policy.parameters.get("holding_trading_days", 20))
            return (today - entered).days >= days
        if policy_id == "sma_above_v1":
            closes = self._closes(code)
            period = int(policy.parameters.get("sma_period", 20))
            return should_sma_above_exit(closes, period)
        raise BacktestInputError(
            f"unsupported exit policy in exit group: {policy_id}"
        )

    def _entry_hit(self, code: str, policy: PolicySpec) -> bool:
        """Whether one entry policy allows buying an eligible, unheld code."""
        policy_id = policy.policy_id
        if policy_id == "eligibility_enter_v1":
            return True
        if policy_id == "pullback_entry_v1":
            lookback = int(
                policy.parameters.get("lookback_trading_days", 20)
            )
            drawdown = Decimal(str(policy.parameters.get("drawdown_ratio", "0.05")))
            return self._pullback_ok(code, lookback, drawdown)
        if policy_id == "sma_below_v1":
            closes = self._closes(code)
            period = int(policy.parameters.get("sma_period", 20))
            return should_sma_below_entry(closes, period)
        raise BacktestInputError(
            f"unsupported entry policy in entry group: {policy_id}"
        )

    def _apply_take_profit(
        self,
        full_exit_codes: set[str],
        today: date,
        tiers: tuple[TakeProfitTierSpec, ...] | None = None,
    ) -> None:
        """Independent take-profit tiers on positions that are not exiting fully.

        Tiers are pre-sorted by ascending threshold; matched tiers fire in that
        order and each sells ``partial_ratio`` of the then-current holding.
        """
        tiers = self.spec.take_profit_tiers if tiers is None else tiers
        if not tiers:
            return
        candidates = sorted(set(self._holdings()) - set(full_exit_codes))
        for code in candidates:
            closes = self._closes(code)
            cost = self.cost_by_code.get(code)
            if not closes or cost is None:
                continue
            close = closes[-1]
            for tier in tiers:
                if should_take_profit(cost, close, tier.take_profit_ratio):
                    self._partial_sell(code, today, tier.partial_ratio)

    def _closes(self, code: str) -> tuple[Decimal, ...]:
        data = self._data_by_name(code)
        if data is None:
            return ()
        # get(size=N) 取当前时刻往前 N 根;size=0 会得到空切片,故用 idx+1 取全部历史。
        return tuple(Decimal(str(v)) for v in data.close.get(size=data.close.idx + 1))

    def _pullback_ok(self, code: str, lookback: int, drawdown_ratio: Decimal) -> bool:
        """回调入场:近 N 日高点回落 drawdown_ratio 才允许买入。"""
        data = self._data_by_name(code)
        if data is None:
            return False
        highs = tuple(Decimal(str(v)) for v in data.high.get(size=lookback))
        closes = tuple(Decimal(str(v)) for v in data.close.get(size=1))
        return should_pullback_entry(
            closes, highs, lookback=lookback, drawdown_ratio=drawdown_ratio
        )

    def _codes(self) -> tuple[str, ...]:
        return tuple(data._name for data in self.datas[1:])

    def _buy(self, code: str, today: date, target_value: Decimal) -> None:
        bar = self._bar_for(code)
        decision = decide_buy(
            code=code,
            bar=bar,
            is_st=self.is_st.get(code, False),
            parameters=self.execution,
            cash=self._cash(),
            target_value=target_value,
            today=today,
        )
        if not decision.allowed:
            self.warnings_list.append(f"execution: {decision.reason}")
            return
        data = self._data_by_name(code)
        if data is None:
            return
        order = self.buy(data=data, size=decision.shares)
        if order is None:
            self.warnings_list.append(f"execution: broker rejected buy for {code}")

    def _partial_sell(self, code: str, today: date, partial_ratio: Decimal) -> None:
        """止盈减仓:卖出当前持仓的一部分(整手向下取整,保留余股)。"""
        data = self._data_by_name(code)
        if data is None or self.getposition(data).size <= 0:
            return
        held = int(self.getposition(data).size)
        raw_sell = int(held * partial_ratio)
        lot = self.execution.lot_size
        sell_size = (raw_sell // lot) * lot if raw_sell >= lot else 0
        if sell_size <= 0 or sell_size >= held:
            return
        decision = decide_sell(
            code=code,
            bar=self._bar_for(code),
            is_st=self.is_st.get(code, False),
            parameters=self.execution,
            bought_day=self.held_since.get(code),
            today=today,
        )
        if not decision.allowed:
            self.warnings_list.append(f"execution: {decision.reason}")
            return
        order = self.sell(data=data, size=sell_size)
        if order is None:
            self.warnings_list.append(f"execution: partial sell rejected for {code}")

    def _add_position(self, code: str, today: date, add_fraction: Decimal) -> None:
        """补仓:按当前持仓市值的 add_fraction 加仓(现金约束由 decide_buy 处理)。"""
        data = self._data_by_name(code)
        if data is None or self.getposition(data).size <= 0:
            return
        pos_value = Decimal(str(data.close[0])) * Decimal(self.getposition(data).size)
        self._buy(code, today, pos_value * add_fraction)
        self.add_count[code] = self.add_count.get(code, 0) + 1

    def _sell(self, code: str, today: date, reason: str) -> None:
        data = self._data_by_name(code)
        if data is None or self.getposition(data).size <= 0:
            return
        bar = self._bar_for(code)
        decision = decide_sell(
            code=code,
            bar=bar,
            is_st=self.is_st.get(code, False),
            parameters=self.execution,
            bought_day=self.held_since.get(code),
            today=today,
        )
        if not decision.allowed:
            self.warnings_list.append(f"execution: {decision.reason}")
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
                    # 记录首次买入成本(供止盈/补仓判定;补仓不改基准成本)
                    if data_name not in self.cost_by_code:
                        self.cost_by_code[data_name] = price
            elif order.status == order.Rejected:
                self.trades_log.append(
                    BacktestTrade(
                        self._date(), data_name, "buy", price, Decimal(abs(size)),
                        Decimal("0"), Decimal("0"), "rejected", "rejected by broker",
                    )
                )
                self.warnings_list.append(f"execution: broker rejected order for {data_name}")

    def stop(self) -> None:
        for data in self.datas[1:]:
            if self.getposition(data).size > 0:
                self.close(data=data)


def _run_level_fee(spec: ResearchStrategySpec, key: str, default: str) -> Decimal:
    """Fee value: run-level spec field wins, legacy execution-policy param fallback."""
    value = getattr(spec, key, None)
    if value is not None:
        return Decimal(str(value))
    return Decimal(str(spec.execution_policy.parameters.get(key, default)))


def _run_level_lot(spec: ResearchStrategySpec) -> int:
    if spec.lot_size is not None:
        return int(spec.lot_size)
    return int(spec.execution_policy.parameters.get("lot_size", 100))


def _legacy_take_profit_policy(
    exit_policies: tuple[PolicySpec, ...],
) -> PolicySpec | None:
    """Return the legacy single take-profit exit policy, or None.

    P5A treated ``take_profit_partial_v1`` as an exit policy; P5C moved it to
    the independent take-profit tier area. A pure legacy configuration (exactly
    one exit policy of that id) keeps its previous behaviour.
    """
    if len(exit_policies) != 1:
        return None
    policy = exit_policies[0]
    if policy.policy_id == "take_profit_partial_v1":
        return policy
    return None


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
        is_st_map = {
            stock.code: stock.is_st for stock in market_data.stocks
        }
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.setcash(float(spec.initial_cash))
        cerebro.broker.addcommissioninfo(
            AShareCommission(
                commission=float(_run_level_fee(spec, "commission_rate", "0.0003")),
                min_commission=float(_run_level_fee(spec, "min_commission", "5")),
                stamp_duty=float(_run_level_fee(spec, "stamp_duty_rate", "0.0005")),
                transfer=float(
                    _run_level_fee(spec, "transfer_fee_rate", "0.00001")
                ),
            )
        )
        cerebro.addstrategy(
            StockManagerPortfolioStrategy,
            spec=spec,
            eligibility=eligibility_map,
            is_st_map=is_st_map,
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
        provenance = {
            "engine": "backtrader",
            "engine_version": bt.__version__,
            "strategy": "StockManagerPortfolioStrategy",
            "strategy_policies": ";".join(
                [
                    "entry="
                    + ",".join(
                        f"{p.policy_id}@{p.version}" for p in spec.entry_policies
                    ),
                    "exit="
                    + ",".join(
                        f"{p.policy_id}@{p.version}" for p in spec.exit_policies
                    ),
                    "take_profit="
                    + ";".join(
                        f"{tier.take_profit_ratio}x{tier.partial_ratio}"
                        for tier in spec.take_profit_tiers
                    ),
                    "rebalance=" + f"{spec.rebalance_policy.policy_id}@{spec.rebalance_policy.version}",
                    "allocation=" + f"{spec.allocation_policy.policy_id}@{spec.allocation_policy.version}",
                    "ranking=" + f"{spec.ranking_policy.policy_id}@{spec.ranking_policy.version}",
                    "execution=" + f"{spec.execution_policy.policy_id}@{spec.execution_policy.version}",
                ]
            ),
            "cheat_modes": "none",
            "execution_model": "ashare_v1",
        }
        return BacktestResult(
            spec_id=spec.strategy_spec_id,
            screening_plan_fingerprint=spec.screening_plan_fingerprint,
            adjustment=spec.adjustment,
            score_start=spec.backtest_start,
            score_end=spec.backtest_end,
            metrics=metrics,
            equity_curve=tuple(strategy.equity_points),
            trades=tuple(strategy.trades_log),
            warnings=tuple(strategy.warnings_list),
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
