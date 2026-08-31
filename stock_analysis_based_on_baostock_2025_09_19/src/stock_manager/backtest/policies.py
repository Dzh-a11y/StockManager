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
