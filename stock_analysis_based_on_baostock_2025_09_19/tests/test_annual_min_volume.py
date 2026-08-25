from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata
from stock_manager.rules.annual_min_volume import evaluate_annual_min_volume


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


def evaluate(items: tuple[DailyBar, ...], minimum_sessions: int = 3):
    return evaluate_annual_min_volume(
        items,
        METADATA,
        TARGET,
        365,
        minimum_sessions,
        True,
        AdjustmentMethod.QFQ,
    )


def test_target_day_passes_when_tied_for_minimum_volume() -> None:
    items = (
        bar(TARGET - timedelta(days=2), "10"),
        bar(TARGET - timedelta(days=1), "20"),
        bar(TARGET, "10"),
    )

    result = evaluate(tuple(reversed(items)))

    assert result.passed is True
    assert result.actual_value["target_volume"] == Decimal("10")
    assert result.actual_value["minimum_dates"] == (
        "2026-08-23",
        "2026-08-25",
    )


def test_zero_volume_and_suspension_are_excluded() -> None:
    items = (
        bar(TARGET - timedelta(days=3), "0"),
        bar(TARGET - timedelta(days=2), "0", is_trading=False),
        bar(TARGET - timedelta(days=1), "20"),
        bar(TARGET, "10"),
    )

    result = evaluate(items, minimum_sessions=2)

    assert result.passed is True
    assert result.actual_value["valid_session_count"] == 2


def test_insufficient_sessions_fails_explicitly() -> None:
    result = evaluate((bar(TARGET, "10"),), minimum_sessions=120)

    assert result.passed is False
    assert result.actual_value["valid_session_count"] == 1
    assert "insufficient" in result.reason


def test_target_must_be_a_nonzero_trading_session() -> None:
    result = evaluate(
        (
            bar(TARGET - timedelta(days=1), "10"),
            bar(TARGET, "0"),
        ),
        minimum_sessions=1,
    )

    assert result.passed is False
    assert "target trading day" in result.reason


def test_rule_uses_calendar_day_window_inclusively() -> None:
    result = evaluate(
        (
            bar(TARGET - timedelta(days=366), "1"),
            bar(TARGET - timedelta(days=365), "20"),
            bar(TARGET - timedelta(days=1), "30"),
            bar(TARGET, "10"),
        )
    )

    assert result.passed is True
    assert result.actual_value["valid_session_count"] == 3


def test_rule_rejects_duplicate_days_and_mixed_codes() -> None:
    duplicate = (bar(TARGET, "10"), bar(TARGET, "20"))
    with pytest.raises(ValueError, match="unique trading days"):
        evaluate(duplicate, minimum_sessions=1)

    mixed = (
        bar(TARGET - timedelta(days=1), "20"),
        bar(TARGET, "10", code="sz.000001"),
    )
    with pytest.raises(ValueError, match="one stock code"):
        evaluate(mixed, minimum_sessions=1)


def test_rule_rejects_adjustment_mismatch() -> None:
    with pytest.raises(ValueError, match="adjustment mismatch"):
        evaluate_annual_min_volume(
            (bar(TARGET, "10"),),
            METADATA,
            TARGET,
            365,
            1,
            True,
            AdjustmentMethod.HFQ,
        )
