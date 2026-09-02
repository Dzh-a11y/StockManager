"""Policy bridges for the portfolio strategy (P5A-6).

Pure decision functions used by StockManagerPortfolioStrategy. They do not
import Backtrader; the strategy (adapter layer) feeds them strategy state.
Full A-share execution semantics arrive in P5A-7.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stock_manager.research.models import PolicySpec


@dataclass(frozen=True, slots=True)
class RankingEntry:
    code: str
    turnover: Decimal  # 近 N 日均成交额


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    code: str
    target_value: Decimal  # 目标市值(现金口径)
    reason: str


def rank_candidates(
    candidates: tuple[RankingEntry, ...],
    max_positions: int,
) -> tuple[str, ...]:
    """Turnover desc, then code asc (decision 4)."""
    ordered = sorted(
        candidates, key=lambda item: (-item.turnover, item.code)
    )
    return tuple(item.code for item in ordered[:max_positions])


def equal_weight_targets(
    codes: tuple[str, ...],
    net_value: Decimal,
    max_positions: int,
    cash_reserve_ratio: Decimal,
) -> tuple[AllocationDecision, ...]:
    """Equal-weight target market values for the selected codes."""
    if not codes:
        return ()
    investable = net_value * (Decimal("1") - cash_reserve_ratio)
    per_position = investable / Decimal(max_positions)
    return tuple(
        AllocationDecision(code, per_position, "equal_weight") for code in codes
    )


def exit_on_eligibility(
    held_codes: tuple[str, ...],
    eligible_codes: tuple[str, ...],
    policy: PolicySpec,
) -> tuple[str, ...]:
    """eligibility_exit_v1: exit codes no longer eligible."""
    del policy
    eligible = set(eligible_codes)
    return tuple(code for code in held_codes if code not in eligible)


def exit_on_sma(
    held_codes: tuple[str, ...],
    closes: dict[str, tuple[Decimal, ...]],
    sma_period: int,
) -> tuple[str, ...]:
    """sma_timing_v1: exit when close < SMA(period) over available history."""
    exits: list[str] = []
    for code in held_codes:
        series = closes.get(code, ())
        if len(series) < sma_period:
            continue
        window = series[-sma_period:]
        sma = sum(window) / Decimal(sma_period)
        if series[-1] < sma:
            exits.append(code)
    return tuple(exits)


def exit_on_fixed_holding(
    held_since: dict[str, date],
    today: date,
    holding_trading_days: int,
) -> tuple[str, ...]:
    """fixed_holding_v1: exit after N trading days from entry."""
    return tuple(
        code
        for code, entered in held_since.items()
        if (today - entered).days >= holding_trading_days
    )


def should_pullback_entry(
    closes: tuple[Decimal, ...],
    highs: tuple[Decimal, ...],
    *,
    lookback: int,
    drawdown_ratio: Decimal,
) -> bool:
    """回调入场:收盘价低于近 N 日最高价回落 drawdown_ratio 才允许买入。"""
    if not closes or not highs:
        return False
    window = highs[-lookback:] if len(highs) >= lookback else highs
    window_high = max(window) if window else Decimal("0")
    if window_high <= 0:
        return False
    return closes[-1] <= window_high * (Decimal("1") - drawdown_ratio)


def should_take_profit(
    cost: Decimal, close: Decimal, take_profit_ratio: Decimal
) -> bool:
    """止盈:收盘价相对成本的涨幅达到 take_profit_ratio 即触发减仓。"""
    if cost <= 0:
        return False
    return close >= cost * (Decimal("1") + take_profit_ratio)


def should_add_on_dip(
    cost: Decimal,
    close: Decimal,
    add_drawdown_ratio: Decimal,
    add_count: int,
    max_additions: int,
) -> bool:
    """补仓:持仓成本回撤达到 add_drawdown_ratio 且未超过最大补仓次数。"""
    if cost <= 0 or add_count >= max_additions:
        return False
    return close <= cost * (Decimal("1") - add_drawdown_ratio)


def sma_value(series: tuple[Decimal, ...], period: int) -> Decimal | None:
    """简单移动平均:最近 period 个值的均值;周期不足或非法返回 None。"""
    if period <= 0 or len(series) < period:
        return None
    return sum(series[-period:]) / Decimal(period)


def should_sma_below_entry(closes: tuple[Decimal, ...], sma_period: int) -> bool:
    """sma_below_v1: 收盘价低于 N 日均线才买入。"""
    sma = sma_value(closes, sma_period)
    if sma is None:
        return False
    return closes[-1] < sma


def should_sma_above_exit(closes: tuple[Decimal, ...], sma_period: int) -> bool:
    """sma_above_v1: 收盘价高于 N 日均线才卖出。"""
    sma = sma_value(closes, sma_period)
    if sma is None:
        return False
    return closes[-1] > sma
