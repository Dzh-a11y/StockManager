from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata
from stock_manager.rules.price_range_ratio import evaluate_price_range_ratio


TARGET = date(2026, 8, 25)
METADATA = DatasetMetadata(
    "market",
    TARGET,
    "fixture",
    datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
    AdjustmentMethod.QFQ,
)


def bar(
    trading_day: date,
    high: str,
    low: str,
    *,
    code: str = "sh.600000",
    is_trading: bool = True,
) -> DailyBar:
    return DailyBar(
        code,
        trading_day,
        Decimal("10"),
        Decimal(high),
        Decimal(low),
        Decimal("10"),
        Decimal("10"),
        Decimal("1000"),
        Decimal("10000"),
        is_trading,
    )


def evaluate(
    items: tuple[DailyBar, ...],
    *,
    lookback: int = 3,
    minimum_ratio: str = "1.3",
    maximum_ratio: str = "1.4",
):
    return evaluate_price_range_ratio(
        items,
        METADATA,
        lookback,
        Decimal(minimum_ratio),
        Decimal(maximum_ratio),
        AdjustmentMethod.QFQ,
    )


def test_passes_when_ratio_is_within_inclusive_interval() -> None:
    items = (
        bar(TARGET - timedelta(days=2), "12", "10"),
        bar(TARGET - timedelta(days=1), "13", "10"),
        bar(TARGET, "12", "9.5"),
    )

    result = evaluate(items)

    assert result.passed is True
    assert result.actual_value["ratio"] == Decimal("13") / Decimal("9.5")
    assert result.actual_value["highest"] == Decimal("13")
    assert result.actual_value["lowest"] == Decimal("9.5")


def test_passes_on_exact_boundaries() -> None:
    items = (
        bar(TARGET - timedelta(days=2), "13", "10"),
        bar(TARGET - timedelta(days=1), "12", "10"),
        bar(TARGET, "11", "10"),
    )

    result = evaluate(items)

    assert result.passed is True
    assert result.actual_value["ratio"] == Decimal("1.3")

    items_high = (
        bar(TARGET - timedelta(days=2), "14", "10"),
        bar(TARGET - timedelta(days=1), "12", "10"),
        bar(TARGET, "11", "10"),
    )
    result_high = evaluate(items_high)

    assert result_high.passed is True
    assert result_high.actual_value["ratio"] == Decimal("1.4")


def test_fails_when_ratio_is_below_interval() -> None:
    items = (
        bar(TARGET - timedelta(days=2), "12", "10"),
        bar(TARGET - timedelta(days=1), "11", "10"),
        bar(TARGET, "10.5", "10"),
    )

    result = evaluate(items)

    assert result.passed is False
    assert result.actual_value["ratio"] < Decimal("1.3")


def test_fails_when_ratio_is_above_interval() -> None:
    items = (
        bar(TARGET - timedelta(days=2), "15", "10"),
        bar(TARGET - timedelta(days=1), "12", "10"),
        bar(TARGET, "11", "10"),
    )

    result = evaluate(items)

    assert result.passed is False
    assert result.actual_value["ratio"] > Decimal("1.4")


def test_insufficient_sessions_fails_explicitly() -> None:
    result = evaluate(
        (
            bar(TARGET - timedelta(days=1), "12", "10"),
            bar(TARGET, "13", "10"),
        ),
        lookback=3,
    )

    assert result.passed is False
    assert result.actual_value["session_count"] == 2
    assert "insufficient" in result.reason


def test_nontrading_rows_are_excluded() -> None:
    items = (
        bar(TARGET - timedelta(days=3), "15", "10", is_trading=False),
        bar(TARGET - timedelta(days=2), "12", "10"),
        bar(TARGET - timedelta(days=1), "13", "10"),
        bar(TARGET, "12", "10"),
    )

    result = evaluate(items, lookback=3)

    assert result.passed is True
    assert result.actual_value["session_count"] == 3


def test_rejects_nonpositive_or_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="minimum_ratio"):
        evaluate((bar(TARGET, "10", "10"),), minimum_ratio="0")
    with pytest.raises(ValueError, match="maximum_ratio"):
        evaluate((bar(TARGET, "10", "10"),), minimum_ratio="1.5", maximum_ratio="1.4")


def test_rejects_duplicate_days_and_mixed_codes() -> None:
    duplicate = (bar(TARGET, "10", "10"), bar(TARGET, "11", "10"))
    with pytest.raises(ValueError, match="unique trading days"):
        evaluate(duplicate, lookback=1)

    mixed = (
        bar(TARGET - timedelta(days=1), "10", "10"),
        bar(TARGET, "11", "10", code="sz.000001"),
    )
    with pytest.raises(ValueError, match="one stock code"):
        evaluate(mixed, lookback=1)


def test_rejects_adjustment_mismatch() -> None:
    with pytest.raises(ValueError, match="adjustment mismatch"):
        evaluate_price_range_ratio(
            (bar(TARGET, "10", "10"),),
            METADATA,
            1,
            Decimal("1.3"),
            Decimal("1.4"),
            AdjustmentMethod.HFQ,
        )
