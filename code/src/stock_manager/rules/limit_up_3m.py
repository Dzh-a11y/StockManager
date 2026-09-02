"""Recent gain-count rule (涨幅次数)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "limit_up_3m"


def evaluate_limit_up_3m(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    minimum_events: int,
    maximum_events: int,
    limit_ratio_lower_exclusive: Decimal,
    limit_ratio_upper_exclusive: Decimal,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when the recent gain-event count lies in the configured inclusive range."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    if minimum_events < 0 or maximum_events < minimum_events:
        raise ValueError("event count bounds are invalid")
    if limit_ratio_lower_exclusive >= limit_ratio_upper_exclusive:
        raise ValueError("limit ratio lower bound must be below upper bound")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    event_days = tuple(
        bar.trading_day.isoformat()
        for bar in window
        if bar.preclose > 0
        and limit_ratio_lower_exclusive < bar.close / bar.preclose < limit_ratio_upper_exclusive
    )
    count = len(event_days)
    passed = minimum_events <= count <= maximum_events
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "minimum_events": minimum_events,
        "maximum_events": maximum_events,
        "limit_ratio_open_interval": [limit_ratio_lower_exclusive, limit_ratio_upper_exclusive],
        "adjustment": required_adjustment.value,
    }
    reason = f"found {count} gain event(s); expected {minimum_events}..{maximum_events}"
    return RuleResult(RULE_ID, passed, {"count": count, "trading_days": event_days}, threshold, reason)
