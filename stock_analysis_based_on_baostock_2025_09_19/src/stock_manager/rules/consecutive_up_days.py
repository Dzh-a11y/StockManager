"""Recent N consecutive up-session (N连阳) rule.

A trading session counts as "up" when its close is strictly higher than the
previous trading session's close. The rule passes when every one of the latest
N trading sessions is up, which is the classic "N连阳" pattern (N consecutive
bullish sessions); N = 5 is the well-known 五连阳.
"""

from __future__ import annotations

from collections.abc import Sequence

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "consecutive_up_days"


def evaluate_consecutive_up_days(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when the latest N trading sessions each closed above the previous session.

    The oldest windowed session is compared against the session immediately
    before the window, so a full check needs N + 1 trading bars. With fewer
    bars the result fails explicitly as insufficient data.
    """
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    required = lookback_trading_sessions
    window = ordered[-required:]
    window_days = tuple(bar.trading_day.isoformat() for bar in window)
    reference_day = (
        ordered[-required - 1].trading_day.isoformat()
        if len(ordered) >= required + 1
        else None
    )
    consecutive = 0
    for index in range(len(ordered) - 1, 0, -1):
        if ordered[index].close <= ordered[index - 1].close:
            break
        consecutive += 1
    threshold = {
        "lookback_trading_sessions": required,
        "adjustment": required_adjustment.value,
    }
    if reference_day is None:
        return RuleResult(
            RULE_ID,
            False,
            {
                "lookback_trading_sessions": required,
                "consecutive_up_sessions": consecutive,
                "session_count": len(ordered),
                "window_start": window_days[0] if window_days else None,
                "window_end": window_days[-1] if window_days else None,
                "reference_trading_day": None,
                "trading_days": window_days,
            },
            threshold,
            f"insufficient trading data: {len(ordered)} bar(s), need {required + 1}",
        )
    passed = consecutive >= required
    reason = (
        f"latest {required} sessions each closed above the previous session"
        if passed
        else f"only {consecutive} consecutive up session(s); expected at least {required}"
    )
    return RuleResult(
        RULE_ID,
        passed,
        {
            "lookback_trading_sessions": required,
            "consecutive_up_sessions": consecutive,
            "session_count": len(ordered),
            "window_start": window_days[0],
            "window_end": window_days[-1],
            "reference_trading_day": reference_day,
            "trading_days": window_days,
        },
        threshold,
        reason,
    )
