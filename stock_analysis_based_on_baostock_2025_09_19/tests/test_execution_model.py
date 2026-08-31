"""P5A-7 A-share execution model tests: lots, T+1, limits, fees."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_manager.backtest.execution import (
    ExecutionParameters,
    decide_buy,
    decide_sell,
    is_limit_down,
    is_limit_up,
    is_suspended,
    limit_down_price,
    limit_up_price,
    lot_ceil,
    lot_floor,
    compute_fees,
)
from stock_manager.domain import DailyBar

P = ExecutionParameters()
TODAY = date(2020, 1, 6)


def _bar(
    *,
    open: str = "10",
    close: str = "10",
    preclose: str = "10",
    volume: str = "1000000",
    is_trading: bool = True,
) -> DailyBar:
    return DailyBar(
        "000001.SZ",
        TODAY,
        Decimal(open),
        Decimal(open),
        Decimal(open),
        Decimal(close),
        Decimal(preclose),
        Decimal(volume),
        Decimal("10000000"),
        is_trading,
    )


def test_lot_rounding() -> None:
    assert lot_floor(150, 100) == 100
    assert lot_floor(99, 100) == 0
    assert lot_ceil(150, 100) == 200
    assert lot_ceil(100, 100) == 100
    assert lot_ceil(0, 100) == 0


def test_buy_requires_whole_lots_and_sufficient_cash() -> None:
    decision = decide_buy(
        code="A",
        bar=_bar(),
        is_st=False,
        parameters=P,
        cash=Decimal("100000"),
        target_value=Decimal("1500"),  # 150 股 → 100 股整手
        today=TODAY,
    )
    assert decision.allowed
    assert decision.shares == 100
    # 资金不足拒单
    decision = decide_buy(
        code="A",
        bar=_bar(),
        is_st=False,
        parameters=P,
        cash=Decimal("500"),
        target_value=Decimal("10000"),
        today=TODAY,
    )
    assert not decision.allowed
    assert "insufficient cash" in decision.reason


def test_buy_rejected_at_limit_up() -> None:
    bar = _bar(close="11", preclose="10")  # +10% 涨停
    assert is_limit_up(bar, P, False)
    decision = decide_buy(
        code="A", bar=bar, is_st=False, parameters=P,
        cash=Decimal("1000000"), target_value=Decimal("10000"), today=TODAY,
    )
    assert not decision.allowed
    assert "limit-up" in decision.reason


def test_sell_rejected_at_limit_down_and_suspension() -> None:
    bar = _bar(close="9", preclose="10")  # -10% 跌停
    assert is_limit_down(bar, P, False)
    decision = decide_sell(
        code="A", bar=bar, is_st=False, parameters=P,
        bought_day=TODAY - __import__("datetime").timedelta(days=1), today=TODAY,
    )
    assert not decision.allowed
    assert "limit-down" in decision.reason
    suspended = _bar(volume="0")
    assert is_suspended(suspended)
    decision = decide_sell(
        code="A", bar=suspended, is_st=False, parameters=P,
        bought_day=TODAY - __import__("datetime").timedelta(days=1), today=TODAY,
    )
    assert not decision.allowed
    assert "suspended" in decision.reason


def test_t_plus_1_blocks_same_day_sell() -> None:
    decision = decide_sell(
        code="A", bar=_bar(), is_st=False, parameters=P,
        bought_day=TODAY, today=TODAY,
    )
    assert not decision.allowed
    assert "T+1" in decision.reason
    # 次日可卖
    decision = decide_sell(
        code="A", bar=_bar(), is_st=False, parameters=P,
        bought_day=TODAY - __import__("datetime").timedelta(days=1), today=TODAY,
    )
    assert decision.allowed


def test_st_uses_5_percent_limits() -> None:
    bar = _bar(close="10.5", preclose="10")
    assert is_limit_up(bar, P, is_st=True)  # +5% 涨停
    assert not is_limit_up(bar, P, is_st=False)  # 主板 10% 未到
    assert limit_up_price(bar, P, is_st=True) == Decimal("10.50")
    assert limit_down_price(bar, P, is_st=False) == Decimal("9.00")


def test_fees_include_minimum_commission_and_stamp_duty() -> None:
    # 小金额:佣金按最低 5 元
    fees_buy = compute_fees(Decimal("1000"), "buy", P)
    assert fees_buy >= Decimal("5")
    # 卖出加印花税
    fees_sell = compute_fees(Decimal("1000000"), "sell", P)
    fees_buy_large = compute_fees(Decimal("1000000"), "buy", P)
    assert fees_sell > fees_buy_large
    assert fees_sell == fees_buy_large + Decimal("500")  # 印花税 0.05%


def test_zero_value_fees() -> None:
    assert compute_fees(Decimal("0"), "buy", P) == Decimal("0")


def test_partial_fill_policy() -> None:
    partial = ExecutionParameters(allow_partial_fill=True)
    decision = decide_buy(
        code="A", bar=_bar(), is_st=False, parameters=partial,
        cash=Decimal("5000"), target_value=Decimal("10000"), today=TODAY,
    )
    assert decision.allowed
    assert decision.shares > 0
    assert decision.shares % 100 == 0
    assert decision.estimated_cost <= Decimal("5000")
    # 现金不足一手时仍拒单(partial 也不能破整手)
    decision = decide_buy(
        code="A", bar=_bar(), is_st=False, parameters=partial,
        cash=Decimal("800"), target_value=Decimal("10000"), today=TODAY,
    )
    assert not decision.allowed


def test_missing_bar_rejected() -> None:
    decision = decide_buy(
        code="A", bar=None, is_st=False, parameters=P,
        cash=Decimal("1000000"), target_value=Decimal("10000"), today=TODAY,
    )
    assert not decision.allowed
    assert "suspended" in decision.reason
