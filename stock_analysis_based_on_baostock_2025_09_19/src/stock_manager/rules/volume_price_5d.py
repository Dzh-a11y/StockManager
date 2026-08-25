"""Volume and price rise rule."""

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "volume_price_5d"


def evaluate_volume_price_5d(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    minimum_volume_ratio: Decimal,
    minimum_close_rise_percent: Decimal,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when one adjacent pair meets both volume and close-rise thresholds."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    matches: list[dict[str, object]] = []
    for previous, current in zip(window, window[1:], strict=False):
        if previous.volume <= 0 or previous.close == 0:
            continue
        volume_ratio = current.volume / previous.volume
        rise_percent = (current.close - previous.close) / previous.close * Decimal("100")
        if volume_ratio >= minimum_volume_ratio and rise_percent >= minimum_close_rise_percent:
            matches.append(
                {
                    "trading_day": current.trading_day.isoformat(),
                    "volume_ratio": volume_ratio,
                    "close_rise_percent": rise_percent,
                }
            )
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "minimum_volume_ratio": minimum_volume_ratio,
        "minimum_close_rise_percent": minimum_close_rise_percent,
        "adjustment": required_adjustment.value,
    }
    passed = bool(matches)
    reason = "matching volume-price event found" if passed else "no matching volume-price event"
    return RuleResult(RULE_ID, passed, tuple(matches), threshold, reason)
