"""P5A strategy mechanism tests: pullback entry, take-profit partial, add-on-dip."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.backtest.backtrader_engine import BacktraderBacktestEngine
from stock_manager.backtest.contracts import BacktestMarketData
from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    StockIdentity,
)
from stock_manager.research import (
    EvaluationSchedule,
    PolicyKind,
    PolicySpec,
    ResearchStrategySpec,
)
from stock_manager.services.historical_screening_executor import (
    EligibilitySnapshot,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)
QFQ = AdjustmentMethod.QFQ

DAYS = tuple(
    d
    for d in (
        date(2020, 1, 2) + __import__("datetime").timedelta(days=i)
        for i in range(40)
    )
    if d.weekday() < 5
)[:24]


class _Timeline:
    def __init__(self, snapshots: tuple[EligibilitySnapshot, ...]) -> None:
        self.snapshots = snapshots


def _bar(code: str, day: date, close: str, high: str) -> DailyBar:
    v = Decimal(close)
    return DailyBar(
        code, day, v, Decimal(high), v - Decimal("0.1"), v, v,
        Decimal("1000000"), Decimal("10000000"), True,
    )


def _market(close_seq: list[str]) -> tuple[BacktestMarketData, date, date]:
    """单只股票 price path,返回 market data 与 span。"""
    stock = StockIdentity("000001.SZ", "Alpha", "SZSE", False, DAYS[0], None)
    bars = [
        _bar("000001.SZ", DAYS[i], close_seq[i], close_seq[i])
        for i in range(len(close_seq))
    ]
    return (
        BacktestMarketData(
            "market", QFQ, DAYS[: len(close_seq)],
            tuple(bars), (stock,),
        ),
        DAYS[0],
        DAYS[len(close_seq) - 1],
    )


def _spec_when(
    *,
    entry: str,
    exit: str,
    allocation: str,
    entry_params: dict[str, object] | None = None,
    exit_params: dict[str, object] | None = None,
    alloc_params: dict[str, object] | None = None,
) -> ResearchStrategySpec:
    return ResearchStrategySpec(
        strategy_spec_id="test-spec",
        screening_template_id="t",
        screening_template_revision=1,
        screening_plan_fingerprint="fp",
        adjustment=QFQ,
        evaluation_schedule=EvaluationSchedule.DAILY,
        entry_policy=PolicySpec(entry, 1, entry_params or {}),
        exit_policy=PolicySpec(exit, 1, exit_params or {}),
        rebalance_policy=PolicySpec("daily_v1", 1, {}),
        allocation_policy=PolicySpec(allocation, 1, alloc_params or {}),
        ranking_policy=PolicySpec("turnover_20d_desc_v1", 1, {}),
        execution_policy=PolicySpec("ashare_execution_v1", 1, {}),
        initial_cash=Decimal("1000000"),
        backtest_start=DAYS[0],
        backtest_end=DAYS[23],
    )


def _eligible_from(day: date) -> _Timeline:
    return _Timeline(
        tuple(
            EligibilitySnapshot(d, ("000001.SZ",) if d >= day else (), 1)
            for d in DAYS
        )
    )


def test_pullback_entry_buys_after_drawdown() -> None:
    """回调入场:价格先涨后回落幅度达标才买入,而非筛选即买。"""
    # 价格:先涨到 12,回落到 10.5(回落 12.5%>5%),之后回升
    closes = [str(10 + i * 0.2) for i in range(10)]  # 10 -> 11.8
    closes += ["11.5", "11.0", "10.5", "10.8", "11.2"]  # 回落至 10.5
    market, start, end = _market(closes)
    spec = _spec_when(
        entry="pullback_entry_v1", exit="eligibility_exit_v1", allocation="equal_weight_v1",
        entry_params={"lookback_trading_days": 5, "drawdown_ratio": "0.05"},
        alloc_params={"max_positions": 5},
    )
    engine = BacktraderBacktestEngine()
    result = engine.run(spec, _eligible_from(DAYS[0]), market)
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys, "回调后应买入"
    # 买入价应接近回落后的低价段(< 11.6),而非初始上涨段
    assert all(t.price < Decimal("11.6") for t in buys)


def test_take_profit_partial_sells_fraction() -> None:
    """止盈减仓:上涨达 +10% 后卖出约一半,不全卖。"""
    closes = ["10", "10", "10.2", "10.4", "10.6", "10.8", "11.0", "11.2", "11.4", "11.5"]  # 涨至 +15%
    market, start, end = _market(closes)
    spec = _spec_when(
        entry="eligibility_enter_v1", exit="take_profit_partial_v1", allocation="equal_weight_v1",
        exit_params={"take_profit_ratio": "0.10", "partial_ratio": "0.50"},
        alloc_params={"max_positions": 5},
    )
    engine = BacktraderBacktestEngine()
    result = engine.run(spec, _eligible_from(DAYS[0]), market)
    sells = [t for t in result.trades if t.side == "sell"]
    assert sells, "止盈应触发减仓"
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys, "应已买入"
    # 卖出股数 < 买入股数(部分减仓,不是全卖)
    assert sells[0].shares < buys[0].shares


def test_add_position_on_dip_after_buy() -> None:
    """补仓:买入后价格回调 -10% 且有现金则补仓(买入次数>1)。"""
    closes = ["12", "12", "11.9", "11.7", "11.5", "11.3", "11.0", "10.8", "10.6", "10.5"]  # 冲高后回落至 -12.5%
    market, start, end = _market(closes)
    spec = _spec_when(
        entry="eligibility_enter_v1", exit="eligibility_exit_v1", allocation="add_position_on_dip_v1",
        alloc_params={"max_positions": 5, "add_drawdown_ratio": "0.10", "add_fraction": "0.5", "max_additions": 2},
    )
    engine = BacktraderBacktestEngine()
    result = engine.run(spec, _eligible_from(DAYS[0]), market)
    buys = [t for t in result.trades if t.side == "buy"]
    assert len(buys) >= 2, "回调后应补仓(多次买入)"
