"""N-session high/low price range ratio rule."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "price_range_ratio"


def evaluate_price_range_ratio(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    minimum_ratio: Decimal,
    maximum_ratio: Decimal,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when high/low ratio in the latest N sessions lies in the configured interval."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    if minimum_ratio <= 0:
        raise ValueError("minimum_ratio must be positive")
    if maximum_ratio < minimum_ratio:
        raise ValueError("maximum_ratio must be greater than or equal to minimum_ratio")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "minimum_ratio": minimum_ratio,
        "maximum_ratio": maximum_ratio,
        "adjustment": required_adjustment.value,
    }
    if len(window) < lookback_trading_sessions:
        return RuleResult(
            RULE_ID,
            False,
            {
                "highest": None,
                "lowest": None,
                "ratio": None,
                "high_dates": (),
                "low_dates": (),
                "session_count": len(window),
                "window_start": window[0].trading_day.isoformat() if window else None,
                "window_end": window[-1].trading_day.isoformat() if window else None,
            },
            threshold,
            "insufficient trading sessions",
        )
    lowest = min(bar.low for bar in window)
    if lowest <= 0:
        raise ValueError("low prices must be positive for ratio calculation")
    highest = max(bar.high for bar in window)
    ratio = highest / lowest
    passed = minimum_ratio <= ratio <= maximum_ratio
    high_dates = tuple(
        bar.trading_day.isoformat() for bar in window if bar.high == highest
    )
    low_dates = tuple(
        bar.trading_day.isoformat() for bar in window if bar.low == lowest
    )
    reason = (
        f"high/low ratio {ratio} is within [{minimum_ratio}, {maximum_ratio}]"
        if passed
        else f"high/low ratio {ratio} is outside [{minimum_ratio}, {maximum_ratio}]"
    )
    return RuleResult(
        RULE_ID,
        passed,
        {
            "highest": highest,
            "lowest": lowest,
            "ratio": ratio,
            "high_dates": high_dates,
            "low_dates": low_dates,
            "session_count": len(window),
            "window_start": window[0].trading_day.isoformat(),
            "window_end": window[-1].trading_day.isoformat(),
        },
        threshold,
        reason,
    )
