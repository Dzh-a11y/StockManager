"""Offline provider used by unit and integration tests."""

import threading
from collections import Counter
from collections.abc import Sequence
from datetime import date

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
)


class FixtureProvider:
    """Return caller-owned immutable fixture data and record every provider call."""

    def __init__(
        self,
        *,
        trading_days: Sequence[date] = (),
        stocks: Sequence[StockIdentity] = (),
        bars: Sequence[DailyBar] = (),
        fundamentals: Sequence[FundamentalSnapshot] = (),
        dividends: Sequence[DividendRecord] = (),
        fail_method: str | None = None,
    ) -> None:
        self._trading_days = tuple(trading_days)
        self._stocks = tuple(stocks)
        self._bars = tuple(bars)
        self._fundamentals = tuple(fundamentals)
        self._dividends = tuple(dividends)
        self._fail_method = fail_method
        self._calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    @property
    def source_name(self) -> str:
        return "fixture"

    @property
    def calls(self) -> dict[str, int]:
        with self._lock:
            return dict(self._calls)

    def set_failure(self, method: str | None) -> None:
        """Change deterministic failure injection between explicit test attempts."""
        self._fail_method = method

    def _record(self, method: str) -> None:
        with self._lock:
            self._calls[method] += 1
        if self._fail_method == method:
            raise RuntimeError(f"fixture failure in {method}")

    def fetch_trading_days(self, start: date, end: date) -> Sequence[date]:
        self._record("fetch_trading_days")
        return tuple(day for day in self._trading_days if start <= day <= end)

    def fetch_stocks(self, as_of: date) -> Sequence[StockIdentity]:
        self._record("fetch_stocks")
        return self._stocks

    def fetch_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]:
        self._record("fetch_daily_bars")
        selected = set(codes)
        return tuple(
            bar
            for bar in self._bars
            if bar.code in selected and start <= bar.trading_day <= end
        )

    def fetch_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]:
        self._record("fetch_fundamentals")
        selected = set(codes)
        return tuple(
            item for item in self._fundamentals if item.code in selected and item.published_on <= as_of
        )
