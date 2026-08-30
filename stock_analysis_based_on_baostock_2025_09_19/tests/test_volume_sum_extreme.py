from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata
from stock_manager.rules.volume_sum_extreme import evaluate_volume_sum_extreme


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
    volume: str,
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
        Decimal("10"),
        Decimal("10"),
        Decimal(volume),
        Decimal("100"),
        is_trading,
    )


def evaluate(
    items: tuple[DailyBar, ...],
    *,
    lookback: int = 4,
    target_days: int = 2,
    reference_days: int = 2,
    mode: str = "min",
    minimum_sessions: int = 4,
):
    return evaluate_volume_sum_extreme(
        items,
        METADATA,
        lookback,
        target_days,
        reference_days,
        mode,
        minimum_sessions,
        AdjustmentMethod.QFQ,
    )


def test_passes_when_latest_two_day_sum_is_minimum() -> None:
    items = (
        bar(TARGET - timedelta(days=3), "100"),
        bar(TARGET - timedelta(days=2), "50"),
        bar(TARGET - timedelta(days=1), "40"),
        bar(TARGET, "30"),
    )

    result = evaluate(items)

    assert result.passed is True
    assert result.actual_value["target_sum"] == Decimal("70")
    assert result.actual_value["extreme_sum"] == Decimal("70")
    assert ("2026-08-24", "2026-08-25") in result.actual_value["extreme_windows"]


def test_passes_when_latest_two_day_sum_is_maximum() -> None:
    items = (
        bar(TARGET - timedelta(days=3), "10"),
        bar(TARGET - timedelta(days=2), "20"),
        bar(TARGET - timedelta(days=1), "30"),
        bar(TARGET, "40"),
    )

    result = evaluate(items, mode="max")

    assert result.passed is True
    assert result.actual_value["target_sum"] == Decimal("70")
    assert result.actual_value["extreme_sum"] == Decimal("70")


def test_supports_different_target_and_reference_days() -> None:
    # 最近 3 天量能和 = 100，是全部连续 1 天量能和的最大值。
    items = (
        bar(TARGET - timedelta(days=2), "0"),
        bar(TARGET - timedelta(days=1), "0"),
        bar(TARGET, "100"),
    )

    result = evaluate(
        items,
        lookback=3,
        target_days=3,
        reference_days=1,
        mode="max",
        minimum_sessions=3,
    )

    assert result.passed is True
    assert result.actual_value["target_sum"] == Decimal("100")
    assert result.actual_value["extreme_sum"] == Decimal("100")


def test_fails_when_latest_sum_is_not_extreme() -> None:
    items = (
        bar(TARGET - timedelta(days=3), "10"),
        bar(TARGET - timedelta(days=2), "20"),
        bar(TARGET - timedelta(days=1), "30"),
        bar(TARGET, "40"),
    )

    result = evaluate(items)

    assert result.passed is False
    assert result.actual_value["extreme_sum"] == Decimal("30")


def test_insufficient_sessions_fails_explicitly() -> None:
    result = evaluate(
        (
            bar(TARGET - timedelta(days=1), "30"),
            bar(TARGET, "40"),
        ),
        minimum_sessions=3,
    )

    assert result.passed is False
    assert result.actual_value["valid_session_count"] == 2
    assert "insufficient" in result.reason


def test_nontrading_rows_are_excluded() -> None:
    items = (
        bar(TARGET - timedelta(days=3), "100", is_trading=False),
        bar(TARGET - timedelta(days=2), "50"),
        bar(TARGET - timedelta(days=1), "40"),
        bar(TARGET, "30"),
    )

    result = evaluate(items, lookback=3, minimum_sessions=3)

    assert result.passed is True
    assert result.actual_value["valid_session_count"] == 3


def test_rejects_invalid_mode_and_window_sizes() -> None:
    with pytest.raises(ValueError, match="mode"):
        evaluate(
            (bar(TARGET, "10"),),
            minimum_sessions=1,
            mode="middle",
        )
    with pytest.raises(ValueError, match="lookback_trading_sessions"):
        evaluate(
            (bar(TARGET, "10"),),
            lookback=1,
            target_days=2,
            reference_days=1,
            minimum_sessions=1,
        )


def test_rejects_duplicate_days_and_mixed_codes() -> None:
    duplicate = (bar(TARGET, "10"), bar(TARGET, "20"))
    with pytest.raises(ValueError, match="unique trading days"):
        evaluate(duplicate, minimum_sessions=1)

    mixed = (
        bar(TARGET - timedelta(days=1), "10"),
        bar(TARGET, "11", code="sz.000001"),
    )
    with pytest.raises(ValueError, match="one stock code"):
        evaluate(mixed, minimum_sessions=1)


def test_rejects_adjustment_mismatch() -> None:
    with pytest.raises(ValueError, match="adjustment mismatch"):
        evaluate_volume_sum_extreme(
            (bar(TARGET, "10"),),
            METADATA,
            2,
            1,
            1,
            "min",
            1,
            AdjustmentMethod.HFQ,
        )
