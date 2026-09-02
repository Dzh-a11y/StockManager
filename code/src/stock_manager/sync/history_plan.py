"""P5A-1 eight-year coverage planning: prefix/tail gap analysis.

Pure computation over the local trading calendar and the repository's actual
per-data-type coverage; it never calls a Provider.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from stock_manager.domain import AdjustmentMethod, DataCoverageStatus

TRADING_DAYS_PER_YEAR = 260
COVERED_DATA_TYPES: tuple[str, ...] = ("daily_bars", "fundamentals", "stocks", "dividends")

CoverageProbe = Callable[[AdjustmentMethod, str], tuple[date | None, date | None]]


def trading_day_lookback(
    trading_days: Sequence[date], end: date, count: int
) -> date:
    """Return the trading day count - 1 steps before end.

    count=1 returns end itself. When the calendar does not reach far enough
    back, the earliest available trading day is returned (the real coverage
    start is recorded separately, never silently assumed).
    """
    if count <= 0:
        raise ValueError("count must be positive")
    ordered = sorted(set(trading_days))
    if not ordered:
        raise ValueError("trading calendar is empty")
    if end < ordered[0]:
        raise ValueError(f"end {end.isoformat()} precedes the trading calendar")
    if end > ordered[-1]:
        raise ValueError(f"end {end.isoformat()} is after the trading calendar")
    index = ordered.index(end)
    return ordered[max(0, index - (count - 1))]


@dataclass(frozen=True, slots=True)
class DataTypePlan:
    """Planned vs actual coverage for one data type over the target window."""

    data_type: str
    actual_earliest: date | None
    actual_latest: date | None
    status: DataCoverageStatus
    prefix_gap: tuple[date, date] | None
    tail_gap: tuple[date, date] | None


@dataclass(frozen=True, slots=True)
class CoveragePlan:
    """Gap plan for the whole target window (decided at config v2)."""

    dataset_id: str
    adjustment: AdjustmentMethod
    target_start: date
    target_end: date
    per_type: tuple[DataTypePlan, ...]

    @property
    def has_prefix_gap(self) -> bool:
        return any(plan.prefix_gap is not None for plan in self.per_type)

    @property
    def has_tail_gap(self) -> bool:
        return any(plan.tail_gap is not None for plan in self.per_type)

    @property
    def needs_backfill(self) -> bool:
        return self.has_prefix_gap or self.has_tail_gap

    @property
    def overall_status(self) -> DataCoverageStatus:
        statuses = {plan.status for plan in self.per_type}
        if statuses == {DataCoverageStatus.COMPLETE}:
            return DataCoverageStatus.COMPLETE
        if statuses == {DataCoverageStatus.UNAVAILABLE}:
            return DataCoverageStatus.UNAVAILABLE
        return DataCoverageStatus.PARTIAL

    def by_type(self, data_type: str) -> DataTypePlan:
        for plan in self.per_type:
            if plan.data_type == data_type:
                return plan
        raise ValueError(f"no plan for data type {data_type}")


def _plan_one(
    data_type: str,
    actual_earliest: date | None,
    actual_latest: date | None,
    target_start: date,
    target_end: date,
) -> DataTypePlan:
    if actual_earliest is None or actual_latest is None:
        return DataTypePlan(
            data_type,
            None,
            None,
            DataCoverageStatus.UNAVAILABLE,
            (target_start, target_end),
            None,
        )
    prefix_gap: tuple[date, date] | None = None
    tail_gap: tuple[date, date] | None = None
    if actual_earliest > target_start:
        prefix_gap = (target_start, actual_earliest - timedelta(days=1))
    if actual_latest < target_end:
        tail_gap = (actual_latest + timedelta(days=1), target_end)
    status = DataCoverageStatus.COMPLETE
    if prefix_gap is not None or tail_gap is not None:
        status = DataCoverageStatus.PARTIAL
    return DataTypePlan(
        data_type,
        actual_earliest,
        actual_latest,
        status,
        prefix_gap,
        tail_gap,
    )


def plan_coverage(
    *,
    dataset_id: str,
    adjustment: AdjustmentMethod,
    target_start: date,
    target_end: date,
    probe: CoverageProbe,
    data_types: Sequence[str] = COVERED_DATA_TYPES,
) -> CoveragePlan:
    """Build the prefix/tail gap plan for every requested data type."""
    if target_start > target_end:
        raise ValueError("target_start must not be after target_end")
    if not data_types:
        raise ValueError("data_types must not be empty")
    plans = tuple(
        _plan_one(
            data_type,
            *(probe(adjustment, data_type)),
            target_start,
            target_end,
        )
        for data_type in data_types
    )
    return CoveragePlan(
        dataset_id, adjustment, target_start, target_end, plans
    )
