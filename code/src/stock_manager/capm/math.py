"""Side-effect-free one-factor CAPM estimation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Sequence


class CapmInputError(ValueError):
    """Raised when a CAPM sample has no statistically defined OLS result."""


@dataclass(frozen=True, slots=True)
class CapmEstimate:
    alpha_daily: Decimal
    alpha_annualized: Decimal
    beta: Decimal
    r_squared: Decimal
    observation_count: int
    periods_per_year: int


def estimate_capm(
    market_excess_returns: Sequence[Decimal],
    stock_excess_returns: Sequence[Decimal],
    *,
    periods_per_year: int = 252,
    minimum_observations: int = 15,
) -> CapmEstimate:
    """Fit ``stock_excess = alpha + beta * market_excess`` deterministically."""
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if minimum_observations < 2:
        raise ValueError("minimum_observations must be at least two")
    if len(market_excess_returns) != len(stock_excess_returns):
        raise CapmInputError("market and stock return counts differ")
    if len(market_excess_returns) < minimum_observations:
        raise CapmInputError("insufficient effective return observations")
    values = tuple(market_excess_returns) + tuple(stock_excess_returns)
    if any(not value.is_finite() for value in values):
        raise CapmInputError("returns must be finite")
    count = Decimal(len(market_excess_returns))
    with localcontext() as context:
        context.prec = 40
        mean_x = sum(market_excess_returns, Decimal("0")) / count
        mean_y = sum(stock_excess_returns, Decimal("0")) / count
        variance_x = sum(
            ((value - mean_x) ** 2 for value in market_excess_returns), Decimal("0")
        )
        if variance_x == Decimal("0"):
            raise CapmInputError("market excess return has zero variance")
        covariance = sum(
            (
                (x - mean_x) * (y - mean_y)
                for x, y in zip(market_excess_returns, stock_excess_returns, strict=True)
            ),
            Decimal("0"),
        )
        beta = covariance / variance_x
        alpha = mean_y - beta * mean_x
        residual_sum = sum(
            (
                (y - alpha - beta * x) ** 2
                for x, y in zip(market_excess_returns, stock_excess_returns, strict=True)
            ),
            Decimal("0"),
        )
        total_sum = sum(
            ((value - mean_y) ** 2 for value in stock_excess_returns), Decimal("0")
        )
        if total_sum == Decimal("0"):
            raise CapmInputError("stock excess return has zero variance")
        return CapmEstimate(
            alpha_daily=alpha,
            alpha_annualized=alpha * Decimal(periods_per_year),
            beta=beta,
            r_squared=Decimal("1") - residual_sum / total_sum,
            observation_count=len(market_excess_returns),
            periods_per_year=periods_per_year,
        )
