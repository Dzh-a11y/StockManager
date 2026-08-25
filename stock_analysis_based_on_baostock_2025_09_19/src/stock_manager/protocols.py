from datetime import date
from typing import Protocol, Sequence, runtime_checkable

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
    SyncRecord,
)


@runtime_checkable
class ProviderProtocol(Protocol):
    @property
    def source_name(self) -> str: ...

    def fetch_trading_days(self, start: date, end: date) -> Sequence[date]: ...

    def fetch_stocks(self, as_of: date) -> Sequence[StockIdentity]: ...

    def fetch_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]: ...

    def fetch_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]: ...

    def fetch_dividends(
        self, codes: Sequence[str], start: date, end: date
    ) -> Sequence[DividendRecord]: ...


@runtime_checkable
class LocalRepositoryProtocol(Protocol):
    def save_stocks(
        self, stocks: Sequence[StockIdentity], metadata: DatasetMetadata
    ) -> None: ...

    def save_daily_bars(
        self, bars: Sequence[DailyBar], metadata: DatasetMetadata
    ) -> None: ...

    def save_fundamentals(
        self,
        items: Sequence[FundamentalSnapshot],
        metadata: DatasetMetadata,
    ) -> None: ...

    def save_dividends(
        self, items: Sequence[DividendRecord], metadata: DatasetMetadata
    ) -> None: ...

    def save_trading_days(
        self, days: Sequence[date], metadata: DatasetMetadata
    ) -> None: ...

    def save_sync_record(self, record: SyncRecord) -> None: ...

    def save_market_snapshot(
        self,
        stocks: Sequence[StockIdentity],
        bars: Sequence[DailyBar],
        fundamentals: Sequence[FundamentalSnapshot],
        dividends: Sequence[DividendRecord],
        trading_days: Sequence[date],
        metadata: DatasetMetadata,
        success_record: SyncRecord,
    ) -> None: ...

    def save_trading_calendar(
        self,
        days: Sequence[date],
        metadata: DatasetMetadata,
        success_record: SyncRecord,
    ) -> None: ...

    def get_stocks(self, as_of: date) -> Sequence[StockIdentity]: ...

    def get_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]: ...

    def get_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]: ...

    def get_dividends(
        self, codes: Sequence[str], start: date, end: date
    ) -> Sequence[DividendRecord]: ...

    def get_sync_record(
        self, dataset_id: str, trading_day: date
    ) -> SyncRecord | None: ...

    def get_latest_sync_record(self, dataset_id: str) -> SyncRecord | None: ...

    def get_dataset_metadata(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
    ) -> DatasetMetadata | None: ...

    def get_latest_dataset_metadata(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> DatasetMetadata | None: ...

    def get_trading_days(self, start: date, end: date) -> Sequence[date]: ...
