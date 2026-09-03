"""Deterministic construction of aligned simple excess-return observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from stock_manager.domain import DepositRate


@dataclass(frozen=True, slots=True)
class ReturnObservation:
    start: date
    end: date
    stock_return: Decimal
    market_return: Decimal
    risk_free_return: Decimal

    @property
    def stock_excess_return(self) -> Decimal:
        return self.stock_return - self.risk_free_return

    @property
    def market_excess_return(self) -> Decimal:
        return self.market_return - self.risk_free_return


def build_aligned_returns(
    stock_closes: Sequence[tuple[date, Decimal]],
    market_closes: Sequence[tuple[date, Decimal]],
    rates: Sequence[DepositRate],
    *,
    term: str = "1_year",
) -> tuple[ReturnObservation, ...]:
    """Return same-endpoint simple returns; gaps are rejected, never filled."""
    stock = _levels_by_day(stock_closes, "stock")
    market = _levels_by_day(market_closes, "market")
    rate_schedule = _rates_by_effective_day(rates, term)
    if set(stock) != set(market):
        raise ValueError("stock and market dates differ; affected window is incomplete")
    shared = sorted(stock)
    observations: list[ReturnObservation] = []
    for previous, current in zip(shared, shared[1:], strict=False):
        risk_free = _interval_risk_free(previous, current, rate_schedule)
        observations.append(
            ReturnObservation(
                previous,
                current,
                stock[current] / stock[previous] - Decimal("1"),
                market[current] / market[previous] - Decimal("1"),
                risk_free,
            )
        )
    return tuple(observations)


def _levels_by_day(
    levels: Sequence[tuple[date, Decimal]], label: str) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    previous: date | None = None
    for day, value in levels:
        if previous is not None and day <= previous:
            raise ValueError(f"{label} levels must be strictly date-ordered")
        if day in result:
            raise ValueError(f"duplicate {label} date: {day.isoformat()}")
        if not value.is_finite() or value <= Decimal("0"):
            raise ValueError(f"{label} close must be finite and positive")
        result[day] = value
        previous = day
    return result


def _rates_by_effective_day(rates: Sequence[DepositRate], term: str) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for rate in rates:
        if rate.term == term:
            if rate.effective_on in result:
                raise ValueError("duplicate effective deposit-rate date")
            result[rate.effective_on] = rate.annual_rate
    if not result:
        raise ValueError(f"no deposit rates available for term {term!r}")
    return result


def _interval_risk_free(start: date, end: date, schedule: dict[date, Decimal]) -> Decimal:
    if end <= start:
        raise ValueError("return interval must advance in time")
    total = Decimal("0")
    cursor = start
    while cursor < end:
        eligible = [day for day in schedule if day <= cursor]
        if not eligible:
            raise ValueError("no effective deposit rate at interval start")
        next_changes = [day for day in schedule if cursor < day < end]
        segment_end = min(next_changes) if next_changes else end
        total += schedule[max(eligible)] * Decimal((segment_end - cursor).days) / Decimal(365)
        cursor = segment_end
    return total
