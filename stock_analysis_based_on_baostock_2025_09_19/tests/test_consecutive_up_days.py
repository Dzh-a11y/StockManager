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


def evaluate(items: tuple[DailyBar, ...], lookback: int = 5):
    return evaluate_consecutive_up_days(
        items, METADATA, lookback, AdjustmentMethod.QFQ
    )


def test_passes_when_last_n_sessions_are_all_up() -> None:
    # 5 根连续上涨 + 1 根参照日 = 五连阳。
    items = (
        bar(TARGET - timedelta(days=6), "100"),
        bar(TARGET - timedelta(days=5), "101"),
        bar(TARGET - timedelta(days=4), "102"),
        bar(TARGET - timedelta(days=3), "103"),
        bar(TARGET - timedelta(days=2), "104"),
        bar(TARGET - timedelta(days=1), "105"),
        bar(TARGET, "106"),
    )

    result = evaluate(tuple(reversed(items)))

    assert result.passed is True
    assert result.actual_value["consecutive_up_sessions"] == 6
    assert result.actual_value["reference_trading_day"] == "2026-08-20"
    assert result.actual_value["window_start"] == "2026-08-21"
    assert result.actual_value["window_end"] == "2026-08-25"
    assert len(result.actual_value["trading_days"]) == 5


def test_fails_when_one_session_is_flat() -> None:
    items = (
        bar(TARGET - timedelta(days=6), "100"),
        bar(TARGET - timedelta(days=5), "101"),
        bar(TARGET - timedelta(days=4), "101"),  # 平盘:收盘不高于前一日
        bar(TARGET - timedelta(days=3), "102"),
        bar(TARGET - timedelta(days=2), "103"),
        bar(TARGET - timedelta(days=1), "104"),
        bar(TARGET, "105"),
    )

    result = evaluate(tuple(reversed(items)))

    assert result.passed is False
    assert "consecutive up" in result.reason


def test_fails_when_one_session_closes_lower() -> None:
    items = (
        bar(TARGET - timedelta(days=6), "100"),
        bar(TARGET - timedelta(days=5), "103"),
        bar(TARGET - timedelta(days=4), "102"),  # 收阴
        bar(TARGET - timedelta(days=3), "103"),
        bar(TARGET - timedelta(days=2), "104"),
        bar(TARGET - timedelta(days=1), "105"),
        bar(TARGET, "106"),
    )

    result = evaluate(tuple(reversed(items)))

    assert result.passed is False


def test_single_session_check_uses_reference_day() -> None:
    # n=1:最近 1 个交易日收盘高于前一日。
    items = (
        bar(TARGET - timedelta(days=1), "100"),
        bar(TARGET, "101"),
    )

    result = evaluate(items, lookback=1)

    assert result.passed is True
    assert result.actual_value["consecutive_up_sessions"] == 1


def test_insufficient_bars_fails_explicitly() -> None:
    items = tuple(bar(TARGET - timedelta(days=index), str(100 + index)) for index in range(3))

    result = evaluate(items, lookback=5)

    assert result.passed is False
    assert result.actual_value["reference_trading_day"] is None
    assert "insufficient" in result.reason


def test_empty_bars_fails_explicitly() -> None:
    result = evaluate(())

    assert result.passed is False
    assert "insufficient" in result.reason


def test_nontrading_rows_do_not_break_the_window() -> None:
    # 中间一根非交易日不参与比较,窗口仍取最近 5 个有效交易日。
    items = (
        bar(TARGET - timedelta(days=7), "100"),
        bar(TARGET - timedelta(days=6), "101"),
        bar(TARGET - timedelta(days=5), "102", is_trading=False),
        bar(TARGET - timedelta(days=4), "103"),
        bar(TARGET - timedelta(days=3), "104"),
        bar(TARGET - timedelta(days=2), "105"),
        bar(TARGET - timedelta(days=1), "106"),
        bar(TARGET, "107"),
    )

    result = evaluate(tuple(reversed(items)))

    assert result.passed is True
    assert result.actual_value["session_count"] == 7
    assert result.actual_value["reference_trading_day"] == "2026-08-19"


def test_rejects_nonpositive_lookback() -> None:
    with pytest.raises(ValueError, match="lookback_trading_sessions"):
        evaluate((bar(TARGET, "10"),), lookback=0)


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
            AdjustmentMethod.HFQ,
        )
