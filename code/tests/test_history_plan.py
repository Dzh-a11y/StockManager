"""P5A-1 history planning tests: lookback and prefix/tail gap analysis."""

from __future__ import annotations

from datetime import date

import pytest

from stock_manager.domain import AdjustmentMethod, DataCoverageStatus
from stock_manager.sync import plan_coverage, trading_day_lookback


DAYS = tuple(
    date(2018, 9, 3) + __import__("datetime").timedelta(days=i)
    for i in range(0, 2000)
    if (date(2018, 9, 3) + __import__("datetime").timedelta(days=i)).weekday() < 5
)


def _probe(earliest: date | None, latest: date | None):
    def probe(adjustment: AdjustmentMethod, data_type: str) -> tuple[date | None, date | None]:
        return earliest, latest

    return probe


def test_lookback_returns_nth_trading_day() -> None:
    end = DAYS[100]
    assert trading_day_lookback(DAYS, end, 1) == end
    assert trading_day_lookback(DAYS, end, 21) == DAYS[80]
    assert trading_day_lookback(DAYS, end, 101) == DAYS[0]


def test_lookback_clamps_to_earliest_available() -> None:
    assert trading_day_lookback(DAYS, DAYS[100], 5000) == DAYS[0]


def test_lookback_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="count must be positive"):
        trading_day_lookback(DAYS, DAYS[0], 0)
    with pytest.raises(ValueError, match="trading calendar is empty"):
        trading_day_lookback([], date(2020, 1, 1), 10)
    with pytest.raises(ValueError, match="precedes the trading calendar"):
        trading_day_lookback(DAYS, DAYS[0] - __import__("datetime").timedelta(days=1), 10)
    with pytest.raises(ValueError, match="after the trading calendar"):
        trading_day_lookback(DAYS, DAYS[-1] + __import__("datetime").timedelta(days=1), 10)


def test_plan_complete_when_coverage_covers_window() -> None:
    target_start, target_end = DAYS[0], DAYS[100]
    plan = plan_coverage(
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=target_start,
        target_end=target_end,
        probe=_probe(DAYS[0], DAYS[100]),
    )
    assert plan.overall_status is DataCoverageStatus.COMPLETE
    assert not plan.needs_backfill
    daily = plan.by_type("daily_bars")
    assert daily.prefix_gap is None
    assert daily.tail_gap is None


def test_plan_prefix_and_tail_gaps() -> None:
    target_start, target_end = DAYS[0], DAYS[100]
    plan = plan_coverage(
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=target_start,
        target_end=target_end,
        probe=_probe(DAYS[50], DAYS[80]),
    )
    assert plan.has_prefix_gap
    assert plan.has_tail_gap
    assert plan.overall_status is DataCoverageStatus.PARTIAL
    from datetime import timedelta

    daily = plan.by_type("daily_bars")
    # gap 端点是自然日(拉取区间语义,由 Provider 过滤交易日)
    assert daily.prefix_gap == (DAYS[0], DAYS[50] - timedelta(days=1))
    assert daily.tail_gap == (DAYS[80] + timedelta(days=1), DAYS[100])


def test_plan_unavailable_when_no_data() -> None:
    plan = plan_coverage(
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=DAYS[0],
        target_end=DAYS[100],
        probe=_probe(None, None),
    )
    assert plan.overall_status is DataCoverageStatus.UNAVAILABLE
    assert plan.by_type("daily_bars").prefix_gap == (DAYS[0], DAYS[100])


def test_plan_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="target_start must not be after target_end"):
        plan_coverage(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[10],
            target_end=DAYS[5],
            probe=_probe(None, None),
        )
    with pytest.raises(ValueError, match="data_types must not be empty"):
        plan_coverage(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[5],
            probe=_probe(None, None),
            data_types=(),
        )


def test_plan_unknown_data_type_probe_raises() -> None:
    plan = plan_coverage(
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        target_start=DAYS[0],
        target_end=DAYS[5],
        probe=_probe(None, None),
    )
    with pytest.raises(ValueError, match="no plan for data type"):
        plan.by_type("nope")
