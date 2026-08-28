from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata
from stock_manager.rules.avg_close_above import evaluate_avg_close_above


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
    close: str,
    *,
    code: str = "sh.600000",
    is_trading: bool = True,
) -> DailyBar:
    return DailyBar(
        code,
        trading_day,
        Decimal("10"),
        Decimal("11"),
        Decimal("9"),
        Decimal(close),
        Decimal("10"),
        Decimal("1000"),
        Decimal("10000"),
        is_trading,
    )


def evaluate(
    items: tuple[DailyBar, ...],
    lookback: int = 5,
    threshold: str = "10",
):
    return evaluate_avg_close_above(
        items,
        METADATA,
        lookback,
        Decimal(threshold),
        AdjustmentMethod.QFQ,
    )


def test_passes_when_average_close_is_above_threshold() -> None:
    items = (
        bar(TARGET - timedelta(days=4), "9"),
        bar(TARGET - timedelta(days=3), "10"),
        bar(TARGET - timedelta(days=2), "11"),
        bar(TARGET - timedelta(days=1), "12"),
        bar(TARGET, "13"),
    )

    result = evaluate(items, threshold="10")

    assert result.passed is True
    assert result.actual_value["average_close"] == Decimal("11")
    assert result.actual_value["latest_close"] == Decimal("13")
    assert result.actual_value["session_count"] == 5


def test_fails_when_average_close_equals_threshold() -> None:
    items = tuple(
        bar(TARGET - timedelta(days=index), "10") for index in range(5)
    )

    result = evaluate(items, threshold="10")

    assert result.passed is False
    assert result.actual_value["average_close"] == Decimal("10")


def test_fails_when_average_close_is_below_threshold() -> None:
    items = (
        bar(TARGET - timedelta(days=4), "8"),
        bar(TARGET - timedelta(days=3), "8"),
        bar(TARGET - timedelta(days=2), "9"),
        bar(TARGET - timedelta(days=1), "10"),
        bar(TARGET, "11"),
    )

    result = evaluate(items, threshold="10")

    assert result.passed is False
    assert "not above" in result.reason


def test_insufficient_bars_fails_explicitly() -> None:
    items = tuple(bar(TARGET - timedelta(days=index), "10") for index in range(3))

    result = evaluate(items, lookback=5)

    assert result.passed is False
    assert result.actual_value["average_close"] is None
    assert "insufficient" in result.reason


def test_empty_bars_fails_explicitly() -> None:
    result = evaluate(())

    assert result.passed is False
    assert "insufficient" in result.reason


def test_nontrading_rows_are_excluded_from_the_window() -> None:
    items = (
        bar(TARGET - timedelta(days=4), "9", is_trading=False),
        bar(TARGET - timedelta(days=3), "10"),
        bar(TARGET - timedelta(days=2), "11"),
        bar(TARGET - timedelta(days=1), "12"),
        bar(TARGET, "13"),
    )

    result = evaluate(items, lookback=3, threshold="10")

    assert result.passed is True
    assert result.actual_value["average_close"] == Decimal("12")


def test_rejects_nonpositive_lookback() -> None:
    with pytest.raises(ValueError, match="lookback_trading_sessions"):
        evaluate((bar(TARGET, "10"),), lookback=0)


def test_rejects_nonpositive_threshold() -> None:
    with pytest.raises(ValueError, match="minimum_average_close"):
        evaluate((bar(TARGET, "10"),), threshold="0")


def test_rejects_duplicate_days_and_mixed_codes() -> None:
    duplicate = (bar(TARGET, "10"), bar(TARGET, "20"))
    with pytest.raises(ValueError, match="unique trading days"):
        evaluate(duplicate)

    mixed = (
        bar(TARGET - timedelta(days=1), "10"),
        bar(TARGET, "11", code="sz.000001"),
    )
    with pytest.raises(ValueError, match="one stock code"):
        evaluate(mixed)


def test_rejects_adjustment_mismatch() -> None:
    with pytest.raises(ValueError, match="adjustment mismatch"):
        evaluate_avg_close_above(
            (bar(TARGET, "10"),),
            METADATA,
            5,
            Decimal("10"),
            AdjustmentMethod.HFQ,
        )
