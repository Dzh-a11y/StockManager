"""Limit-up break or fake-negative-line rule."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "limit_up_breakout"


def evaluate_limit_up_breakout(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    signal_lookback_trading_sessions: int,
    highest_lookback_trading_sessions: int,
    limit_ratio_lower_exclusive: Decimal,
    limit_ratio_upper_exclusive: Decimal,
    close_below_high_amount: Decimal,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass on a configured break event or a fake negative line in the signal window."""
    require_positive_integer(signal_lookback_trading_sessions, "signal_lookback_trading_sessions")
    require_positive_integer(highest_lookback_trading_sessions, "highest_lookback_trading_sessions")
    if limit_ratio_lower_exclusive >= limit_ratio_upper_exclusive:
        raise ValueError("limit ratio lower bound must be below upper bound")
    if close_below_high_amount < 0:
        raise ValueError("close_below_high_amount must be non-negative")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    signal_window = ordered[-signal_lookback_trading_sessions:]
    history_window = ordered[-highest_lookback_trading_sessions:]
    history_max_close = max((bar.close for bar in history_window), default=None)
    signal_max_high = max((bar.high for bar in signal_window), default=None)
    events: list[dict[str, object]] = []
    for index, current in enumerate(signal_window):
        previous = signal_window[index - 1] if index > 0 else None
        limit_ratio = None if current.preclose <= 0 else current.high / current.preclose
        is_break = (
            limit_ratio is not None
            and limit_ratio_lower_exclusive < limit_ratio < limit_ratio_upper_exclusive
            and current.close < current.high - close_below_high_amount
            and signal_max_high is not None
            and current.high == signal_max_high
            and history_max_close is not None
            and history_max_close <= signal_max_high
        )
        is_fake_negative = (
            previous is not None and current.close < current.open and current.close > previous.close
        )
        if is_break or is_fake_negative:
            events.append(
                {
                    "trading_day": current.trading_day.isoformat(),
                    "kind": "limit_up_break" if is_break else "fake_negative_line",
                    "limit_ratio": limit_ratio,
                }
            )
    threshold = {
        "signal_lookback_trading_sessions": signal_lookback_trading_sessions,
        "highest_lookback_trading_sessions": highest_lookback_trading_sessions,
        "limit_ratio_open_interval": [limit_ratio_lower_exclusive, limit_ratio_upper_exclusive],
        "close_below_high_amount": close_below_high_amount,
        "adjustment": required_adjustment.value,
    }
    passed = bool(events)
    reason = "breakout or fake-negative event found" if passed else "no breakout or fake-negative event"
    return RuleResult(RULE_ID, passed, tuple(events), threshold, reason)
