"""Minimum-close-price rule over an inclusive calendar-day window."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer

RULE_ID = "annual_min_close_price"


def evaluate_annual_min_close_price(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    target_day: date,
    lookback_calendar_days: int,
    minimum_required_trading_sessions: int,
    exclude_zero_close: bool,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when target close equals the minimum in the calendar-day window."""
    require_positive_integer(lookback_calendar_days, "lookback_calendar_days")
    require_positive_integer(
        minimum_required_trading_sessions,
        "minimum_required_trading_sessions",
    )
    if target_day != metadata.trading_day:
        raise ValueError("target_day must match dataset metadata")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    start = target_day - timedelta(days=lookback_calendar_days)
    window = tuple(
        item
        for item in ordered
        if start <= item.trading_day <= target_day
        and (not exclude_zero_close or item.close > 0)
    )
    threshold = {
        "lookback_calendar_days": lookback_calendar_days,
        "minimum_required_trading_sessions": minimum_required_trading_sessions,
        "comparison": "equal_to_minimum",
        "exclude_zero_close": exclude_zero_close,
        "adjustment": required_adjustment.value,
    }
    target = next((item for item in window if item.trading_day == target_day), None)
    if target is None:
        return RuleResult(
            RULE_ID,
            False,
            {
                "target_close": None,
                "minimum_close": None,
                "minimum_dates": (),
                "valid_session_count": len(window),
            },
            threshold,
            "target trading day is missing or excluded from the valid window",
        )
    if len(window) < minimum_required_trading_sessions:
        return RuleResult(
            RULE_ID,
            False,
            {
                "target_close": target.close,
                "minimum_close": None,
                "minimum_dates": (),
                "valid_session_count": len(window),
            },
            threshold,
            "insufficient valid trading sessions",
        )
    minimum = min(item.close for item in window)
    minimum_dates = tuple(
        item.trading_day.isoformat() for item in window if item.close == minimum
    )
    passed = target.close == minimum
    actual = {
        "target_close": target.close,
        "minimum_close": minimum,
        "minimum_dates": minimum_dates,
        "valid_session_count": len(window),
    }
    reason = (
        "target close equals the minimum in the calendar-day window"
        if passed
        else "target close is above the minimum in the calendar-day window"
    )
    return RuleResult(RULE_ID, passed, actual, threshold, reason)
