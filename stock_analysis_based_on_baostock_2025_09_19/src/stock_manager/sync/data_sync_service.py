"""Single-entry, idempotent synchronization into the local repository."""

import time
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    ProviderSmokeOutcome,
    StockIdentity,
    SyncOutcome,
    SyncRecord,
    SyncStatus,
)
from stock_manager.protocols import LocalRepositoryProtocol, ProviderProtocol
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
class SyncConfig:
    cutoff_time: wall_time
    retry_cooldown: timedelta
    minimum_request_interval_seconds: float
    calendar_horizon_days: int
    dividend_lookback_years: int

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


class _SerialRateLimiter:
    def __init__(
        self,
        minimum_interval_seconds: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._minimum_interval = minimum_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_call_at: float | None = None

    def call(self, operation: Callable[[], T]) -> T:
        now = self._monotonic()
        if self._last_call_at is not None:
            remaining = self._minimum_interval - (now - self._last_call_at)
            if remaining > 0:
                self._sleep(remaining)
        result = operation()
        self._last_call_at = self._monotonic()
        return result


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
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._lock_directory = lock_directory
        self._config = config
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._rate_limiter = _SerialRateLimiter(
            config.minimum_request_interval_seconds, monotonic, sleep
        )
        provider_key = f"{lock_directory.resolve()}:{provider.source_name}:provider"
        self._provider_process_lock = process_lock(provider_key)
        self._provider_file_lock = lock_directory / f"{provider.source_name}.provider.lock"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sync clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    def _provider_call(self, operation: Callable[[], T]) -> T:
        return self._rate_limiter.call(operation)

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
        dividend_start = date(
            trading_day.year - self._config.dividend_lookback_years, 1, 1
        )
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
            dividends = self._provider_call(
                lambda: self._provider.fetch_dividends(
                    (normalized_code,), dividend_start, trading_day
                )
            )
            if any(item.code != normalized_code for item in dividends):
                raise ValueError("provider returned dividends for another stock")
        return ProviderSmokeOutcome(
            self._provider.source_name,
            normalized_code,
            trading_day,
            adjustment,
            len(trading_days),
            len(stocks),
            len(bars),
            len(fundamentals),
            len(dividends),
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
        dividends: Sequence[DividendRecord],
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
        if any(item.code not in requested for item in dividends):
            raise ValueError("provider returned dividends for an unrequested stock")

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
                    dividend_start = date(
                        trading_day.year - self._config.dividend_lookback_years, 1, 1
                    )
                    dividends = self._provider_call(
                        lambda: self._provider.fetch_dividends(
                            selected_codes, dividend_start, trading_day
                        )
                    )
                    self._validate_payload(
                        trading_day,
                        selected_codes,
                        stocks,
                        bars,
                        fundamentals,
                        dividends,
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
                    dividends,
                    trading_days,
                    metadata,
                    success,
                )
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
        return tuple(
            self.sync(dataset_id, day, adjustment, retry=False) for day in missing
        )

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
