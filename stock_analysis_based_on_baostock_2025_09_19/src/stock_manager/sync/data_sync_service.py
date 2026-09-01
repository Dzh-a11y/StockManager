"""Single-entry, idempotent synchronization into the local repository."""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    BackfillChunkV2,
    BackfillRunStatus,
    BackfillRunV2,
    DailyBar,
    DataCoverageStatus,
    DatasetCoverage,
    DatasetMetadata,
    DatasetVersion,
    DatasetVersionStatus,
    FundamentalSnapshot,
    ProviderSmokeOutcome,
    StockIdentity,
    SyncOutcome,
    SyncRecord,
    SyncStatus,
)
from stock_manager.protocols import LocalRepositoryProtocol, ProviderProtocol
from stock_manager.sync.history_plan import (
    TRADING_DAYS_PER_YEAR,
    CoveragePlan,
    plan_coverage,
    trading_day_lookback,
)
from stock_manager.sync.locks import dataset_lock_path, persistent_file_lock, process_lock


SHANGHAI = ZoneInfo("Asia/Shanghai")
T = TypeVar("T")


class RetryRequiredError(RuntimeError):
    """Raised when a failed synchronization is retried without explicit consent."""


class CooldownActiveError(RuntimeError):
    """Raised when an explicit retry occurs before its cooldown expires."""


class SyncFailedError(RuntimeError):
    """Raised after a provider failure has been persisted as FAILED."""


@dataclass(frozen=True, slots=True)
class SyncHistoryConfig:
    """Eight-year history window (config v2) with a fixed coverage policy."""

    target_years: int
    coverage_policy: str = "latest_completed_trading_day"

    def __post_init__(self) -> None:
        if self.target_years <= 0:
            raise ValueError("target_years must be positive")
        if self.coverage_policy != "latest_completed_trading_day":
            raise ValueError(
                f"unsupported coverage policy: {self.coverage_policy}"
            )


@dataclass(frozen=True, slots=True)
class SyncConfig:
    cutoff_time: wall_time
    retry_cooldown: timedelta
    minimum_request_interval_seconds: float
    calendar_horizon_days: int
    dividend_lookback_years: int
    retention_days: int = 360
    history: SyncHistoryConfig | None = None

    def __post_init__(self) -> None:
        if self.cutoff_time.tzinfo is not None:
            raise ValueError("cutoff_time must be a local wall-clock time without tzinfo")
        if self.retry_cooldown < timedelta(0):
            raise ValueError("retry_cooldown must be non-negative")
        if self.minimum_request_interval_seconds < 0:
            raise ValueError("minimum_request_interval_seconds must be non-negative")
        if self.calendar_horizon_days <= 0:
            raise ValueError("calendar_horizon_days must be positive")
        if self.dividend_lookback_years <= 0:
            raise ValueError("dividend_lookback_years must be positive")
        if self.retention_days <= 0:
            raise ValueError("retention_days must be positive")


def latest_completed_trading_day(
    now: datetime,
    trading_days: Sequence[date],
    cutoff_time: wall_time,
) -> date:
    """Resolve the newest completed A-share trading day from a supplied calendar."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(SHANGHAI)
    eligible = [
        day
        for day in trading_days
        if day < local_now.date()
        or (day == local_now.date() and local_now.time().replace(tzinfo=None) >= cutoff_time)
    ]
    if not eligible:
        raise ValueError("no completed trading day is available in the local calendar")
    return max(eligible)


class DataSyncService:
    """The only service authorized to call a market-data Provider."""

    def __init__(
        self,
        provider: ProviderProtocol,
        repository: LocalRepositoryProtocol,
        lock_directory: Path,
        config: SyncConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._lock_directory = lock_directory
        self._config = config
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._progress = progress
        provider_key = f"{lock_directory.resolve()}:{provider.source_name}:provider"
        self._provider_process_lock = process_lock(provider_key)
        self._provider_file_lock = lock_directory / f"{provider.source_name}.provider.lock"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sync clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    def _provider_call(self, operation: Callable[[], T]) -> T:
        """Invoke the provider; request pacing is the provider's own job."""
        return operation()

    def _emit_progress(
        self, phase: str, completed: int, total: int, current_code: str
    ) -> None:
        """Report overall progress through the optional service-level callback."""
        if self._progress is None:
            return
        self._progress(
            {
                "phase": phase,
                "completed": completed,
                "total": total,
                "current_code": current_code,
            }
        )

    def _emit_v2_progress(
        self,
        run_id: str,
        phase: str,
        completed: int,
        total: int,
        current_code: str,
    ) -> None:
        """Emit v2 backfill progress and persist the in-batch state."""
        self._emit_progress(phase, completed, total, current_code)
        self._repository.update_backfill_batch_progress(
            run_id,
            phase=phase,
            completed=completed,
            total=total,
            current_code=current_code,
        )

    def smoke_test_provider(
        self,
        code: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
    ) -> ProviderSmokeOutcome:
        """Exercise every Provider endpoint for one stock without persisting market data."""
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError("code must not be empty")
        with self._provider_process_lock, persistent_file_lock(self._provider_file_lock):
            trading_days = self._provider_call(
                lambda: self._provider.fetch_trading_days(trading_day, trading_day)
            )
            self._validate_calendar(trading_days, trading_day, trading_day)
            if trading_day not in trading_days:
                raise ValueError(f"{trading_day.isoformat()} is not an A-share trading day")
            stocks = self._provider_call(
                lambda: self._provider.fetch_stocks(trading_day)
            )
            if normalized_code not in {stock.code for stock in stocks}:
                raise ValueError(
                    f"provider stock universe does not contain {normalized_code}"
                )
            bars = self._provider_call(
                lambda: self._provider.fetch_daily_bars(
                    (normalized_code,), trading_day, trading_day, adjustment
                )
            )
            if len(bars) != 1 or bars[0].code != normalized_code:
                raise ValueError("provider did not return exactly one requested daily bar")
            fundamentals = self._provider_call(
                lambda: self._provider.fetch_fundamentals(
                    (normalized_code,), trading_day
                )
            )
            if any(item.code != normalized_code for item in fundamentals):
                raise ValueError("provider returned fundamentals for another stock")
        return ProviderSmokeOutcome(
            self._provider.source_name,
            normalized_code,
            trading_day,
            adjustment,
            len(trading_days),
            len(stocks),
            len(bars),
            len(fundamentals),
            0,
        )

    def _skip_success(
        self, dataset_id: str, trading_day: date, adjustment: AdjustmentMethod
    ) -> SyncOutcome | None:
        record = self._repository.get_sync_record(dataset_id, trading_day)
        if record is None or record.status is not SyncStatus.SUCCESS:
            return None
        if record.adjustment is not adjustment:
            raise ValueError(
                "a successful dataset/day already exists with a different adjustment"
            )
        warning = "数据已存在，跳过拉取"
        warnings.warn(warning, UserWarning, stacklevel=3)
        metadata = self._repository.get_dataset_metadata(dataset_id, trading_day, adjustment)
        return SyncOutcome(dataset_id, trading_day, SyncStatus.SUCCESS, True, warning, metadata)

    def _validate_retry(
        self,
        record: SyncRecord | None,
        retry: bool,
        now: datetime,
        adjustment: AdjustmentMethod,
    ) -> None:
        if record is not None and record.adjustment is not adjustment:
            raise ValueError("existing sync state uses a different adjustment")
        if record is None or record.status is not SyncStatus.FAILED:
            return
        if not retry:
            raise RetryRequiredError("previous synchronization failed; explicit retry is required")
        if record.finished_at is None:
            raise ValueError("FAILED sync record is missing finished_at")
        retry_at = record.finished_at.astimezone(SHANGHAI) + self._config.retry_cooldown
        if now < retry_at:
            raise CooldownActiveError(f"retry cooldown is active until {retry_at.isoformat()}")

    @staticmethod
    def _validate_payload(
        trading_day: date,
        selected_codes: Sequence[str],
        stocks: Sequence[StockIdentity],
        bars: Sequence[DailyBar],
        fundamentals: Sequence[FundamentalSnapshot],
    ) -> None:
        stock_codes = tuple(stock.code for stock in stocks)
        if not stock_codes:
            raise ValueError("provider returned an empty stock universe")
        if len(set(stock_codes)) != len(stock_codes):
            raise ValueError("provider returned duplicate stock identities")
        requested = set(selected_codes)
        if len(requested) != len(selected_codes):
            raise ValueError("requested stock codes must be unique")
        if not requested.issubset(set(stock_codes)):
            raise ValueError("requested stock codes are absent from the stock universe")
        bar_codes: set[str] = set()
        for bar in bars:
            if bar.trading_day != trading_day:
                raise ValueError("provider returned a daily bar outside the requested dataset")
            bar_codes.add(bar.code)
        if bar_codes != requested or len(bars) != len(requested):
            raise ValueError("provider did not return exactly one-day bars for all requested stocks")
        if any(item.code not in requested for item in fundamentals):
            raise ValueError("provider returned fundamentals for an unrequested stock")

    @staticmethod
    def _validate_calendar(days: Sequence[date], start: date, end: date) -> None:
        if not days:
            raise ValueError("provider returned an empty trading calendar")
        if len(set(days)) != len(days):
            raise ValueError("provider returned duplicate trading days")
        if any(day < start or day > end for day in days):
            raise ValueError("provider returned a trading day outside the requested range")

    def sync(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        *,
        retry: bool = False,
    ) -> SyncOutcome:
        """Synchronize one dataset/day once, atomically, under process and file locks."""
        if not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        lock_key = f"{self._lock_directory.resolve()}:{dataset_id}:{trading_day.isoformat()}"
        dataset_process_lock = process_lock(lock_key)
        dataset_file_lock = dataset_lock_path(
            self._lock_directory, dataset_id, trading_day
        )
        with dataset_process_lock, persistent_file_lock(dataset_file_lock):
            skipped = self._skip_success(dataset_id, trading_day, adjustment)
            if skipped is not None:
                return skipped
            now = self._now()
            existing = self._repository.get_sync_record(dataset_id, trading_day)
            self._validate_retry(existing, retry, now, adjustment)
            if existing is not None and existing.status is SyncStatus.RUNNING:
                warnings.warn(
                    "previous synchronization left a RUNNING record; it is superseded",
                    UserWarning,
                    stacklevel=3,
                )
            running = SyncRecord(
                dataset_id,
                trading_day,
                SyncStatus.RUNNING,
                self._provider.source_name,
                adjustment,
                now,
                None,
                None,
            )
            self._repository.save_sync_record(running)
            try:
                with self._provider_process_lock, persistent_file_lock(self._provider_file_lock):
                    calendar_end = trading_day + timedelta(days=self._config.calendar_horizon_days)
                    trading_days = self._provider_call(
                        lambda: self._provider.fetch_trading_days(trading_day, calendar_end)
                    )
                    self._validate_calendar(trading_days, trading_day, calendar_end)
                    if trading_day not in trading_days:
                        raise ValueError(f"{trading_day.isoformat()} is not an A-share trading day")
                    stocks = self._provider_call(lambda: self._provider.fetch_stocks(trading_day))
                    selected_codes = tuple(stock.code for stock in stocks)
                    bars = self._provider_call(
                        lambda: self._provider.fetch_daily_bars(
                            selected_codes, trading_day, trading_day, adjustment
                        )
                    )
                    fundamentals = self._provider_call(
                        lambda: self._provider.fetch_fundamentals(selected_codes, trading_day)
                    )
                    self._validate_payload(
                        trading_day,
                        selected_codes,
                        stocks,
                        bars,
                        fundamentals,
                    )
                finished_at = self._now()
                metadata = DatasetMetadata(
                    dataset_id,
                    trading_day,
                    self._provider.source_name,
                    finished_at,
                    adjustment,
                )
                success = SyncRecord(
                    dataset_id,
                    trading_day,
                    SyncStatus.SUCCESS,
                    self._provider.source_name,
                    adjustment,
                    now,
                    finished_at,
                    None,
                )
                self._repository.save_market_snapshot(
                    stocks,
                    bars,
                    fundamentals,
                    (),
                    trading_days,
                    metadata,
                    success,
                )
                self._prune(trading_day)
                return SyncOutcome(dataset_id, trading_day, SyncStatus.SUCCESS, False, None, metadata)
            except Exception as error:
                failed_at = self._now()
                failed = SyncRecord(
                    dataset_id,
                    trading_day,
                    SyncStatus.FAILED,
                    self._provider.source_name,
                    adjustment,
                    now,
                    failed_at,
                    str(error),
                )
                self._repository.save_sync_record(failed)
                raise SyncFailedError(
                    f"synchronization failed for {dataset_id} on {trading_day.isoformat()}"
                ) from error

    def sync_missing_on_startup(
        self,
        dataset_id: str,
        calendar_start: date,
        adjustment: AdjustmentMethod,
    ) -> tuple[SyncOutcome, ...]:
        """On application startup, fill locally known completed trading days once."""
        now = self._now()
        coverage_end = now.date() + timedelta(days=self._config.calendar_horizon_days)
        self.sync_trading_calendar(calendar_start, coverage_end)
        days = self._repository.get_trading_days(calendar_start, now.date())
        target = latest_completed_trading_day(now, days, self._config.cutoff_time)
        missing = [
            day
            for day in days
            if calendar_start <= day <= target
            and (
                (record := self._repository.get_sync_record(dataset_id, day)) is None
                or record.status is not SyncStatus.SUCCESS
            )
        ]
        outcomes: list[SyncOutcome] = []
        for day in missing:
            record = self._repository.get_sync_record(dataset_id, day)
            # FAILED 且冷却期已过 → 自动带 retry 重试,不再卡住启动;
            # FAILED 但冷却未过 → 跳过该日,等下次(不中止其余日)。
            auto_retry = (
                record is not None
                and record.status is SyncStatus.FAILED
                and record.finished_at is not None
                and record.finished_at + self._config.retry_cooldown <= self._now()
            )
            if (
                record is not None
                and record.status is SyncStatus.FAILED
                and not auto_retry
            ):
                warnings.warn(
                    f"{day.isoformat()} 上次同步失败且在冷却期内,本次跳过",
                    UserWarning,
                    stacklevel=3,
                )
                continue
            outcomes.append(
                self.sync(dataset_id, day, adjustment, retry=auto_retry)
            )
        return tuple(outcomes)

    def _prune(self, as_of: date) -> None:
        """Drop market data older than the configured retention window."""
        cutoff = as_of - timedelta(days=self._config.retention_days)
        self._repository.prune_before(cutoff)

    def _latest_completed_trading_day(self) -> date:
        """Resolve the newest completed A-share trading day from the local calendar."""
        now = self._now()
        calendar_start = now.date() - timedelta(days=self._config.retention_days)
        coverage_end = now.date() + timedelta(days=self._config.calendar_horizon_days)
        self.sync_trading_calendar(calendar_start, coverage_end)
        days = self._repository.get_trading_days(calendar_start, now.date())
        return latest_completed_trading_day(now, days, self._config.cutoff_time)

    def backfill_on_startup(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> SyncOutcome | None:
        """Ensure the local repository is current on startup.

        Always goes through :meth:`backfill_history` — the same mechanism as the
        manual sync button: per-100-stock chunk checkpoints, incremental tail
        (only new trading days since the last checkpoint are fetched), FAILED
        immune re-runs and stale-RUNNING takeover. Returns ``None`` when the
        dataset is already synced through the latest completed trading day.
        """
        if not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        target = self._latest_completed_trading_day()
        # 若保留窗口内存在 FAILED 的交易日,把回补目标退回到最早的失败日,
        # 以便重新拉取那段未能完整同步的缺口(否则会被最新 checkpoint 掩盖)。
        window_start = target - timedelta(days=self._config.retention_days)
        first_failed = self._repository.earliest_failed_day(
            dataset_id, window_start, target
        )
        if first_failed is not None:
            target = first_failed
        record = self._repository.get_sync_record(dataset_id, target)
        if record is not None and record.status is SyncStatus.SUCCESS:
            return None
        return self.backfill_history(dataset_id, target, adjustment)

    def backfill_history(
        self,
        dataset_id: str,
        as_of: date,
        adjustment: AdjustmentMethod,
        *,
        batch_size: int = 100,
    ) -> SyncOutcome:
        """Fetch and persist the retention window ending at ``as_of`` incrementally.

        Market data is written per code batch as it is fetched, so an interrupted
        backfill keeps every batch saved before the failure instead of losing the
        whole window. Re-running is idempotent: a previous ``FAILED`` marker does not
        gate a fresh attempt (unlike the explicit-retry single-day ``sync``).
        """
        if not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        start = as_of - timedelta(days=self._config.retention_days)
        lock_key = (
            f"{self._lock_directory.resolve()}:{dataset_id}:backfill:{as_of.isoformat()}"
        )
        dataset_process_lock = process_lock(lock_key)
        dataset_file_lock = dataset_lock_path(self._lock_directory, dataset_id, as_of)
        with dataset_process_lock, persistent_file_lock(dataset_file_lock):
            now = self._now()
            existing = self._repository.get_sync_record(dataset_id, as_of)
            if existing is not None and existing.status is SyncStatus.RUNNING:
                warnings.warn(
                    "previous backfill left a RUNNING record; resuming from completed chunks",
                    UserWarning,
                    stacklevel=3,
                )
            running = SyncRecord(
                dataset_id,
                as_of,
                SyncStatus.RUNNING,
                self._provider.source_name,
                adjustment,
                now,
                None,
                None,
            )
            self._repository.save_sync_record(running)
            try:
                with self._provider_process_lock, persistent_file_lock(self._provider_file_lock):
                    trading_days = self._provider_call(
                        lambda: self._provider.fetch_trading_days(start, as_of)
                    )
                    self._validate_calendar(trading_days, start, as_of)
                    stocks = self._provider_call(
                        lambda: self._provider.fetch_stocks(as_of)
                    )
                    if not stocks:
                        raise ValueError("provider returned an empty stock universe")
                    selected_codes = tuple(stock.code for stock in stocks)
                    metadata = DatasetMetadata(
                        dataset_id,
                        as_of,
                        self._provider.source_name,
                        self._now(),
                        adjustment,
                    )
                    self._repository.save_trading_days(trading_days, metadata)
                    self._repository.save_stocks(stocks, metadata)
                    # 增量尾部:上次回补已覆盖到 covered_end,本次只拉其后的新交易日,
                    # 不再重拉整个保留窗口(旧数据与 checkpoint 均保留)。
                    covered_end = self._repository.latest_backfill_cover_date(
                        dataset_id, adjustment
                    )
                    bars_start = start
                    if covered_end is not None and start <= covered_end < as_of:
                        bars_start = min(covered_end + timedelta(days=1), as_of)
                        print(
                            f"增量回补:上次覆盖到 {covered_end.isoformat()},"
                            f"本次只拉 {bars_start.isoformat()} 起的交易日"
                        )
                    # 若保留窗口内存在 FAILED 的交易日(夹在已成功天之间),把增量起点
                    # 退回最早的失败日,避免其被 covered_end 掩盖而永久跳过。
                    first_failed = self._repository.earliest_failed_day(
                        dataset_id, start, as_of
                    )
                    if first_failed is not None and first_failed < bars_start:
                        bars_start = first_failed
                        print(
                            f"发现失败日 {first_failed.isoformat()},"
                            f"增量起点退回该日重新拉取"
                        )
                    total_units = 2 * len(selected_codes)
                    done_units = 0
                    completed = self._repository.completed_chunk_codes(
                        dataset_id, as_of, adjustment
                    )
                    for offset in range(0, len(selected_codes), batch_size):
                        chunk_index = offset // batch_size
                        chunk = selected_codes[offset : offset + batch_size]
                        if completed.get(chunk_index) == tuple(sorted(chunk)):
                            done_units += 2 * len(chunk)
                            self._emit_progress(
                                "daily_bars", done_units, total_units, chunk[-1]
                            )
                            self._emit_progress(
                                "fundamentals", done_units, total_units, chunk[-1]
                            )
                            continue
                        bars = self._provider_call(
                            lambda chunk=chunk: self._provider.fetch_daily_bars(
                                chunk, bars_start, as_of, adjustment
                            )
                        )
                        self._repository.save_daily_bars(bars, metadata)
                        done_units += len(chunk)
                        self._emit_progress(
                            "daily_bars", done_units, total_units, chunk[-1]
                        )
                        fundamentals = self._provider_call(
                            lambda chunk=chunk: self._provider.fetch_fundamentals(
                                chunk, as_of
                            )
                        )
                        self._repository.save_fundamentals(fundamentals, metadata)
                        done_units += len(chunk)
                        self._emit_progress(
                            "fundamentals", done_units, total_units, chunk[-1]
                        )
                        self._repository.mark_chunk_complete(
                            dataset_id, as_of, adjustment, chunk_index, chunk
                        )
                finished_at = self._now()
                success = SyncRecord(
                    dataset_id,
                    as_of,
                    SyncStatus.SUCCESS,
                    self._provider.source_name,
                    adjustment,
                    now,
                    finished_at,
                    None,
                )
                self._repository.save_sync_record(success)
                self._prune(as_of)
                return SyncOutcome(
                    dataset_id, as_of, SyncStatus.SUCCESS, False, None, metadata
                )
            except Exception as error:
                failed_at = self._now()
                self._repository.save_sync_record(
                    SyncRecord(
                        dataset_id,
                        as_of,
                        SyncStatus.FAILED,
                        self._provider.source_name,
                        adjustment,
                        now,
                        failed_at,
                        str(error),
                    )
                )
                raise SyncFailedError(
                    f"backfill failed for {dataset_id} through {as_of.isoformat()}"
                ) from error

    def sync_trading_calendar(
        self, start: date, coverage_end: date, *, retry: bool = False
    ) -> SyncOutcome:
        """Idempotently cache A-share trading days through a natural-date coverage marker."""
        if start > coverage_end:
            raise ValueError("calendar start must not be after coverage_end")
        dataset_id = "trading_calendar"
        adjustment = AdjustmentMethod.UNADJUSTED
        lock_key = f"{self._lock_directory.resolve()}:{dataset_id}:{coverage_end.isoformat()}"
        calendar_process_lock = process_lock(lock_key)
        calendar_file_lock = self._lock_directory / f"{dataset_id}.{coverage_end.isoformat()}.lock"
        with calendar_process_lock, persistent_file_lock(calendar_file_lock):
            skipped = self._skip_success(dataset_id, coverage_end, adjustment)
            if skipped is not None:
                return skipped
            started_at = self._now()
            existing = self._repository.get_sync_record(dataset_id, coverage_end)
            self._validate_retry(existing, retry, started_at, adjustment)
            running = SyncRecord(
                dataset_id,
                coverage_end,
                SyncStatus.RUNNING,
                self._provider.source_name,
                adjustment,
                started_at,
                None,
                None,
            )
            self._repository.save_sync_record(running)
            try:
                with self._provider_process_lock, persistent_file_lock(self._provider_file_lock):
                    days = self._provider_call(
                        lambda: self._provider.fetch_trading_days(start, coverage_end)
                    )
                self._validate_calendar(days, start, coverage_end)
                finished_at = self._now()
                metadata = DatasetMetadata(
                    dataset_id,
                    coverage_end,
                    self._provider.source_name,
                    finished_at,
                    adjustment,
                )
                success = SyncRecord(
                    dataset_id,
                    coverage_end,
                    SyncStatus.SUCCESS,
                    self._provider.source_name,
                    adjustment,
                    started_at,
                    finished_at,
                    None,
                )
                self._repository.save_trading_calendar(days, metadata, success)
                return SyncOutcome(
                    dataset_id, coverage_end, SyncStatus.SUCCESS, False, None, metadata
                )
            except Exception as error:
                failed_at = self._now()
                self._repository.save_sync_record(
                    SyncRecord(
                        dataset_id,
                        coverage_end,
                        SyncStatus.FAILED,
                        self._provider.source_name,
                        adjustment,
                        started_at,
                        failed_at,
                        str(error),
                    )
                )
                raise SyncFailedError(
                    f"trading calendar synchronization failed through {coverage_end.isoformat()}"
                ) from error
    # ------------------------------------------------------------------
    # P5A-1 v2: eight-year coverage backfill (config v2 history path)
    # ------------------------------------------------------------------

    @staticmethod
    def _backfill_run_id(
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        as_of: date,
    ) -> str:
        """Deterministic run identity bound to the exact target range."""
        raw = (
            f"{dataset_id}|{adjustment.value}|"
            f"{target_start.isoformat()}|{as_of.isoformat()}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _resolve_targets_v2(self) -> tuple[date, date]:
        """Resolve the eight-year window: latest completed day and lookback start."""
        if self._config.history is None:
            raise ValueError("history config is required for v2 targets")
        now = self._now()
        span_days = self._config.history.target_years * 366 + 45
        calendar_start = now.date() - timedelta(days=span_days)
        coverage_end = now.date() + timedelta(
            days=self._config.calendar_horizon_days
        )
        self.sync_trading_calendar(calendar_start, coverage_end)
        days = self._repository.get_trading_days(calendar_start, now.date())
        if not days or days[0] > calendar_start:
            # 本地日历未覆盖窗口起点(例如 v1 只缓存了近一年):幂等跳过会复用
            # 旧 coverage_end 的 SUCCESS 而不拉取更早年份,这里强制刷新整段日历。
            with self._provider_process_lock, persistent_file_lock(
                self._provider_file_lock
            ):
                fetched = self._provider_call(
                    lambda: self._provider.fetch_trading_days(
                        calendar_start, coverage_end
                    )
                )
                self._validate_calendar(fetched, calendar_start, coverage_end)
                calendar_metadata = DatasetMetadata(
                    "trading_calendar",
                    coverage_end,
                    self._provider.source_name,
                    now,
                    AdjustmentMethod.UNADJUSTED,
                )
                self._repository.save_trading_days(fetched, calendar_metadata)
                self._repository.save_sync_record(
                    SyncRecord(
                        "trading_calendar",
                        coverage_end,
                        SyncStatus.SUCCESS,
                        self._provider.source_name,
                        AdjustmentMethod.UNADJUSTED,
                        now,
                        now,
                        None,
                    )
                )
            days = self._repository.get_trading_days(calendar_start, now.date())
        target_end = latest_completed_trading_day(
            now, days, self._config.cutoff_time
        )
        target_start = trading_day_lookback(
            days,
            target_end,
            self._config.history.target_years * TRADING_DAYS_PER_YEAR,
        )
        return target_start, target_end

    def _update_coverage_rows(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
    ) -> dict[str, DatasetCoverage]:
        """Recompute and persist per-data-type coverage against the target window."""
        plan = plan_coverage(
            dataset_id=dataset_id,
            adjustment=adjustment,
            target_start=target_start,
            target_end=target_end,
            probe=self._repository.actual_coverage,
        )
        coverages: dict[str, DatasetCoverage] = {}
        for type_plan in plan.per_type:
            gap_markers: list[date] = []
            for gap in (type_plan.prefix_gap, type_plan.tail_gap):
                if gap is not None:
                    gap_markers.extend(gap)
            item = DatasetCoverage(
                dataset_id=dataset_id,
                adjustment=adjustment,
                data_type=type_plan.data_type,
                earliest_day=type_plan.actual_earliest,
                latest_day=type_plan.actual_latest,
                status=type_plan.status,
                gap_days=tuple(gap_markers),
            )
            self._repository.save_dataset_coverage(item)
            coverages[type_plan.data_type] = item
        return coverages

    def _commit_generation(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
    ) -> DatasetVersion | None:
        """Commit an immutable COMPLETE generation once coverage is proven."""
        plan = plan_coverage(
            dataset_id=dataset_id,
            adjustment=adjustment,
            target_start=target_start,
            target_end=target_end,
            probe=self._repository.actual_coverage,
        )
        if plan.overall_status is not DataCoverageStatus.COMPLETE:
            return None
        digest = hashlib.sha256(
            (
                f"{dataset_id}|{adjustment.value}|"
                f"{target_start.isoformat()}|{target_end.isoformat()}"
            ).encode("utf-8")
        ).hexdigest()[:8]
        generation = f"{dataset_id}-{target_end.isoformat()}-{digest}"
        latest = self._repository.get_latest_dataset_version(
            dataset_id, adjustment
        )
        if latest is not None and latest.generation == generation:
            return latest
        version = DatasetVersion(
            dataset_id=dataset_id,
            generation=generation,
            source=self._provider.source_name,
            adjustment=adjustment,
            created_at=self._now(),
            status=DatasetVersionStatus.COMPLETE,
            coverage_start=target_start,
            coverage_end=target_end,
        )
        self._repository.save_dataset_version(version)
        return version

    def backfill_on_startup_v2(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> SyncOutcome | None:
        """v2 startup path: plan the eight-year window and backfill gaps.

        Returns None when the target window is already covered and a
        COMPLETE generation is committed. Requires config.history.
        """
        if self._config.history is None:
            raise ValueError("backfill_on_startup_v2 requires history in sync config")
        if not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        target_start, target_end = self._resolve_targets_v2()
        plan = plan_coverage(
            dataset_id=dataset_id,
            adjustment=adjustment,
            target_start=target_start,
            target_end=target_end,
            probe=self._repository.actual_coverage,
        )
        if not plan.needs_backfill:
            self._update_coverage_rows(dataset_id, adjustment, target_start, target_end)
            self._commit_generation(dataset_id, adjustment, target_start, target_end)
            return None
        return self.backfill_history_v2(
            dataset_id,
            adjustment,
            target_start=target_start,
            as_of=target_end,
        )

    def backfill_history_v2(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        *,
        target_start: date,
        as_of: date,
        batch_size: int = 100,
    ) -> SyncOutcome:
        """Eight-year range backfill with range-bound checkpoint identity.

        The run identity is a deterministic digest of (dataset, adjustment,
        target_start, as_of), so a completed one-year chunk can never be
        reused as an eight-year chunk, and re-running the exact same range is
        idempotent: an existing SUCCESS run is skipped with a warning, and an
        interrupted run resumes from its matching checkpoints only.

        Provider requests stay serial and paced inside the provider lock.
        """
        if not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if target_start > as_of:
            raise ValueError("target_start must not be after as_of")
        run_id = self._backfill_run_id(dataset_id, adjustment, target_start, as_of)
        lock_key = f"{self._lock_directory.resolve()}:{dataset_id}:backfillv2:{run_id}"
        dataset_process_lock = process_lock(lock_key)
        dataset_file_lock = dataset_lock_path(self._lock_directory, dataset_id, as_of)
        with dataset_process_lock, persistent_file_lock(dataset_file_lock):
            existing_run = self._repository.get_backfill_run_v2(run_id)
            if (
                existing_run is not None
                and existing_run.status is BackfillRunStatus.SUCCESS
            ):
                warnings.warn(
                    "八年回补已完成,跳过拉取",
                    UserWarning,
                    stacklevel=3,
                )
                return SyncOutcome(
                    dataset_id, as_of, SyncStatus.SUCCESS, True, "八年回补已完成,跳过拉取", None
                )
            if (
                existing_run is not None
                and existing_run.status is BackfillRunStatus.RUNNING
            ):
                warnings.warn(
                    "previous v2 backfill left a RUNNING run; resuming from completed chunks",
                    UserWarning,
                    stacklevel=3,
                )
            now = self._now()
            running = BackfillRunV2(
                run_id,
                dataset_id,
                adjustment,
                target_start,
                as_of,
                BackfillRunStatus.RUNNING,
                now,
                None,
                None,
            )
            self._repository.save_backfill_run_v2(running)
            try:
                plan = plan_coverage(
                    dataset_id=dataset_id,
                    adjustment=adjustment,
                    target_start=target_start,
                    target_end=as_of,
                    probe=self._repository.actual_coverage,
                )
                daily = plan.by_type("daily_bars")
                gaps: list[tuple[date, date]] = []
                if daily.prefix_gap is not None:
                    gaps.append(daily.prefix_gap)
                if daily.tail_gap is not None:
                    gaps.append(daily.tail_gap)
                if not gaps:
                    # ear/late 覆盖不代表完整:上次中断可能只拉了部分股票。
                    # 校验窗口内每个交易日 bar 股票数是否达到股票池的 95%,
                    # 不完整则把整个目标区间作为缺口重拉(幂等 INSERT OR REPLACE)。
                    pool = max(1, len(self._repository.get_stocks(as_of)))
                    complete_threshold = max(1, int(pool * 0.95))
                    counts = self._repository.daily_bar_stock_counts(
                        target_start, as_of, adjustment
                    )
                    incomplete_days = [
                        day
                        for day, n in counts.items()
                        if n < complete_threshold
                    ]
                    if incomplete_days:
                        gaps.append((target_start, as_of))
                if not gaps:
                    finished = self._now()
                    self._update_coverage_rows(
                        dataset_id, adjustment, target_start, as_of
                    )
                    self._commit_generation(
                        dataset_id, adjustment, target_start, as_of
                    )
                    self._repository.save_backfill_run_v2(
                        BackfillRunV2(
                            run_id,
                            dataset_id,
                            adjustment,
                            target_start,
                            as_of,
                            BackfillRunStatus.SUCCESS,
                            now,
                            finished,
                            None,
                        ),
                    )
                    self._repository.save_sync_record(
                        SyncRecord(
                            dataset_id,
                            as_of,
                            SyncStatus.SUCCESS,
                            self._provider.source_name,
                            adjustment,
                            now,
                            finished,
                            None,
                        ),
                    )
                    return SyncOutcome(
                        dataset_id, as_of, SyncStatus.SUCCESS, True, "数据已覆盖目标区间", None
                    )
                with self._provider_process_lock, persistent_file_lock(
                    self._provider_file_lock
                ):
                    trading_days = self._provider_call(
                        lambda: self._provider.fetch_trading_days(target_start, as_of)
                    )
                    self._validate_calendar(trading_days, target_start, as_of)
                    stocks = self._provider_call(
                        lambda: self._provider.fetch_stocks(as_of)
                    )
                    if not stocks:
                        raise ValueError("provider returned an empty stock universe")
                    selected_codes = tuple(stock.code for stock in stocks)
                    metadata = DatasetMetadata(
                        dataset_id,
                        as_of,
                        self._provider.source_name,
                        self._now(),
                        adjustment,
                    )
                    self._repository.save_trading_days(trading_days, metadata)
                    self._repository.save_stocks(stocks, metadata)
                    completed = self._repository.completed_chunk_codes_v2(run_id)
                    total_units = len(gaps) * 2 * len(selected_codes)
                    done_units = 0
                    for gap_start, gap_end in gaps:
                        for offset in range(0, len(selected_codes), batch_size):
                            chunk_index = offset // batch_size
                            chunk = selected_codes[offset : offset + batch_size]
                            checkpoint = completed.get(chunk_index)
                            if checkpoint is not None and any(
                                item.codes == tuple(chunk)
                                and item.range_start == gap_start
                                and item.range_end == gap_end
                                for item in checkpoint
                            ):
                                done_units += 2 * len(chunk)
                                continue
                            bars = self._provider_call(
                                lambda chunk=chunk, gap_start=gap_start, gap_end=gap_end: (
                                    self._provider.fetch_daily_bars(
                                        chunk, gap_start, gap_end, adjustment
                                    )
                                )
                            )
                            self._repository.save_daily_bars(bars, metadata)
                            done_units += len(chunk)
                            self._emit_v2_progress(
                                run_id, "daily_bars", done_units, total_units, chunk[-1]
                            )
                            fundamentals = self._provider_call(
                                lambda chunk=chunk: self._provider.fetch_fundamentals(
                                    chunk, as_of
                                )
                            )
                            self._repository.save_fundamentals(
                                fundamentals, metadata
                            )
                            done_units += len(chunk)
                            self._emit_v2_progress(
                                run_id, "fundamentals", done_units, total_units, chunk[-1]
                            )
                            self._repository.save_backfill_chunk_v2(
                                BackfillChunkV2(
                                    run_id,
                                    chunk_index,
                                    tuple(chunk),
                                    gap_start,
                                    gap_end,
                                    len(bars),
                                    BackfillRunStatus.SUCCESS,
                                ),
                            )
                finished_at = self._now()
                self._update_coverage_rows(
                    dataset_id, adjustment, target_start, as_of
                )
                self._commit_generation(
                    dataset_id, adjustment, target_start, as_of
                )
                self._repository.save_backfill_run_v2(
                    BackfillRunV2(
                        run_id,
                        dataset_id,
                        adjustment,
                        target_start,
                        as_of,
                        BackfillRunStatus.SUCCESS,
                        now,
                        finished_at,
                        None,
                    ),
                )
                self._repository.save_sync_record(
                    SyncRecord(
                        dataset_id,
                        as_of,
                        SyncStatus.SUCCESS,
                        self._provider.source_name,
                        adjustment,
                        now,
                        finished_at,
                        None,
                    ),
                )
                return SyncOutcome(
                    dataset_id, as_of, SyncStatus.SUCCESS, False, None, metadata
                )
            except Exception as error:
                failed_at = self._now()
                self._repository.save_backfill_run_v2(
                    BackfillRunV2(
                        run_id,
                        dataset_id,
                        adjustment,
                        target_start,
                        as_of,
                        BackfillRunStatus.FAILED,
                        now,
                        failed_at,
                        str(error),
                    ),
                )
                self._repository.save_sync_record(
                    SyncRecord(
                        dataset_id,
                        as_of,
                        SyncStatus.FAILED,
                        self._provider.source_name,
                        adjustment,
                        now,
                        failed_at,
                        str(error),
                    ),
                )
                raise SyncFailedError(
                    f"v2 backfill failed for {dataset_id} over "
                    f"{target_start.isoformat()}..{as_of.isoformat()}"
                ) from error

