"""P5C engine tests: multi-policy entry/exit groups, take-profit tiers and
run-level fee wiring."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from stock_manager.backtest.backtrader_engine import BacktraderBacktestEngine
from stock_manager.backtest.contracts import BacktestMarketData
from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    StockIdentity,
)
from stock_manager.research import (
    EvaluationSchedule,
    PolicyOperator,
    PolicySpec,
    ResearchStrategySpec,
    TakeProfitTierSpec,
)

QFQ = AdjustmentMethod.QFQ

DAYS = tuple(
    d
    for d in (date(2020, 1, 2) + timedelta(days=i) for i in range(40))
    if d.weekday() < 5
)[:24]


def _bar(code: str, day: date, close: str, high: str | None = None) -> DailyBar:
    value = Decimal(close)
    high_value = Decimal(high) if high is not None else value
    return DailyBar(
        code, day, value, high_value, value - Decimal("0.1"), value, value,
        Decimal("1000000"), Decimal("10000000"), True,
    )


def _market(close_seq: list[str]) -> BacktestMarketData:
    stock = StockIdentity("000001.SZ", "Alpha", "SZSE", False, DAYS[0], None)
    bars = [_bar("000001.SZ", DAYS[i], close_seq[i]) for i in range(len(close_seq))]
    return BacktestMarketData(
        "market", QFQ, DAYS[: len(close_seq)], tuple(bars), (stock,)
    )


class _Timeline:
    """Eligibility active from start_i to end_i (inclusive); end default = all."""

    def __init__(self, start_i: int, end_i: int | None = None) -> None:
        self._start_i = start_i
        self._end_i = len(DAYS) - 1 if end_i is None else end_i

    @property
    def snapshots(self):
        from stock_manager.services.historical_screening_executor import (
            EligibilitySnapshot,
        )

        return tuple(
            EligibilitySnapshot(
                d, ("000001.SZ",) if self._start_i <= i <= self._end_i else (), 1
            )
            for i, d in enumerate(DAYS)
        )


def _spec(
    *,
    entry: tuple[PolicySpec, ...],
    exit: tuple[PolicySpec, ...],
    entry_op: PolicyOperator = PolicyOperator.ANY,
    exit_op: PolicyOperator = PolicyOperator.ANY,
    tiers: tuple[TakeProfitTierSpec, ...] = (),
    alloc_params: dict[str, object] | None = None,
    fees: dict[str, object] | None = None,
) -> ResearchStrategySpec:
    base_fees = {"max_positions": 5}
    base_fees.update(alloc_params or {})
    kwargs: dict[str, object] = dict(
        strategy_spec_id="test-spec",
        screening_template_id="t",
        screening_template_revision=1,
        screening_plan_fingerprint="fp",
        adjustment=QFQ,
        evaluation_schedule=EvaluationSchedule.DAILY,
        entry_policy=entry[0],
        exit_policy=exit[0],
        rebalance_policy=PolicySpec("daily_v1", 1, {}),
        allocation_policy=PolicySpec("equal_weight_v1", 1, base_fees),
        ranking_policy=PolicySpec("turnover_20d_desc_v1", 1, {}),
        execution_policy=PolicySpec("ashare_execution_v1", 1, {}),
        initial_cash=Decimal("1000000"),
        backtest_start=DAYS[0],
        backtest_end=DAYS[23],
        entry_policies=entry,
        exit_policies=exit,
        entry_operator=entry_op,
        exit_operator=exit_op,
        take_profit_tiers=tiers,
    )
    kwargs.update(fees or {})
    return ResearchStrategySpec(**kwargs)  # type: ignore[arg-type]


def _buys(result) -> list:
    return [t for t in result.trades if t.side == "buy"]


def _sells(result) -> list:
    return [t for t in result.trades if t.side == "sell"]


def test_exit_group_any_triggers_full_exit() -> None:
    """ANY:未到期的固定持有 + 站上均线,均线命中即整仓退出。

    资格窗口仅覆盖买入日,退出后不再重新入场,保证只成交一笔。
    """
    closes = [str(10 + i * 0.2) for i in range(14)]
    spec = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(
            PolicySpec("fixed_holding_v1", 1, {"holding_trading_days": 100}),
            PolicySpec("sma_above_v1", 1, {"sma_period": 5}),
        ),
        exit_op=PolicyOperator.ANY,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0, 1), _market(closes))
    sells = _sells(result)
    assert sells, "ANY 下均线卖出应触发"
    assert len(sells) == 1, "退出后不应再入场,只应有一笔卖出"
    assert sells[0].shares == sum(t.shares for t in _buys(result))


def test_exit_group_all_requires_every_condition() -> None:
    """ALL:固定持有 100 日永不满足,即使站上均线也不退出。"""
    closes = [str(10 + i * 0.2) for i in range(14)]
    spec = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(
            PolicySpec("fixed_holding_v1", 1, {"holding_trading_days": 100}),
            PolicySpec("sma_above_v1", 1, {"sma_period": 5}),
        ),
        exit_op=PolicyOperator.ALL,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0), _market(closes))
    assert not _sells(result), "ALL 下未满足全部条件不应退出"


def test_exit_group_all_sells_when_all_conditions_met() -> None:
    """ALL:固定持有 1 日且站上均线都满足时整仓退出。"""
    closes = [str(10 + i * 0.2) for i in range(14)]
    spec = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(
            PolicySpec("fixed_holding_v1", 1, {"holding_trading_days": 1}),
            PolicySpec("sma_above_v1", 1, {"sma_period": 5}),
        ),
        exit_op=PolicyOperator.ALL,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0), _market(closes))
    assert _sells(result), "ALL 下条件都满足应退出"


def test_entry_group_any_buys_immediately_with_eligibility() -> None:
    """ANY:资格即买成员恒真,无需等回调即入场。"""
    closes = [str(10 + i * 0.2) for i in range(12)]
    spec = _spec(
        entry=(
            PolicySpec("eligibility_enter_v1", 1, {}),
            PolicySpec(
                "pullback_entry_v1",
                1,
                {"lookback_trading_days": 5, "drawdown_ratio": "0.05"},
            ),
        ),
        exit=(PolicySpec("eligibility_exit_v1", 1, {}),),
        entry_op=PolicyOperator.ANY,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0), _market(closes))
    buys = _buys(result)
    assert buys, "ANY 下资格即买应立即买入"
    assert buys[0].price < Decimal("11.6"), "应在初期低位买入"


def test_entry_group_all_waits_for_pullback() -> None:
    """ALL:资格即买 + 回调都满足(价格自高点回落)才买入。"""
    closes = [str(10 + i * 0.2) for i in range(10)]
    closes += ["11.5", "11.0", "10.5", "10.8", "11.2"]
    spec = _spec(
        entry=(
            PolicySpec("eligibility_enter_v1", 1, {}),
            PolicySpec(
                "pullback_entry_v1",
                1,
                {"lookback_trading_days": 5, "drawdown_ratio": "0.05"},
            ),
        ),
        exit=(PolicySpec("eligibility_exit_v1", 1, {}),),
        entry_op=PolicyOperator.ALL,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0), _market(closes))
    buys = _buys(result)
    assert buys, "回调满足后应买入"
    assert all(t.price < Decimal("11.6") for t in buys), "应在回落后的低价段买入"


def test_take_profit_tiers_sell_low_to_high() -> None:
    """止盈多档:低价档先触发、高价档后触发,持仓不被清空。"""
    closes = ["10", "10", "10.2", "10.4", "10.6", "10.8", "11.0", "11.2", "11.4", "11.5"]
    tiers = (
        TakeProfitTierSpec(Decimal("0.05"), Decimal("0.5")),
        TakeProfitTierSpec(Decimal("0.10"), Decimal("0.5")),
    )
    spec = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(PolicySpec("eligibility_exit_v1", 1, {}),),
        tiers=tiers,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0), _market(closes))
    sells = _sells(result)
    assert len(sells) >= 2, "两档止盈应产生至少两笔卖出"
    bought = sum(t.shares for t in _buys(result))
    assert 0 < sum(t.shares for t in sells) < bought, "止盈只减仓不应全清"


def test_full_exit_priority_skips_partial_on_same_day() -> None:
    """同日既有全额退出又有止盈命中:只整仓一笔卖出,不做分笔。"""
    closes = [str(10 + i * 0.2) for i in range(12)]
    tiers = (TakeProfitTierSpec(Decimal("0.02"), Decimal("0.5")),)
    spec = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(PolicySpec("sma_above_v1", 1, {"sma_period": 3}),),
        tiers=tiers,
    )
    result = BacktraderBacktestEngine().run(spec, _Timeline(0, 1), _market(closes))
    sells = _sells(result)
    assert len(sells) == 1, "全额退出优先时当日只应有一笔卖出"
    bought = sum(t.shares for t in _buys(result))
    assert sells and sells[0].shares == bought, "应整仓退出"


def test_run_level_fee_fields_drive_costs() -> None:
    """run 级佣金/最低佣金接线:高费率运行总费用更高。"""
    closes = [str(10)] * 12
    cheap = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(PolicySpec("eligibility_exit_v1", 1, {}),),
        fees={
            "commission_rate": Decimal("0.0001"),
            "min_commission": Decimal("0"),
        },
    )
    pricey = _spec(
        entry=(PolicySpec("eligibility_enter_v1", 1, {}),),
        exit=(PolicySpec("eligibility_exit_v1", 1, {}),),
        fees={
            "commission_rate": Decimal("0.001"),
            "min_commission": Decimal("0"),
        },
    )
    engine = BacktraderBacktestEngine()
    cheap_result = engine.run(cheap, _Timeline(0), _market(closes))
    pricey_result = engine.run(pricey, _Timeline(0), _market(closes))
    assert cheap_result.metrics.total_fees > 0
    assert pricey_result.metrics.total_fees > cheap_result.metrics.total_fees
