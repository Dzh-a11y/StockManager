"""Unit tests for the P1-4 pure rule engine."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    FundamentalSnapshot,
    RuleResult,
    StockIdentity,
)
from stock_manager.rules import (
    evaluate_composite,
    evaluate_limit_up_3m,
    evaluate_limit_up_breakout,
    evaluate_non_st,
    evaluate_pe_positive,
    evaluate_volatility_multiple,
    evaluate_volume_price_5d,
    load_rules_config,
)


DAY = date(2026, 8, 25)
QFQ_METADATA = DatasetMetadata(
    "daily_bars:sh.600000",
    DAY,
    "fixture",
    datetime(2026, 8, 25, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    AdjustmentMethod.QFQ,
)


def _bar(
    day: int,
    *,
    open_price: str = "100",
    high: str = "102",
    low: str = "98",
    close: str = "100",
    preclose: str = "100",
    volume: str = "1000",
    is_trading: bool = True,
) -> DailyBar:
    return DailyBar(
        "sh.600000",
        date(2026, 8, day),
        Decimal(open_price),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(preclose),
        Decimal(volume),
        Decimal("10000"),
        is_trading,
    )


def _normal_bars() -> tuple[DailyBar, ...]:
    return tuple(_bar(day) for day in range(20, 26))


def _result(rule_id: str, passed: bool) -> RuleResult:
    return RuleResult(rule_id, passed, None, None, "fixture result")


def test_rules_config_is_valid_json_and_requires_explicit_adjustment() -> None:
    config_path = Path(__file__).parents[1] / "config" / "rules.json"
    config = load_rules_config(config_path)
    assert config.technical_adjustment is AdjustmentMethod.QFQ
    assert config.limit_up_3m.minimum_events == 1
    assert config.volume_price_5d.minimum_volume_ratio == Decimal("4")


def test_rules_config_rejects_invalid_document(tmp_path: Path) -> None:
    invalid = tmp_path / "rules.json"
    invalid.write_text('{"metadata":{"version":2}}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_rules_config(invalid)


@pytest.mark.parametrize(
    ("value", "passed"),
    [(Decimal("12.5"), True), (Decimal("0"), False), (Decimal("-1"), False), (None, False)],
)
def test_pe_positive_has_explicit_missing_value_semantics(
    value: Decimal | None, passed: bool
) -> None:
    snapshot = FundamentalSnapshot("sh.600000", DAY, DAY, value, None, "fixture")
    result = evaluate_pe_positive(snapshot, Decimal("0"))
    assert result.passed is passed
    assert result.actual_value == value
    assert result.threshold == {"exclusive_minimum": Decimal("0")}


def test_pe_positive_rejects_missing_snapshot() -> None:
    result = evaluate_pe_positive(None, Decimal("0"))
    assert result.passed is False
    assert result.actual_value is None


def test_non_st_uses_normalized_identity() -> None:
    normal = StockIdentity("sh.600000", "浦发银行", "SSE", False, None, None)
    st = StockIdentity("sh.600001", "示例", "SSE", True, None, None)
    assert evaluate_non_st(normal).passed is True
    assert evaluate_non_st(st).passed is False


def test_volume_price_rule_sorts_and_matches_same_adjacent_pair() -> None:
    bars = list(_normal_bars())
    bars[-2] = _bar(24, close="100", volume="1000")
    bars[-1] = _bar(25, open_price="105", high="108", low="104", close="107", preclose="100", volume="4000")
    result = evaluate_volume_price_5d(
        tuple(reversed(bars)), QFQ_METADATA, 5, Decimal("4"), Decimal("7"), AdjustmentMethod.QFQ
    )
    assert result.passed is True
    assert result.actual_value[0]["trading_day"] == "2026-08-25"


def test_volume_price_rule_skips_zero_previous_volume() -> None:
    bars = (_bar(24, volume="0"), _bar(25, close="110", preclose="100", volume="4000"))
    result = evaluate_volume_price_5d(
        bars, QFQ_METADATA, 5, Decimal("4"), Decimal("7"), AdjustmentMethod.QFQ
    )
    assert result.passed is False


def test_limit_up_breakout_matches_configured_legacy_amount() -> None:
    bars = list(_normal_bars())
    bars[-2] = _bar(24, close="101", preclose="100")
    bars[-1] = _bar(25, open_price="101", high="111", low="100", close="108", preclose="101")
    result = evaluate_limit_up_breakout(
        bars,
        QFQ_METADATA,
        5,
        90,
        Decimal("1.08"),
        Decimal("1.12"),
        Decimal("0.03"),
        AdjustmentMethod.QFQ,
    )
    assert result.passed is True
    assert result.actual_value[0]["kind"] == "limit_up_break"


def test_limit_up_breakout_also_matches_fake_negative_line() -> None:
    bars = list(_normal_bars())
    bars[-2] = _bar(24, close="100")
    bars[-1] = _bar(25, open_price="103", high="104", low="100", close="101", preclose="100")
    result = evaluate_limit_up_breakout(
        bars,
        QFQ_METADATA,
        5,
        90,
        Decimal("1.08"),
        Decimal("1.12"),
        Decimal("0.03"),
        AdjustmentMethod.QFQ,
    )
    assert result.passed is True
    assert result.actual_value[0]["kind"] == "fake_negative_line"


def test_limit_up_3m_requires_at_least_one_event_and_allows_maximum() -> None:
    no_events = evaluate_limit_up_3m(
        _normal_bars(), QFQ_METADATA, 90, 1, 3, Decimal("1.08"), Decimal("1.12"), AdjustmentMethod.QFQ
    )
    event_bars = _normal_bars() + (
        _bar(26, close="110", preclose="100"),
    )
    event_metadata = DatasetMetadata(
        "daily_bars:sh.600000",
        date(2026, 8, 26),
        "fixture",
        QFQ_METADATA.synced_at,
        AdjustmentMethod.QFQ,
    )
    one_event = evaluate_limit_up_3m(
        event_bars, event_metadata, 90, 1, 3, Decimal("1.08"), Decimal("1.12"), AdjustmentMethod.QFQ
    )
    assert no_events.passed is False
    assert one_event.passed is True


def test_volatility_rule_has_inclusive_maximum_and_minimum_sample() -> None:
    exact = tuple(_bar(day, high="100", low="50") for day in range(20, 25))
    assert evaluate_volatility_multiple(
        exact, QFQ_METADATA, 180, Decimal("2"), 5, AdjustmentMethod.QFQ
    ).passed is True
    assert evaluate_volatility_multiple(
        exact[:4], QFQ_METADATA, 180, Decimal("2"), 5, AdjustmentMethod.QFQ
    ).passed is False


def test_volatility_rule_rejects_nonpositive_low() -> None:
    bars = tuple(_bar(day, low="0") for day in range(20, 25))
    with pytest.raises(ValueError, match="low prices"):
        evaluate_volatility_multiple(
            bars, QFQ_METADATA, 180, Decimal("2"), 5, AdjustmentMethod.QFQ
        )


def test_technical_rules_reject_adjustment_mismatch() -> None:
    with pytest.raises(ValueError, match="adjustment mismatch"):
        evaluate_volume_price_5d(
            _normal_bars(), QFQ_METADATA, 5, Decimal("4"), Decimal("7"), AdjustmentMethod.UNADJUSTED
        )


def test_nontrading_rows_do_not_count_as_trading_sessions() -> None:
    bars = (_bar(20), _bar(21, is_trading=False), _bar(22), _bar(23), _bar(24), _bar(25))
    result = evaluate_volatility_multiple(
        bars, QFQ_METADATA, 180, Decimal("2"), 5, AdjustmentMethod.QFQ
    )
    assert result.passed is True
    assert len([bar for bar in bars if bar.is_trading]) == 5


def test_composite_requires_all_fundamentals_one_trigger_and_required_technical() -> None:
    results = [
        _result("pe_positive", True),
        _result("non_st", True),
        _result("volume_price_5d", False),
        _result("limit_up_breakout", True),
        _result("limit_up_3m", True),
        _result("volatility_multiple", True),
    ]
    assert evaluate_composite(results).passed is True
    results[-1] = _result("volatility_multiple", False)
    assert evaluate_composite(results).passed is False


def test_composite_rejects_missing_or_duplicate_results() -> None:
    with pytest.raises(ValueError, match="missing required"):
        evaluate_composite((_result("pe_positive", True),))
    with pytest.raises(ValueError, match="unique"):
        evaluate_composite((_result("pe_positive", True), _result("pe_positive", True)))
