"""Shared validation for pure technical rules."""

from collections.abc import Sequence

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata


def prepare_bars(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    required_adjustment: AdjustmentMethod,
) -> tuple[DailyBar, ...]:
    """Validate metadata and return trading bars ordered by trading day."""
    if metadata.adjustment is not required_adjustment:
        raise ValueError(
            f"adjustment mismatch: expected {required_adjustment.value}, "
            f"got {metadata.adjustment.value}"
        )
    trading_bars = tuple(sorted((bar for bar in bars if bar.is_trading), key=lambda bar: bar.trading_day))
    if len({bar.trading_day for bar in trading_bars}) != len(trading_bars):
        raise ValueError("daily bars must have unique trading days")
    codes = {bar.code for bar in trading_bars}
    if len(codes) > 1:
        raise ValueError("a rule evaluation must contain exactly one stock code")
    if trading_bars and trading_bars[-1].trading_day > metadata.trading_day:
        raise ValueError("daily bars must not be newer than dataset metadata")
    return trading_bars


def require_positive_integer(value: int, field_name: str) -> None:
    """Reject invalid count thresholds with a precise configuration error."""
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
