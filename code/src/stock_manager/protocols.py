from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence, runtime_checkable

from stock_manager.domain import (
    AdjustmentMethod,
    ActiveGeneration,
    CandidateGeneration,
    CandidateGenerationStatus,
    CoverageVerification,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    GenerationPartition,
    IngestBatch,
    PublishedGeneration,
    StockIdentity,
    SyncPlan,
    SyncRecord,
    SyncTask,
    SyncTaskStatus,
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

    def mark_chunk_complete(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        chunk_index: int,
        codes: Sequence[str],
    ) -> None: ...

    def completed_chunk_codes(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
    ) -> dict[int, tuple[str, ...]]: ...

    def latest_backfill_cover_date(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> date | None: ...

    def daily_bar_days(
        self,
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> set[date]: ...

    def daily_bar_stock_counts(
        self,
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> dict[date, int]: ...

    def earliest_failed_day(
        self,
        dataset_id: str,
        start: date,
        end: date,
    ) -> date | None: ...

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

    def list_dataset_metadata(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        *,
        limit: int = 10,
    ) -> Sequence[DatasetMetadata]: ...

    def register_snapshot_day(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        *,
        source: str,
        synced_at: datetime,
    ) -> bool: ...

    def get_trading_days(self, start: date, end: date) -> Sequence[date]: ...

    def prune_before(self, cutoff: date) -> None: ...


@runtime_checkable
class DataSyncAdminRepositoryProtocol(Protocol):
    """Bookkeeping surface for the P5 DataSync reconstruction (P5-RD-1).

    Implemented by ``SQLiteRepository``; isolates plan/task/candidate/batch/
    verification/generation persistence from the rest of the repository so
    tests can swap in a fake without SQL.
    """

    # -- sync plans ----------------------------------------------------------
    def save_sync_plan(self, plan: SyncPlan) -> None: ...

    def get_sync_plan(self, plan_id: str) -> SyncPlan | None: ...

    def list_sync_plans(self, dataset_id: str, adjustment: AdjustmentMethod) -> Sequence[SyncPlan]: ...

    def update_sync_plan_status(
        self, plan_id: str, status: object, updated_at: datetime
    ) -> None: ...

    # -- sync tasks ----------------------------------------------------------
    def save_sync_task(self, task: SyncTask) -> None: ...

    def get_sync_task(self, task_id: str) -> SyncTask | None: ...

    def list_sync_tasks(self, plan_id: str) -> Sequence[SyncTask]: ...

    def update_sync_task_status(self, task: SyncTask) -> None: ...

    def tasks_by_status(self, plan_id: str, statuses: Sequence[object]) -> Sequence[SyncTask]: ...

    # -- candidate generations ----------------------------------------------
    def save_candidate_generation(self, candidate: CandidateGeneration) -> None: ...

    def get_candidate_generation(
        self, candidate_generation_id: str
    ) -> CandidateGeneration | None: ...

    def update_candidate_status(
        self,
        candidate_generation_id: str,
        status: CandidateGenerationStatus,
        updated_at: datetime,
    ) -> None: ...

    # -- ingest batches ------------------------------------------------------
    def save_ingest_batch(self, batch: IngestBatch) -> None: ...

    def get_ingest_batch(self, batch_id: str) -> IngestBatch | None: ...

    def list_ingest_batches(
        self, candidate_generation_id: str
    ) -> Sequence[IngestBatch]: ...

    # -- coverage verifications ----------------------------------------------
    def save_coverage_verification(self, verification: CoverageVerification) -> None: ...

    def list_coverage_verifications(
        self, candidate_generation_id: str
    ) -> Sequence[CoverageVerification]: ...

    # -- generation partitions -----------------------------------------------
    def save_generation_partition(self, partition: GenerationPartition) -> None: ...

    def list_generation_partitions(self, generation: str) -> Sequence[GenerationPartition]: ...

    # -- published generations / active pointer ------------------------------
    def save_published_generation(self, published: PublishedGeneration) -> None: ...

    def get_latest_published_generation(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> PublishedGeneration | None: ...

    def save_active_generation(self, active: ActiveGeneration) -> None: ...

    def get_active_generation(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> ActiveGeneration | None: ...
