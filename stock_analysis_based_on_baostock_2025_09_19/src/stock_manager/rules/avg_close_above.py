"""N-session average close threshold rule (N日均价下限).

The average of the latest N trading sessions' closing prices must be strictly
above the configured price threshold; an average that merely equals the
threshold fails.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "avg_close_above"


def evaluate_avg_close_above(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    minimum_average_close: Decimal,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when the average close of the latest N sessions exceeds the threshold."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    if minimum_average_close <= 0:
        raise ValueError("minimum_average_close must be positive")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    window_days = tuple(bar.trading_day.isoformat() for bar in window)
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "minimum_average_close": minimum_average_close,
        "adjustment": required_adjustment.value,
    }
    if len(window) < lookback_trading_sessions:
        return RuleResult(
            RULE_ID,
            False,
            {
                "session_count": len(window),
                "average_close": None,
                "latest_close": window[-1].close if window else None,
                "window_start": window_days[0] if window_days else None,
                "window_end": window_days[-1] if window_days else None,
                "trading_days": window_days,
            },
            threshold,
            f"insufficient trading data: {len(window)} bar(s), need {lookback_trading_sessions}",
        )
    total = Decimal("0")
    for bar in window:
        total += bar.close
    average = total / Decimal(lookback_trading_sessions)
    passed = average > minimum_average_close
    reason = (
        f"average close {average} over {lookback_trading_sessions} sessions "
        f"{'is above' if passed else 'is not above'} {minimum_average_close}"
    )
    return RuleResult(
        RULE_ID,
        passed,
        {
            "session_count": len(window),
            "average_close": average,
            "latest_close": window[-1].close,
            "window_start": window_days[0],
            "window_end": window_days[-1],
            "trading_days": window_days,
        },
        threshold,
        reason,
    )
