"""Local-only orchestration for one clicked stock's multi-window CAPM analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol, Sequence
from uuid import uuid4

from stock_manager.capm.math import CapmEstimate, CapmInputError, estimate_capm
from stock_manager.capm.returns import build_aligned_returns
from stock_manager.read.capm import CapmInputs
from stock_manager.domain import (
    AdjustmentMethod, CapmResultRecord, DepositRate, IndexDailyBar, IndexReturnVersion,
)


class CapmRepository(Protocol):
    def read_capm_inputs(self, stock_code: str, benchmark_id: str, rate_term: str,
                         start: date, end: date) -> CapmInputs: ...
    def get_daily_bars(self, codes: Sequence[str], start: date, end: date,
                       adjustment: AdjustmentMethod) -> Sequence[object]: ...
    def get_index_daily_bars(self, index_id: str, start: date, end: date) -> tuple[IndexDailyBar, ...]: ...
    def get_deposit_rates(self, term: str, start: date, end: date) -> tuple[DepositRate, ...]: ...
    def get_index_return_version(self, index_id: str) -> IndexReturnVersion: ...
    def save_capm_results(self, records: Sequence[CapmResultRecord]) -> None: ...


@dataclass(frozen=True, slots=True)
class CapmWindowResult:
    window_days: int
    status: str
    reason: str | None
    estimate: CapmEstimate | None


class CapmAnalysisService:
    """Build CAPM inputs only from published local data; it has no Provider."""

    def __init__(self, repository: CapmRepository) -> None:
        self._repository = repository

    def analyse(
        self,
        stock_code: str,
        as_of: date,
        *,
        benchmark_id: str = "hs300.price",
        rate_term: str = "1_year",
        windows: tuple[int, ...] = (30, 120, 250, 500),
        periods_per_year: int = 252,
    ) -> tuple[CapmWindowResult, ...]:
        if not windows or any(window <= 0 for window in windows):
            raise ValueError("windows must contain positive natural-day lengths")
        # Eligibility is independent of an estimation window: retain enough
        # local history to prove the confirmed 180-natural-day threshold.
        start = as_of - timedelta(days=max(180, max(windows)) + 1)
        try:
            inputs = self._repository.read_capm_inputs(stock_code, benchmark_id, rate_term, start, as_of)
        except ValueError as error:
            return tuple(CapmWindowResult(window, "DATA_INCOMPLETE", str(error), None) for window in windows)
        stock_levels = inputs.stock_levels
        if not stock_levels or (as_of - stock_levels[0][0]).days < 180:
            return tuple(CapmWindowResult(window, "INELIGIBLE", "stock has less than 180 natural days of qfq history", None)
                         for window in windows)
        try:
            observations = build_aligned_returns(
                stock_levels,
                inputs.market_levels,
                inputs.rates,
                term=rate_term,
            )
        except ValueError as error:
            return tuple(CapmWindowResult(window, "DATA_INCOMPLETE", str(error), None) for window in windows)
        results: list[CapmWindowResult] = []
        for window in windows:
            window_start = as_of - timedelta(days=window)
            sample = tuple(item for item in observations if item.end > window_start)
            try:
                estimate = estimate_capm(
                    tuple(item.market_excess_return for item in sample),
                    tuple(item.stock_excess_return for item in sample),
                    periods_per_year=periods_per_year,
                    minimum_observations=15,
                )
            except CapmInputError as error:
                results.append(CapmWindowResult(window, "NOT_ESTIMABLE", str(error), None))
            else:
                results.append(CapmWindowResult(window, "READY", None, estimate))
        return tuple(results)

    def analyse_and_save(
        self, stock_code: str, as_of: date, **kwargs: object
    ) -> tuple[str, tuple[CapmWindowResult, ...]]:
        """Persist one explicitly requested analysis; no retry or network fallback."""
        results = self.analyse(stock_code, as_of, **kwargs)
        analysis_id = str(uuid4())
        benchmark_id = str(kwargs.get("benchmark_id", "hs300.price"))
        rate_term = str(kwargs.get("rate_term", "1_year"))
        periods_per_year = int(kwargs.get("periods_per_year", 252))
        version = self._repository.get_index_return_version(benchmark_id)
        now = datetime.now(timezone.utc)
        self._repository.save_capm_results(tuple(
            CapmResultRecord(
                analysis_id, stock_code, as_of, item.window_days, benchmark_id, version,
                rate_term,
                None if item.estimate is None else item.estimate.alpha_daily,
                None if item.estimate is None else item.estimate.alpha_annualized,
                None if item.estimate is None else item.estimate.beta,
                None if item.estimate is None else item.estimate.r_squared,
                0 if item.estimate is None else item.estimate.observation_count,
                periods_per_year, item.status, item.reason, now,
            ) for item in results
        ))
        return analysis_id, results
