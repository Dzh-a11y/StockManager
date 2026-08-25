"""Price volatility multiple rule."""

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "volatility_multiple"


def evaluate_volatility_multiple(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    maximum_multiple: Decimal,
    minimum_required_sessions: int,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when high-to-low volatility does not exceed the configured maximum."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    require_positive_integer(minimum_required_sessions, "minimum_required_sessions")
    if maximum_multiple <= 0:
        raise ValueError("maximum_multiple must be positive")
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    if len(window) < minimum_required_sessions:
        actual = {"sessions": len(window), "multiple": None}
        threshold = {
            "lookback_trading_sessions": lookback_trading_sessions,
            "minimum_required_sessions": minimum_required_sessions,
            "maximum_multiple": maximum_multiple,
            "adjustment": required_adjustment.value,
        }
        return RuleResult(RULE_ID, False, actual, threshold, "insufficient trading sessions")
    lowest = min(bar.low for bar in window)
    if lowest <= 0:
        raise ValueError("low prices must be positive for volatility calculation")
    highest = max(bar.high for bar in window)
    multiple = highest / lowest
    passed = multiple <= maximum_multiple
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "minimum_required_sessions": minimum_required_sessions,
        "maximum_multiple": maximum_multiple,
        "adjustment": required_adjustment.value,
    }
    reason = f"volatility multiple {multiple} is within limit" if passed else f"volatility multiple {multiple} exceeds limit"
    return RuleResult(RULE_ID, passed, {"highest": highest, "lowest": lowest, "multiple": multiple}, threshold, reason)
