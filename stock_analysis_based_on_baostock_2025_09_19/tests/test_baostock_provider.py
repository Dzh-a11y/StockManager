"""Offline normalization tests for the Baostock adapter."""

from __future__ import annotations

import socket
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
    with pytest.raises(ValueError, match="socket_timeout_seconds"):
        BaostockProvider(client=object(), socket_timeout_seconds=0)


def test_provider_applies_socket_timeout_around_sdk_calls() -> None:
    observed: list[float | None] = []

    def operation() -> Any:
        observed.append(socket.getdefaulttimeout())
        return "ok"

    provider = BaostockProvider(
        client=object(),
        socket_timeout_seconds=30.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    assert provider._query(operation) == "ok"
    # 调用期间进程级默认超时被临时设为 30 秒,结束后恢复原值。
    assert observed == [30.0]
    assert socket.getdefaulttimeout() is None


def test_provider_retries_then_reraises_socket_timeout() -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    def operation() -> Any:
        attempts["count"] += 1
        raise socket.timeout("baostock stalled")

    provider = BaostockProvider(
        client=object(),
        max_retries=2,
        retry_backoff_seconds=0.5,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: sleeps.append(seconds),
    )
    with pytest.raises(socket.timeout):
        provider._query(operation)
    assert attempts["count"] == 2
    assert sleeps == [0.5]


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



class _ExpiredSessionClient:
    """Fake client whose first query fails with an expired session."""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self.login_count = 0
        self.logout_count = 0
        self._query_calls = 0

    def login(self) -> _FakeLoginResult:
        self.login_count += 1
        return _FakeLoginResult()

    def logout(self) -> None:
        self.logout_count += 1

    def query_all_stock(self, day: str = "") -> _FakeQueryResult:
        self._query_calls += 1
        if self._query_calls == 1:
            result = _FakeQueryResult([])
            result.error_code = "-1"
            result.error_msg = "用户未登录"
            return result
        return _FakeQueryResult(self._rows)


def test_session_expiry_recovers_via_relogin() -> None:
    """查询报"用户未登录"时自动重新登录并重试,同步不再失败。"""
    rows = [["sh.600000", "1", "浦发银行"]]
    client = _ExpiredSessionClient(rows)
    provider = BaostockProvider(client=client, max_retries=3, request_interval_seconds=0)
    stocks = provider.fetch_stocks(date(2026, 8, 25))
    assert len(stocks) == 1
    assert stocks[0].code == "sh.600000"
    assert client.login_count == 2  # 首次登录 + 会话恢复重登录
    assert client.logout_count == 1


def test_session_expiry_exhausted_still_fails_explicitly() -> None:
    """会话持续失效且重登录后仍失败 → 明确业务错误,不吞。"""
    rows = [["sh.600000", "1", "浦发银行"]]
    client = _ExpiredSessionClient(rows)

    class _AlwaysExpired(_ExpiredSessionClient):
        def query_all_stock(self, day: str = "") -> _FakeQueryResult:
            result = _FakeQueryResult([])
            result.error_code = "-1"
            result.error_msg = "用户未登录"
            return result

    client = _AlwaysExpired(rows)
    provider = BaostockProvider(client=client, max_retries=3, request_interval_seconds=0)
    with pytest.raises(BaostockProviderError, match="query_all_stock failed"):
        provider.fetch_stocks(date(2026, 8, 25))


def test_is_session_expired_detection() -> None:
    expired = _FakeQueryResult([])
    expired.error_code = "-1"
    expired.error_msg = "用户未登录"
    assert BaostockProvider._is_session_expired(expired)
    other = _FakeQueryResult([])
    other.error_code = "-1"
    other.error_msg = "系统繁忙"
    assert not BaostockProvider._is_session_expired(other)
    ok = _FakeQueryResult([])
    assert not BaostockProvider._is_session_expired(ok)



class _FakeBasicResult(_FakeQueryResult):
    """query_stock_basic result with ipoDate/outDate + pagination fields."""

    def __init__(self, rows: list[list[str]], page_count: int = 1) -> None:
        self.error_code = "0"
        self.error_msg = "ok"
        self.fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        self._rows = list(rows)
        self._index = 0
        self._current: list[str] | None = None
        self.cur_page_num = "1"
        self.page_count = str(page_count)


class _FakeClientWithBasics(_FakeClient):
    def __init__(self, rows: list[list[str]], basics: list[list[str]]) -> None:
        super().__init__(rows)
        self._basics = basics
        self.basic_calls = 0

    def query_stock_basic(self, code: str = "", code_name: str = "") -> _FakeBasicResult:
        self.basic_calls += 1
        return _FakeBasicResult(self._basics)


def test_fetch_stock_basics_parses_listing_dates() -> None:
    client = _FakeClientWithBasics(
        rows=[],
        basics=[
            ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
            ["sz.000001", "平安银行", "1991-04-03", "2020-01-01", "1", "0"],
            ["sh.510300", "沪深300ETF", "2012-05-28", "", "2", "1"],
        ],
    )
    provider = BaostockProvider(client=client, request_interval_seconds=0)
    basics = provider.fetch_stock_basics(session=False)
    assert basics["sh.600000"] == (date(1999, 11, 10), None)
    assert basics["sz.000001"] == (date(1991, 4, 3), date(2020, 1, 1))
    # 非 A 股(ETF 前缀)被过滤
    assert "sh.510300" not in basics


def test_fetch_stocks_merges_listing_dates() -> None:
    client = _FakeClientWithBasics(
        rows=[
            ["sh.600000", "1", "浦发银行"],
            ["sz.000001", "1", "平安银行"],
        ],
        basics=[
            ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
            ["sz.000001", "平安银行", "1991-04-03", "", "1", "1"],
        ],
    )
    provider = BaostockProvider(client=client, request_interval_seconds=0)
    stocks = provider.fetch_stocks(date(2026, 8, 25))
    by_code = {s.code: s for s in stocks}
    assert by_code["sh.600000"].listed_on == date(1999, 11, 10)
    assert by_code["sh.600000"].delisted_on is None
    assert by_code["sz.000001"].listed_on == date(1991, 4, 3)


def test_fetch_stocks_tolerates_missing_basics() -> None:
    # 无 query_stock_basic 的旧 fake:listing 日期保持未知,不崩溃。
    client = _FakeClient(
        rows=[["sh.600000", "1", "浦发银行"]],
    )
    provider = BaostockProvider(client=client, request_interval_seconds=0)
    stocks = provider.fetch_stocks(date(2026, 8, 25))
    assert stocks[0].listed_on is None
    assert stocks[0].delisted_on is None
