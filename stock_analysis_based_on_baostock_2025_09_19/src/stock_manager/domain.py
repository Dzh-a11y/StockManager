from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class AdjustmentMethod(str, Enum):
    UNADJUSTED = "unadjusted"
    QFQ = "qfq"
    HFQ = "hfq"


class SyncStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RuleStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StockIdentity:
    code: str
    name: str
    exchange: str
    is_st: bool
    listed_on: date | None
    delisted_on: date | None

    def __post_init__(self) -> None:
        _require_text(self.code, "code")
        _require_text(self.name, "name")
        _require_text(self.exchange, "exchange")


@dataclass(frozen=True, slots=True)
class DailyBar:
    code: str
    trading_day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    preclose: Decimal
    volume: Decimal
    amount: Decimal
    is_trading: bool

    def __post_init__(self) -> None:
        _require_text(self.code, "code")
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.volume < Decimal("0"):
            raise ValueError("volume must be non-negative")
        if self.amount < Decimal("0"):
            raise ValueError("amount must be non-negative")


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    code: str
    report_date: date
    published_on: date
    pe_ttm: Decimal | None
    pb: Decimal | None
    source: str

    def __post_init__(self) -> None:
        _require_text(self.code, "code")
        _require_text(self.source, "source")


@dataclass(frozen=True, slots=True)
class DividendRecord:
    code: str
    ex_date: date
    cash_dividend_per_share: Decimal
    source: str

    def __post_init__(self) -> None:
        _require_text(self.code, "code")
        _require_text(self.source, "source")


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    dataset_id: str
    trading_day: date
    source: str
    synced_at: datetime
    adjustment: AdjustmentMethod

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.source, "source")
        _require_aware(self.synced_at, "synced_at")


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    passed: bool
    actual_value: object
    threshold: object
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        _require_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    code: str
    trading_day: date
    passed: bool
    rule_results: tuple[RuleResult, ...]
    metadata: DatasetMetadata

    def __post_init__(self) -> None:
        _require_text(self.code, "code")


@dataclass(frozen=True, slots=True)
class RuleExecutionResult:
    rule_id: str
    status: RuleStatus
    result: RuleResult | None

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        if self.status is RuleStatus.SKIPPED and self.result is not None:
            raise ValueError("SKIPPED rule execution must not contain RuleResult")
        if self.status is not RuleStatus.SKIPPED and self.result is None:
            raise ValueError("evaluated rule execution requires RuleResult")
        if self.result is not None:
            if self.result.rule_id != self.rule_id:
                raise ValueError("execution rule_id must match RuleResult")
            expected = RuleStatus.PASSED if self.result.passed else RuleStatus.FAILED
            if self.status is not expected:
                raise ValueError("execution status must match RuleResult.passed")


@dataclass(frozen=True, slots=True)
class ParameterizedScreeningResult:
    code: str
    name: str
    trading_day: date
    passed: bool
    rule_executions: tuple[RuleExecutionResult, ...]
    metadata: DatasetMetadata
    template_id: str
    template_revision: int

    def __post_init__(self) -> None:
        _require_text(self.code, "code")
        _require_text(self.name, "name")
        _require_text(self.template_id, "template_id")
        if self.template_revision <= 0:
            raise ValueError("template_revision must be positive")


@dataclass(frozen=True, slots=True)
class SyncRecord:
    dataset_id: str
    trading_day: date
    status: SyncStatus
    source: str
    adjustment: AdjustmentMethod
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.source, "source")
        _require_aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_aware(self.finished_at, "finished_at")
        if self.status is SyncStatus.SUCCESS:
            if self.finished_at is None:
                raise ValueError("SUCCESS requires finished_at")
            if self.error_message is not None:
                raise ValueError("SUCCESS must not have error_message")
        if self.status is SyncStatus.FAILED:
            if self.finished_at is None:
                raise ValueError("FAILED requires finished_at")
            if self.error_message is None or not self.error_message.strip():
                raise ValueError("FAILED requires a non-empty error_message")


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    dataset_id: str
    trading_day: date
    status: SyncStatus
    skipped: bool
    warning: str | None
    metadata: DatasetMetadata | None

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        if self.skipped and (self.warning is None or not self.warning.strip()):
            raise ValueError("a skipped sync requires a warning")
        if not self.skipped and self.warning is not None:
            raise ValueError("a completed sync must not carry a skip warning")


@dataclass(frozen=True, slots=True)
class ProviderSmokeOutcome:
    source: str
    code: str
    trading_day: date
    adjustment: AdjustmentMethod
    trading_day_count: int
    stock_count: int
    bar_count: int
    fundamental_count: int
    dividend_count: int

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_text(self.code, "code")
        counts = (
            self.trading_day_count,
            self.stock_count,
            self.bar_count,
            self.fundamental_count,
            self.dividend_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("provider smoke counts must be non-negative")


class DataCoverageStatus(str, Enum):
    """Completeness of one data type over the target history window."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class DatasetVersionStatus(str, Enum):
    """Lifecycle of an immutable dataset generation."""

    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class BackfillRunStatus(str, Enum):
    """Lifecycle of a v2 history backfill run."""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DatasetCoverage:
    """Earliest/latest stored day and completeness of one data type."""

    dataset_id: str
    adjustment: AdjustmentMethod
    data_type: str
    earliest_day: date | None
    latest_day: date | None
    status: DataCoverageStatus
    gap_days: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.data_type, "data_type")
        if self.earliest_day is not None and self.latest_day is not None:
            if self.earliest_day > self.latest_day:
                raise ValueError("earliest_day must not be after latest_day")


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """Immutable dataset generation committed once coverage is complete."""

    dataset_id: str
    generation: str
    source: str
    adjustment: AdjustmentMethod
    created_at: datetime
    status: DatasetVersionStatus
    coverage_start: date | None
    coverage_end: date | None

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.generation, "generation")
        _require_text(self.source, "source")
        _require_aware(self.created_at, "created_at")
        if self.coverage_start is not None and self.coverage_end is not None:
            if self.coverage_start > self.coverage_end:
                raise ValueError("coverage_start must not be after coverage_end")


@dataclass(frozen=True, slots=True)
class BackfillRunV2:
    """One v2 history backfill attempt over an explicit target range."""

    run_id: str
    dataset_id: str
    adjustment: AdjustmentMethod
    target_start: date
    target_end: date
    status: BackfillRunStatus
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None
    data_types: tuple[str, ...] = ("daily_bars", "fundamentals", "stocks")

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_text(self.dataset_id, "dataset_id")
        if self.target_start > self.target_end:
            raise ValueError("target_start must not be after target_end")
        _require_aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_aware(self.finished_at, "finished_at")
        if self.status is BackfillRunStatus.SUCCESS:
            if self.finished_at is None:
                raise ValueError("SUCCESS requires finished_at")
            if self.error_message is not None:
                raise ValueError("SUCCESS must not have error_message")
        if self.status is BackfillRunStatus.FAILED:
            if self.finished_at is None:
                raise ValueError("FAILED requires finished_at")
            if self.error_message is None or not self.error_message.strip():
                raise ValueError("FAILED requires a non-empty error_message")


@dataclass(frozen=True, slots=True)
class BackfillChunkV2:
    """Checkpoint of one completed v2 backfill code chunk with its exact range.

    The checkpoint identity (run_id, chunk_index) is bound to the run whose
    target range is part of run_id, so a one-year chunk can never be reused
    as an eight-year chunk.
    """

    run_id: str
    chunk_index: int
    codes: tuple[str, ...]
    range_start: date
    range_end: date
    bar_count: int
    status: BackfillRunStatus

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if not self.codes:
            raise ValueError("codes must not be empty")
        if self.range_start > self.range_end:
            raise ValueError("range_start must not be after range_end")
        if self.bar_count < 0:
            raise ValueError("bar_count must be non-negative")

class HistoricalRunStatus(str, Enum):
    """Lifecycle of one historical screening run (async job state machine)."""

    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    BUILDING_SIGNALS = "BUILDING_SIGNALS"
    RUNNING_BACKTEST = "RUNNING_BACKTEST"
    NORMALIZING = "NORMALIZING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class HistoricalScreeningRun:
    """Persisted state of one historical signal-generation run."""

    run_id: str
    cache_key: str
    dataset_id: str
    adjustment: AdjustmentMethod
    generation: str | None
    template_id: str
    template_revision: int
    plan_fingerprint: str
    rule_implementation_version: str
    universe_policy: str
    evaluation_start: date
    evaluation_end: date
    status: HistoricalRunStatus
    progress_completed: int
    progress_total: int
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_text(self.cache_key, "cache_key")
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.template_id, "template_id")
        _require_text(self.rule_implementation_version, "rule_implementation_version")
        if self.template_revision <= 0:
            raise ValueError("template_revision must be positive")
        if self.evaluation_start > self.evaluation_end:
            raise ValueError("evaluation_start must not be after evaluation_end")
        if self.progress_completed < 0 or self.progress_total < 0:
            raise ValueError("progress counters must be non-negative")
        _require_aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_aware(self.finished_at, "finished_at")
        terminal = (
            HistoricalRunStatus.SUCCEEDED,
            HistoricalRunStatus.FAILED,
            HistoricalRunStatus.CANCELLED,
            HistoricalRunStatus.INTERRUPTED,
        )
        backtest_phases = (
            HistoricalRunStatus.RUNNING_BACKTEST,
            HistoricalRunStatus.NORMALIZING,
        )
        if self.status in backtest_phases:
            if self.finished_at is not None:
                raise ValueError(f"{self.status.value} must not have finished_at")
        if self.status in terminal:
            if self.finished_at is None:
                raise ValueError(f"{self.status.value} requires finished_at")
            if self.status is HistoricalRunStatus.FAILED:
                if self.error_message is None or not self.error_message.strip():
                    raise ValueError("FAILED requires a non-empty error_message")

