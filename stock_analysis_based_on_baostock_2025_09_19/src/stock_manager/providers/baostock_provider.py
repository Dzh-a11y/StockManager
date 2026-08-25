"""Baostock adapter that normalizes remote rows into domain objects."""

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    FundamentalSnapshot,
    StockIdentity,
)


# Baostock's query_all_stock returns every listed security — stocks, indices,
# ETFs, funds and bonds. These exchange code prefixes identify the real A-share
# stocks; everything else is dropped in fetch_stocks() so screening never sees
# indices or funds. B-shares (sh.900xxx, sz.200xxx) and BSE (北交所, bj.*) are
# intentionally excluded.
_ASHARE_STOCK_PREFIXES: dict[str, tuple[str, ...]] = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301", "302"),
}


def _is_ashare_stock(code: str) -> bool:
    """Return whether a baostock security code belongs to an A-share stock."""
    exchange, separator, number = code.partition(".")
    if not separator:
        return False
    return number.startswith(_ASHARE_STOCK_PREFIXES.get(exchange.lower(), ()))


class BaostockProviderError(RuntimeError):
    """Raised when Baostock rejects a request or returns malformed data."""


class BaostockProvider:
    """Thin, replaceable adapter around the Baostock SDK."""

    def __init__(
        self,
        client: ModuleType | Any | None = None,
        *,
        request_interval_seconds: float = 0.2,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must be non-negative")
        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        if client is None:
            import baostock as client_module

            client = client_module
        self._client = client
        self._request_interval = request_interval_seconds
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._progress_callback = progress_callback

    def _emit_progress(
        self, phase: str, index: int, total: int, code: str
    ) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback(
            {
                "phase": phase,
                "index": index,
                "total": total,
                "current_code": code,
            }
        )

    @property
    def source_name(self) -> str:
        return "baostock"

    def _retry(self, operation: Callable[[], Any]) -> Any:
        """Run a baostock SDK call, retrying transient failures with backoff.

        Transient means a network-level ``OSError`` or a result whose
        ``error_code`` is non-zero (server busy, connection reset, rate limit).
        Parsing errors raised after a successful request are permanent and are
        not retried. On exhaustion, the last failure is re-raised (for network
        errors) or the last bad result is returned so the caller's ``_rows``
        raises the usual operation-specific error.
        """
        last_result: Any = None
        last_network_error: OSError | None = None
        for attempt in range(self._max_retries):
            try:
                result = operation()
            except OSError as error:
                last_result = None
                last_network_error = error
            else:
                last_result = result
                last_network_error = None
                if getattr(result, "error_code", "0") == "0":
                    return result
            if attempt + 1 < self._max_retries:
                self._sleep(self._retry_backoff * (2**attempt))
        if last_network_error is not None:
            raise last_network_error
        return last_result

    def _query(self, operation: Callable[[], Any]) -> Any:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self._request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        result = self._retry(operation)
        self._last_request_at = self._monotonic()
        return result

    @contextmanager
    def _session(self) -> Iterator[None]:
        login_result = self._retry(lambda: self._client.login())
        if login_result.error_code != "0":
            raise BaostockProviderError(f"Baostock login failed: {login_result.error_msg}")
        try:
            yield
        finally:
            self._client.logout()

    @staticmethod
    def _rows(result: Any, operation: str) -> tuple[dict[str, str], ...]:
        if result.error_code != "0":
            raise BaostockProviderError(f"{operation} failed: {result.error_msg}")
        rows: list[dict[str, str]] = []
        fields = tuple(result.fields)
        while result.next():
            values = result.get_row_data()
            rows.append(dict(zip(fields, values, strict=True)))
        return tuple(rows)

    @staticmethod
    def _decimal(value: str, field: str, *, optional: bool = False) -> Decimal | None:
        if optional and value == "":
            return None
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise BaostockProviderError(f"invalid decimal in {field}: {value!r}") from error

    @staticmethod
    def _adjustflag(adjustment: AdjustmentMethod) -> str:
        mapping = {
            AdjustmentMethod.UNADJUSTED: "3",
            AdjustmentMethod.QFQ: "2",
            AdjustmentMethod.HFQ: "1",
        }
        return mapping[adjustment]

    def fetch_trading_days(self, start: date, end: date) -> Sequence[date]:
        with self._session():
            rows = self._rows(
                self._query(
                    lambda: self._client.query_trade_dates(
                        start_date=start.isoformat(), end_date=end.isoformat()
                    )
                ),
                "query_trade_dates",
            )
        return tuple(date.fromisoformat(row["calendar_date"]) for row in rows if row["is_trading_day"] == "1")

    def fetch_stocks(self, as_of: date) -> Sequence[StockIdentity]:
        """Return the A-share stock universe for ``as_of``.

        Baostock's ``query_all_stock`` lists every listed security; only real
        A-share stocks (by code prefix) are exposed here.
        """
        with self._session():
            rows = self._rows(
                self._query(lambda: self._client.query_all_stock(day=as_of.isoformat())),
                "query_all_stock",
            )
        return tuple(
            StockIdentity(
                row["code"],
                row["code_name"],
                {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}.get(
                    row["code"].split(".", maxsplit=1)[0].lower(), "UNKNOWN"
                ),
                row["code_name"].upper().startswith(("ST", "*ST")),
                None,
                None,
            )
            for row in rows
            if row.get("tradeStatus", "1") in {"0", "1"}
            and _is_ashare_stock(row["code"])
        )

    def fetch_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]:
        bars: list[DailyBar] = []
        fields = "date,code,open,high,low,close,preclose,volume,amount,tradestatus"
        with self._session():
            for index, code in enumerate(codes):
                self._emit_progress("daily_bars", index + 1, len(codes), code)
                rows = self._rows(
                    self._query(
                        lambda code=code: self._client.query_history_k_data_plus(
                            code,
                            fields,
                            start_date=start.isoformat(),
                            end_date=end.isoformat(),
                            frequency="d",
                            adjustflag=self._adjustflag(adjustment),
                        )
                    ),
                    f"query_history_k_data_plus({code})",
                )
                for row in rows:
                    bars.append(self._daily_bar(row))
        return tuple(bars)

    def _daily_bar(self, row: dict[str, str]) -> DailyBar:
        is_trading = row["tradestatus"] == "1"
        preclose = self._required_decimal(row["preclose"], "preclose")

        def price(field: str) -> Decimal:
            value = row[field]
            return preclose if not is_trading and value == "" else self._required_decimal(value, field)

        volume = "0" if not is_trading and row["volume"] == "" else row["volume"]
        amount = "0" if not is_trading and row["amount"] == "" else row["amount"]
        return DailyBar(
            row["code"],
            date.fromisoformat(row["date"]),
            price("open"),
            price("high"),
            price("low"),
            price("close"),
            preclose,
            self._required_decimal(volume, "volume"),
            self._required_decimal(amount, "amount"),
            is_trading,
        )

    def _required_decimal(self, value: str, field: str) -> Decimal:
        parsed = self._decimal(value, field)
        if parsed is None:
            raise BaostockProviderError(f"missing required decimal in {field}")
        return parsed

    def fetch_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]:
        snapshots: list[FundamentalSnapshot] = []
        fields = "date,code,peTTM,pbMRQ"
        with self._session():
            for index, code in enumerate(codes):
                self._emit_progress("fundamentals", index + 1, len(codes), code)
                rows = self._rows(
                    self._query(
                        lambda code=code: self._client.query_history_k_data_plus(
                            code,
                            fields,
                            start_date=as_of.isoformat(),
                            end_date=as_of.isoformat(),
                            frequency="d",
                            adjustflag="3",
                        )
                    ),
                    f"query_fundamentals({code})",
                )
                for row in rows:
                    snapshots.append(
                        FundamentalSnapshot(
                            row["code"],
                            date.fromisoformat(row["date"]),
                            date.fromisoformat(row["date"]),
                            self._decimal(row["peTTM"], "peTTM", optional=True),
                            self._decimal(row["pbMRQ"], "pbMRQ", optional=True),
                            self.source_name,
                        )
                    )
        return tuple(snapshots)
