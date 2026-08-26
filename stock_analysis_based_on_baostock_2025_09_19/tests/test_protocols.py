from datetime import date
from typing import Sequence

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
    SyncRecord,
)
from stock_manager.protocols import LocalRepositoryProtocol, ProviderProtocol


class EmptyProvider:
    @property
    def source_name(self) -> str:
        return "fixture"

    def fetch_trading_days(self, start: date, end: date) -> Sequence[date]:
        return ()

    def fetch_stocks(self, as_of: date) -> Sequence[StockIdentity]:
        return ()

    def fetch_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]:
        return ()

    def fetch_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]:
        return ()


class EmptyRepository:
    def save_stocks(self, stocks: Sequence[StockIdentity], metadata: DatasetMetadata) -> None: ...
    def save_daily_bars(self, bars: Sequence[DailyBar], metadata: DatasetMetadata) -> None: ...
    def save_fundamentals(self, items: Sequence[FundamentalSnapshot], metadata: DatasetMetadata) -> None: ...
    def save_dividends(self, items: Sequence[DividendRecord], metadata: DatasetMetadata) -> None: ...
    def save_trading_days(self, days: Sequence[date], metadata: DatasetMetadata) -> None: ...
    def save_sync_record(self, record: SyncRecord) -> None: ...
    def save_market_snapshot(self, stocks: Sequence[StockIdentity], bars: Sequence[DailyBar], fundamentals: Sequence[FundamentalSnapshot], dividends: Sequence[DividendRecord], trading_days: Sequence[date], metadata: DatasetMetadata, success_record: SyncRecord) -> None: ...
    def save_trading_calendar(self, days: Sequence[date], metadata: DatasetMetadata, success_record: SyncRecord) -> None: ...
    def mark_chunk_complete(self, dataset_id: str, trading_day: date, adjustment: AdjustmentMethod, chunk_index: int, codes: Sequence[str]) -> None: ...
    def completed_chunk_codes(self, dataset_id: str, trading_day: date, adjustment: AdjustmentMethod) -> dict[int, tuple[str, ...]]: return {}
    def get_stocks(self, as_of: date) -> Sequence[StockIdentity]: return ()
    def get_daily_bars(self, codes: Sequence[str], start: date, end: date, adjustment: AdjustmentMethod) -> Sequence[DailyBar]: return ()
    def get_fundamentals(self, codes: Sequence[str], as_of: date) -> Sequence[FundamentalSnapshot]: return ()
    def get_dividends(self, codes: Sequence[str], start: date, end: date) -> Sequence[DividendRecord]: return ()
    def get_sync_record(self, dataset_id: str, trading_day: date) -> SyncRecord | None: return None
    def get_latest_sync_record(self, dataset_id: str) -> SyncRecord | None: return None
    def get_dataset_metadata(self, dataset_id: str, trading_day: date, adjustment: AdjustmentMethod) -> DatasetMetadata | None: return None
    def get_latest_dataset_metadata(self, dataset_id: str, adjustment: AdjustmentMethod) -> DatasetMetadata | None: return None
    def get_trading_days(self, start: date, end: date) -> Sequence[date]: return ()
    def prune_before(self, cutoff: date) -> None: ...


def test_runtime_protocol_conformance() -> None:
    assert isinstance(EmptyProvider(), ProviderProtocol)
    assert isinstance(EmptyRepository(), LocalRepositoryProtocol)
