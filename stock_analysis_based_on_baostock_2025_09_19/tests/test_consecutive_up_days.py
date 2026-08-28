from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata
from stock_manager.rules.consecutive_up_days import evaluate_consecutive_up_days


TARGET = date(2026, 8, 25)
METADATA = DatasetMetadata(
    "market",
    TARGET,
    "fixture",
    datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
    AdjustmentMethod.QFQ,
)


def bar(
    trading_day: date,
    close: str,
    *,
    code: str = "sh.600000",
    is_trading: bool = True,
) -> DailyBar:
    return DailyBar(
        code,
        trading_day,
        Decimal("10"),
        Decimal("11"),
        Decimal("9"),
        Decimal(close),
        Decimal("10"),
        Decimal("1000"),
        Decimal("10000"),
        is_trading,
    )


def evaluate(
    items: tuple[DailyBar, ...],
    lookback: int = 60,
    required: int = 5,
):
    return evaluate_consecutive_up_days(
        items, METADATA, lookback, required, AdjustmentMethod.QFQ
    )


def test_passes_when_a_k_run_exists_inside_the_window() -> None:
    # 8 根连续上涨;窗口 60 覆盖全部数据,参照日缺失时首日不计数,最长连阳 7。
    items = tuple(
        bar(TARGET - timedelta(days=7 - index), str(100 + index))
        for index in range(8)
    )

    result = evaluate(items, lookback=60, required=5)

    assert result.passed is True
    assert result.actual_value["longest_consecutive_up_days"] == 7
    assert result.actual_value["run_start"] == "2026-08-19"
    assert result.actual_value["run_end"] == "2026-08-25"
    assert result.actual_value["reference_trading_day"] is None


def test_passes_when_reference_day_verifies_window_start() -> None:
    # 窗口 5 日,参照日存在,首日也能确认收阳 → 最长连阳 5。
    items = (
        bar(TARGET - timedelta(days=6), "100"),
        bar(TARGET - timedelta(days=5), "101"),
        bar(TARGET - timedelta(days=4), "102"),
        bar(TARGET - timedelta(days=3), "103"),
        bar(TARGET - timedelta(days=2), "104"),
        bar(TARGET - timedelta(days=1), "105"),
    )

    result = evaluate(tuple(reversed(items)), lookback=5, required=3)

    assert result.passed is True
    assert result.actual_value["longest_consecutive_up_days"] == 5
    assert result.actual_value["reference_trading_day"] == "2026-08-19"
    assert result.actual_value["window_start"] == "2026-08-20"
    assert result.actual_value["window_end"] == "2026-08-24"


def test_fails_when_longest_run_is_shorter_than_required() -> None:
    # 只有 4 根连续上涨。
    items = (
        bar(TARGET - timedelta(days=6), "100"),
        bar(TARGET - timedelta(days=5), "101"),
        bar(TARGET - timedelta(days=4), "102"),
        bar(TARGET - timedelta(days=3), "103"),
        bar(TARGET - timedelta(days=2), "104"),
        bar(TARGET - timedelta(days=1), "103"),
    )

    result = evaluate(tuple(reversed(items)), lookback=60, required=5)

    assert result.passed is False
    assert result.actual_value["longest_consecutive_up_days"] == 4
    assert "need 5" in result.reason


def test_flat_session_breaks_the_run() -> None:
    items = (
        bar(TARGET - timedelta(days=6), "100"),
        bar(TARGET - timedelta(days=5), "101"),
        bar(TARGET - timedelta(days=4), "101"),  # 平盘:不算阳线
        bar(TARGET - timedelta(days=3), "102"),
        bar(TARGET - timedelta(days=2), "103"),
        bar(TARGET - timedelta(days=1), "104"),
    )

    result = evaluate(tuple(reversed(items)), lookback=60, required=5)

    assert result.passed is False
    assert result.actual_value["longest_consecutive_up_days"] == 3


def test_insufficient_bars_fails_explicitly() -> None:
    # K=5 需要至少 6 根 bar 才能确认五连阳。
    items = tuple(
        bar(TARGET - timedelta(days=index), str(100 + index)) for index in range(5)
    )

    result = evaluate(items, lookback=60, required=5)

    assert result.passed is False
    assert result.actual_value["longest_consecutive_up_days"] == 0
    assert "insufficient" in result.reason


def test_empty_bars_fails_explicitly() -> None:
    result = evaluate(())

    assert result.passed is False
    assert "insufficient" in result.reason


def test_nontrading_rows_do_not_break_the_window() -> None:
    # 停牌日不参与比较;窗口取最近 5 个有效交易日,参照日验证首日。
    items = (
        bar(TARGET - timedelta(days=7), "100"),
        bar(TARGET - timedelta(days=6), "101"),
        bar(TARGET - timedelta(days=5), "102", is_trading=False),
        bar(TARGET - timedelta(days=4), "103"),
        bar(TARGET - timedelta(days=3), "104"),
        bar(TARGET - timedelta(days=2), "105"),
        bar(TARGET - timedelta(days=1), "106"),
    )

    result = evaluate(tuple(reversed(items)), lookback=5, required=5)

    assert result.passed is True
    assert result.actual_value["longest_consecutive_up_days"] == 5
    assert result.actual_value["reference_trading_day"] == "2026-08-18"


def test_rejects_required_larger_than_lookback() -> None:
    with pytest.raises(ValueError, match="required_consecutive_days"):
        evaluate((bar(TARGET, "10"),), lookback=5, required=10)


def test_rejects_nonpositive_parameters() -> None:
    with pytest.raises(ValueError, match="lookback_trading_sessions"):
        evaluate((bar(TARGET, "10"),), lookback=0)
    with pytest.raises(ValueError, match="required_consecutive_days"):
        evaluate((bar(TARGET, "10"),), required=0)


def test_rejects_duplicate_days_and_mixed_codes() -> None:
    duplicate = (bar(TARGET, "10"), bar(TARGET, "20"))
    with pytest.raises(ValueError, match="unique trading days"):
        evaluate(duplicate)

    mixed = (
        bar(TARGET - timedelta(days=1), "10"),
        bar(TARGET, "11", code="sz.000001"),
    )
    with pytest.raises(ValueError, match="one stock code"):
        evaluate(mixed)


def test_rejects_adjustment_mismatch() -> None:
    with pytest.raises(ValueError, match="adjustment mismatch"):
        evaluate_consecutive_up_days(
            (bar(TARGET, "10"),),
            METADATA,
            5,
            2,
            AdjustmentMethod.HFQ,
        )
