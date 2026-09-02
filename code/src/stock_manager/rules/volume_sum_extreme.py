"""Recent N-session volume sum extreme rule."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, RuleResult
from stock_manager.rules._shared import prepare_bars, require_positive_integer


RULE_ID = "volume_sum_extreme"


def evaluate_volume_sum_extreme(
    bars: Sequence[DailyBar],
    metadata: DatasetMetadata,
    lookback_trading_sessions: int,
    target_days: int,
    reference_days: int,
    mode: str,
    minimum_required_trading_sessions: int,
    required_adjustment: AdjustmentMethod,
) -> RuleResult:
    """Pass when the latest target-day volume sum is the min/max of all reference sums."""
    require_positive_integer(lookback_trading_sessions, "lookback_trading_sessions")
    require_positive_integer(target_days, "target_days")
    require_positive_integer(reference_days, "reference_days")
    require_positive_integer(
        minimum_required_trading_sessions,
        "minimum_required_trading_sessions",
    )
    if mode not in ("min", "max"):
        raise ValueError("mode must be 'min' or 'max'")
    if lookback_trading_sessions < max(target_days, reference_days):
        raise ValueError(
            "lookback_trading_sessions must be at least max(target_days, reference_days)"
        )
    ordered = prepare_bars(bars, metadata, required_adjustment)
    window = ordered[-lookback_trading_sessions:]
    threshold = {
        "lookback_trading_sessions": lookback_trading_sessions,
        "target_days": target_days,
        "reference_days": reference_days,
        "mode": mode,
        "minimum_required_trading_sessions": minimum_required_trading_sessions,
        "adjustment": required_adjustment.value,
    }
    if len(window) < max(
        minimum_required_trading_sessions, target_days, reference_days
    ):
        return RuleResult(
            RULE_ID,
            False,
            {
                "target_sum": None,
                "extreme_sum": None,
                "extreme_windows": (),
                "valid_session_count": len(window),
                "window_start": window[0].trading_day.isoformat() if window else None,
                "window_end": window[-1].trading_day.isoformat() if window else None,
            },
            threshold,
            "insufficient trading sessions",
        )

    target_sum = sum(
        (bar.volume for bar in window[-target_days:]),
        Decimal("0"),
    )
    reference_sums: list[Decimal] = []
    reference_windows: list[tuple[str, str]] = []
    for index in range(len(window) - reference_days + 1):
        chunk = window[index : index + reference_days]
        reference_sums.append(
            sum((bar.volume for bar in chunk), Decimal("0"))
        )
        reference_windows.append(
            (
                chunk[0].trading_day.isoformat(),
                chunk[-1].trading_day.isoformat(),
            )
        )
    extreme = (
        min(reference_sums) if mode == "min" else max(reference_sums)
    )
    extreme_windows = tuple(
        reference_windows[index]
        for index, value in enumerate(reference_sums)
        if value == extreme
    )
    passed = target_sum == extreme
    reason = (
        f"latest {target_days}-day volume sum {target_sum} is the {mode} of "
        f"{reference_days}-day sums"
        if passed
        else f"latest {target_days}-day volume sum {target_sum} is not the {mode} "
        f"of {reference_days}-day sums"
    )
    return RuleResult(
        RULE_ID,
        passed,
        {
            "target_sum": target_sum,
            "extreme_sum": extreme,
            "extreme_windows": extreme_windows,
            "valid_session_count": len(window),
            "window_start": window[0].trading_day.isoformat(),
            "window_end": window[-1].trading_day.isoformat(),
        },
        threshold,
        reason,
    )
