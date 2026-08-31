"""A-share execution model (P5A-7): pure, testable decision logic.

The model answers can-buy/can-sell questions and computes fees under the
confirmed full constraint set: 100-share lots, T+1 sell restriction, no
trading on suspension days, no buy at limit-up / no sell at limit-down,
commission with minimum, stamp duty and transfer fee, slippage, cash
sufficiency and partial-fill policy. It never touches Backtrader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stock_manager.domain import DailyBar


@dataclass(frozen=True, slots=True)
class ExecutionParameters:
    """Effective execution parameters from the execution policy."""

    lot_size: int = 100
    commission_rate: Decimal = Decimal("0.0003")
    min_commission: Decimal = Decimal("5")
    stamp_duty_rate: Decimal = Decimal("0.0005")
    transfer_fee_rate: Decimal = Decimal("0.00001")
    slippage_rate: Decimal = Decimal("0")
    limit_up_ratio: Decimal = Decimal("0.10")
    limit_down_ratio: Decimal = Decimal("0.10")
    st_limit_ratio: Decimal = Decimal("0.05")
    allow_partial_fill: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    allowed: bool
    reason: str
    shares: int = 0
    estimated_cost: Decimal = Decimal("0")
    estimated_fees: Decimal = Decimal("0")


def limit_up_price(bar: DailyBar, parameters: ExecutionParameters, is_st: bool) -> Decimal:
    """Limit-up price = preclose x (1 + ratio), rounded to 0.01."""
    ratio = parameters.st_limit_ratio if is_st else parameters.limit_up_ratio
    return (bar.preclose * (Decimal("1") + ratio)).quantize(Decimal("0.01"))


def limit_down_price(bar: DailyBar, parameters: ExecutionParameters, is_st: bool) -> Decimal:
    ratio = parameters.st_limit_ratio if is_st else parameters.limit_down_ratio
    return (bar.preclose * (Decimal("1") - ratio)).quantize(Decimal("0.01"))


def is_limit_up(bar: DailyBar, parameters: ExecutionParameters, is_st: bool) -> bool:
    if bar.volume == 0 or not bar.is_trading:
        return False
    return bar.close >= limit_up_price(bar, parameters, is_st)


def is_limit_down(bar: DailyBar, parameters: ExecutionParameters, is_st: bool) -> bool:
    if bar.volume == 0 or not bar.is_trading:
        return False
    return bar.close <= limit_down_price(bar, parameters, is_st)


def is_suspended(bar: DailyBar) -> bool:
    """Suspension: no trading flag or zero volume bar."""
    return (not bar.is_trading) or bar.volume == 0


def lot_floor(shares: int, lot_size: int) -> int:
    """Round shares down to whole lots (buy side)."""
    return (shares // lot_size) * lot_size


def lot_ceil(shares: int, lot_size: int) -> int:
    """Round shares up to whole lots (sell side; odd lots allowed on full exit)."""
    if shares <= 0:
        return 0
    return ((shares + lot_size - 1) // lot_size) * lot_size


def compute_fees(
    value: Decimal,
    side: str,
    parameters: ExecutionParameters,
) -> Decimal:
    """Commission (with minimum) + stamp duty (sell) + transfer fee."""
    if value <= 0:
        return Decimal("0")
    commission = value * parameters.commission_rate
    if commission < parameters.min_commission:
        commission = parameters.min_commission
    stamp = value * parameters.stamp_duty_rate if side == "sell" else Decimal("0")
    transfer = value * parameters.transfer_fee_rate
    return (commission + stamp + transfer).quantize(Decimal("0.01"))


def decide_buy(
    *,
    code: str,
    bar: DailyBar | None,
    is_st: bool,
    parameters: ExecutionParameters,
    cash: Decimal,
    target_value: Decimal,
    today: date,
) -> ExecutionDecision:
    """Decide whether a buy can be placed at T+1 open, and at what size."""
    if bar is None or is_suspended(bar):
        return ExecutionDecision(False, f"suspended: no tradable bar for {code}")
    if is_limit_up(bar, parameters, is_st):
        return ExecutionDecision(False, f"limit-up: cannot buy {code}")
    adjusted_price = bar.open * (Decimal("1") + parameters.slippage_rate)
    if adjusted_price <= 0:
        return ExecutionDecision(False, f"invalid price for {code}")
    raw_shares = target_value / adjusted_price
    shares = lot_floor(int(raw_shares), parameters.lot_size)
    if shares <= 0:
        return ExecutionDecision(False, f"insufficient value for one lot of {code}")
    cost = adjusted_price * Decimal(shares)
    fees = compute_fees(cost, "buy", parameters)
    total = cost + fees
    if total > cash:
        if not parameters.allow_partial_fill:
            return ExecutionDecision(
                False,
                f"insufficient cash for {code}: need {total} but cash is {cash}",
            )
        # 部分成交:按现金可承担的整手股数
        affordable_shares = lot_floor(
            int(cash / (adjusted_price * (Decimal("1") + parameters.commission_rate))),
            parameters.lot_size,
        )
        if affordable_shares <= 0:
            return ExecutionDecision(
                False, f"insufficient cash for even one lot of {code}"
            )
        shares = affordable_shares
        cost = adjusted_price * Decimal(shares)
        fees = compute_fees(cost, "buy", parameters)
        total = cost + fees
        if total > cash:
            return ExecutionDecision(False, f"insufficient cash for {code}")
    return ExecutionDecision(
        True, f"buy {shares} of {code}", shares, total, fees
    )


def decide_sell(
    *,
    code: str,
    bar: DailyBar | None,
    is_st: bool,
    parameters: ExecutionParameters,
    bought_day: date | None,
    today: date,
    full_exit: bool = True,
) -> ExecutionDecision:
    """Decide whether a sell can be placed (T+1, suspension, limit-down)."""
    if bar is None or is_suspended(bar):
        return ExecutionDecision(False, f"suspended: cannot sell {code}")
    if bought_day is not None and bought_day >= today:
        return ExecutionDecision(
            False, f"T+1: {code} bought on {bought_day} cannot be sold on {today}"
        )
    if is_limit_down(bar, parameters, is_st):
        return ExecutionDecision(False, f"limit-down: cannot sell {code}")
    size = int(bar.volume)  # placeholder; real size comes from the position
    return ExecutionDecision(True, f"sell {code}", size, Decimal("0"), Decimal("0"))
