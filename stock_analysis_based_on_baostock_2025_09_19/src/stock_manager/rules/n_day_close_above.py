"""N-session close floor rule (N日收盘价下限).

Every one of the latest N trading sessions' closing prices must be strictly
above the configured price threshold; a session whose close merely equals the
threshold fails the rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "n_day_close_above"


def evaluate_n_day_close_above(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    minimum_close: Decimal,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when every close in the latest N sessions is strictly above the threshold."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    if minimum_close <= 0:
        raise ValueError("minimum_close must be positive")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    window_days = tuple(bar.trading_day.isoformat() for bar in window)
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "minimum_close": minimum_close,
        "adjustment": required_adjustment.value,
    }
    if len(window) < lookback_trading_sessions:
        return RuleResult(
            RULE_ID,
            False,
            {
                "session_count": len(window),
                "lowest_close": None,
                "latest_close": window[-1].close if window else None,
                "below_threshold_days": (),
                "window_start": window_days[0] if window_days else None,
                "window_end": window_days[-1] if window_days else None,
                "trading_days": window_days,
            },
            threshold,
            f"insufficient trading data: {len(window)} bar(s), need {lookback_trading_sessions}",
        )
    below = tuple(
        bar.trading_day.isoformat() for bar in window if bar.close <= minimum_close
    )
    lowest = min((bar.close for bar in window), default=None)
    passed = not below
    reason = (
        f"all {lookback_trading_sessions} sessions closed above {minimum_close}"
        if passed
        else f"{len(below)} of {lookback_trading_sessions} sessions closed at or below {minimum_close}"
    )
    return RuleResult(
        RULE_ID,
        passed,
        {
            "session_count": len(window),
            "lowest_close": lowest,
            "latest_close": window[-1].close,
            "below_threshold_days": below,
            "window_start": window_days[0],
            "window_end": window_days[-1],
            "trading_days": window_days,
        },
        threshold,
        reason,
    )
