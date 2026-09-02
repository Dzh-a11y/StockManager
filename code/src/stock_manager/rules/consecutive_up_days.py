"""Consecutive up-session (K连阳) search within a lookback window.

Searches the latest N trading sessions for at least K consecutive sessions
whose close is strictly higher than the previous trading session's close
(K连阳; K = 5 is the well-known 五连阳). The oldest windowed session is
compared against the session immediately before the window when available, so
the data requirement is N + 1 trading sessions.
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
    required_consecutive_days: int,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when at least K consecutive up sessions occur in the latest N sessions.

    A session is up when its close is strictly higher than the previous trading
    session's close. The oldest windowed session is verified against the session
    immediately before the window; when that reference bar is unavailable (short
    history), the first windowed session is simply not counted as up, which can
    only under-report a run, never over-report one. A K-session run needs K + 1
    bars, so fewer bars fail explicitly as insufficient data.
    """
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    require_positive_integer(
        required_consecutive_days, "required_consecutive_days"
    )
    if required_consecutive_days > lookback_trading_sessions:
        raise ValueError(
            "required_consecutive_days must not exceed lookback_trading_sessions"
        )
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    window_days = tuple(bar.trading_day.isoformat() for bar in window)
    reference_day = (
        ordered[-lookback_trading_sessions - 1].trading_day.isoformat()
        if len(ordered) >= lookback_trading_sessions + 1
        else None
    )
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "required_consecutive_days": required_consecutive_days,
        "adjustment": required_adjustment.value,
    }
    minimum_bars = required_consecutive_days + 1
    if len(ordered) < minimum_bars:
        return RuleResult(
            RULE_ID,
            False,
            {
                "lookback_trading_sessions": lookback_trading_sessions,
                "required_consecutive_days": required_consecutive_days,
                "longest_consecutive_up_days": 0,
                "session_count": len(ordered),
                "window_start": window_days[0] if window_days else None,
                "window_end": window_days[-1] if window_days else None,
                "reference_trading_day": reference_day,
                "run_start": None,
                "run_end": None,
                "trading_days": window_days,
            },
            threshold,
            f"insufficient trading data: {len(ordered)} bar(s), need at least {minimum_bars}",
        )
    up_flags: list[bool] = []
    for index, current in enumerate(window):
        if index > 0:
            previous = window[index - 1]
        else:
            previous = ordered[-lookback_trading_sessions - 1] if reference_day is not None else None
        up_flags.append(previous is not None and current.close > previous.close)

    longest = 0
    run_length = 0
    run_start: str | None = None
    run_end: str | None = None
    best_run_start: str | None = None
    best_run_end: str | None = None
    for index, is_up in enumerate(up_flags):
        if is_up:
            if run_length == 0:
                run_start = window_days[index]
            run_length += 1
            run_end = window_days[index]
        else:
            if run_length > longest:
                longest = run_length
                best_run_start = run_start
                best_run_end = run_end
            run_length = 0
    if run_length > longest:
        longest = run_length
        best_run_start = run_start
        best_run_end = run_end

    passed = longest >= required_consecutive_days
    reason = (
        f"found a {longest}-session consecutive up run in the last "
        f"{lookback_trading_sessions} sessions (need {required_consecutive_days})"
        if passed
        else f"longest consecutive up run is {longest} session(s) in the last "
        f"{lookback_trading_sessions} sessions (need {required_consecutive_days})"
    )
    return RuleResult(
        RULE_ID,
        passed,
        {
            "lookback_trading_sessions": lookback_trading_sessions,
            "required_consecutive_days": required_consecutive_days,
            "longest_consecutive_up_days": longest,
            "session_count": len(window),
            "window_start": window_days[0],
            "window_end": window_days[-1],
            "reference_trading_day": reference_day,
            "run_start": best_run_start,
            "run_end": best_run_end,
            "trading_days": window_days,
        },
        threshold,
        reason,
    )
