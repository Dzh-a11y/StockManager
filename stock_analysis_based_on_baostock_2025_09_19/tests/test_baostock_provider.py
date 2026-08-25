"""Offline normalization tests for the Baostock adapter."""

from decimal import Decimal

import pytest

from stock_manager.domain import AdjustmentMethod
from stock_manager.providers.baostock_provider import BaostockProvider, BaostockProviderError


def test_adjustment_mapping_is_explicit() -> None:
    assert BaostockProvider._adjustflag(AdjustmentMethod.UNADJUSTED) == "3"
    assert BaostockProvider._adjustflag(AdjustmentMethod.QFQ) == "2"
    assert BaostockProvider._adjustflag(AdjustmentMethod.HFQ) == "1"


def test_suspended_row_is_normalized_with_preclose_and_zero_volume() -> None:
    provider = BaostockProvider(client=object())
    bar = provider._daily_bar(
        {
            "date": "2026-08-25",
            "code": "sh.600000",
            "open": "",
            "high": "",
            "low": "",
            "close": "",
            "preclose": "10.5",
            "volume": "",
            "amount": "",
            "tradestatus": "0",
        }
    )
    assert bar.open == bar.high == bar.low == bar.close == Decimal("10.5")
    assert bar.volume == Decimal("0")
    assert bar.amount == Decimal("0")
    assert bar.is_trading is False


def test_invalid_required_numeric_data_is_explicit_error() -> None:
    provider = BaostockProvider(client=object())
    with pytest.raises(BaostockProviderError, match="invalid decimal"):
        provider._required_decimal("not-a-number", "close")


def test_internal_baostock_queries_are_rate_limited() -> None:
    ticks = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        ticks[0] += seconds

    provider = BaostockProvider(
        client=object(),
        request_interval_seconds=0.5,
        monotonic=lambda: ticks[0],
        sleep=sleep,
    )
    assert provider._query(lambda: "first") == "first"
    assert provider._query(lambda: "second") == "second"
    assert sleeps == [0.5]
