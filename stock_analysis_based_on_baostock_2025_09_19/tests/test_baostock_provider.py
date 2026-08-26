"""Offline normalization tests for the Baostock adapter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from stock_manager.domain import AdjustmentMethod
from stock_manager.providers.baostock_provider import BaostockProvider, BaostockProviderError


class _FakeLoginResult:
    error_code = "0"
    error_msg = "ok"


class _FakeQueryResult:
    def __init__(self, rows: list[list[str]]) -> None:
        self.error_code = "0"
        self.error_msg = "ok"
        self.fields = ["code", "tradeStatus", "code_name"]
        self._rows = list(rows)
        self._index = 0
        self._current: list[str] | None = None

    def next(self) -> bool:
        if self._index < len(self._rows):
            self._current = self._rows[self._index]
            self._index += 1
            return True
        return False

    def get_row_data(self) -> list[str]:
        assert self._current is not None
        return self._current


class _FakeClient:
    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows

    def login(self) -> _FakeLoginResult:
        return _FakeLoginResult()

    def logout(self) -> None:
        return None

    def query_all_stock(self, day: str = "") -> _FakeQueryResult:
        return _FakeQueryResult(self._rows)


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


class _ErrorResult:
    def __init__(self, error_code: str, error_msg: str) -> None:
        self.error_code = error_code
        self.error_msg = error_msg


def test_retry_config_is_validated() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        BaostockProvider(client=object(), max_retries=0)
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        BaostockProvider(client=object(), retry_backoff_seconds=-1)


def test_query_retries_transient_server_errors_with_backoff() -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    def operation() -> Any:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return _ErrorResult("10001", "server busy")
        return "ok"

    provider = BaostockProvider(
        client=object(),
        max_retries=3,
        retry_backoff_seconds=0.5,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: sleeps.append(seconds),
    )
    assert provider._query(operation) == "ok"
    assert attempts["count"] == 3
    assert sleeps == [0.5, 1.0]


def test_query_gives_up_after_max_retries() -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    def operation() -> Any:
        attempts["count"] += 1
        return _ErrorResult("10001", "server busy")

    provider = BaostockProvider(
        client=object(),
        max_retries=3,
        retry_backoff_seconds=0.5,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: sleeps.append(seconds),
    )
    result = provider._query(operation)
    assert result.error_code == "10001"
    assert attempts["count"] == 3
    assert sleeps == [0.5, 1.0]


def test_query_retries_network_errors() -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    def operation() -> Any:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise OSError("connection reset")
        return "ok"

    provider = BaostockProvider(
        client=object(),
        max_retries=3,
        retry_backoff_seconds=0.5,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: sleeps.append(seconds),
    )
    assert provider._query(operation) == "ok"
    assert attempts["count"] == 3
    assert sleeps == [0.5, 1.0]


def test_query_raises_when_network_errors_exhausted() -> None:
    def operation() -> Any:
        raise OSError("connection refused")

    provider = BaostockProvider(
        client=object(),
        max_retries=2,
        retry_backoff_seconds=0.1,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    with pytest.raises(OSError, match="connection refused"):
        provider._query(operation)


def test_fetch_stocks_keeps_only_ashare_stocks() -> None:
    client = _FakeClient(
        [
            ["sh.000001", "1", "上证综合指数"],  # index
            ["sh.510050", "1", "华夏上证50ETF"],  # ETF
            ["sh.600000", "1", "浦发银行"],  # stock
            ["sh.688001", "1", "华兴源创"],  # STAR stock
            ["sz.399001", "1", "深证成指"],  # index
            ["sz.159915", "1", "创业板ETF"],  # ETF
            ["sz.000001", "1", "平安银行"],  # stock
            ["sz.300750", "1", "宁德时代"],  # ChiNext stock
            ["bj.430047", "1", "诺思兰德"],  # BSE stock (excluded)
            ["sz.000002", "0", "万科A"],  # suspended stock still kept
        ]
    )
    stocks = BaostockProvider(client=client).fetch_stocks(date(2026, 8, 25))
    assert [stock.code for stock in stocks] == [
        "sh.600000",
        "sh.688001",
        "sz.000001",
        "sz.300750",
        "sz.000002",
    ]
