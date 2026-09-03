from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class AdjustmentMethod(str, Enum):
    UNADJUSTED = "unadjusted"
    QFQ = "qfq"
    HFQ = "hfq"


class IndexReturnVersion(str, Enum):
    """The economic return convention represented by an index level."""

    PRICE = "price"
    GROSS_TOTAL_RETURN = "gross_total_return"
    NET_TOTAL_RETURN = "net_total_return"


@dataclass(frozen=True, slots=True)
class IndexIdentity:
    """A provider-mapped index; indexes never enter the A-share stock pool."""

    index_id: str
    provider_code: str
    name: str
    category: str
    return_version: IndexReturnVersion
    source: str

    def __post_init__(self) -> None:
        for field_name in ("index_id", "provider_code", "name", "category", "source"):
            _require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class IndexDailyBar:
    """One published local index closing level and its explicit return version."""

    index_id: str
    trading_day: date
    close: Decimal
    return_version: IndexReturnVersion

    def __post_init__(self) -> None:
        _require_text(self.index_id, "index_id")
        if not self.close.is_finite() or self.close <= Decimal("0"):
            raise ValueError("index close must be finite and positive")


@dataclass(frozen=True, slots=True)
class DepositRate:
    """A verified central-bank annual deposit benchmark rate effective on a day."""

    term: str
    effective_on: date
    annual_rate: Decimal
    source: str

    def __post_init__(self) -> None:
        _require_text(self.term, "term")
        _require_text(self.source, "source")
        if not self.annual_rate.is_finite() or self.annual_rate < Decimal("0"):
            raise ValueError("annual_rate must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class CapmResultRecord:
    """Persistable outcome of one requested stock/window analysis."""

    analysis_id: str
    stock_code: str
    as_of: date
    window_days: int
    benchmark_id: str
    benchmark_return_version: IndexReturnVersion
    rate_term: str
    alpha_daily: Decimal | None
    alpha_annualized: Decimal | None
    beta: Decimal | None
    r_squared: Decimal | None
    observation_count: int
    periods_per_year: int
    status: str
    reason: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("analysis_id", "stock_code", "benchmark_id", "rate_term", "status"):
            _require_text(getattr(self, field_name), field_name)
        if self.window_days <= 0 or self.observation_count < 0 or self.periods_per_year <= 0:
            raise ValueError("CAPM result numeric bounds are invalid")
        _require_aware(self.created_at, "created_at")


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


# ---------------------------------------------------------------------------
# P5 DataSync reconstruction domain contracts (P5-RD-1)
# ---------------------------------------------------------------------------


class SyncPlanMode(str, Enum):
    """Top-level mode of a deterministic synchronization plan."""

    BOOTSTRAP = "BOOTSTRAP"
    INCREMENTAL = "INCREMENTAL"
    REPAIR = "REPAIR"
    LEGACY_IMPORT = "LEGACY_IMPORT"


class SyncSource(str, Enum):
    """Where the plan's data comes from."""

    BAOSTOCK = "BAOSTOCK"
    SEED = "SEED"
    LEGACY_DATABASE = "LEGACY_DATABASE"


class SyncPlanStatus(str, Enum):
    """Lifecycle of one deterministic sync plan."""

    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SyncTaskStatus(str, Enum):
    """Lifecycle of one serial fetch/write task within a plan."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class CandidateGenerationStatus(str, Enum):
    """Lifecycle of a candidate generation (P5 plan section 6.3)."""

    PLANNED = "PLANNED"
    WRITING = "WRITING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    PUBLISHED = "PUBLISHED"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED = "SUPERSEDED"


class VerificationStatus(str, Enum):
    """Outcome of one coverage verification partition."""

    COMPLETE = "COMPLETE"
    ACCEPTED_WITH_GAPS = "ACCEPTED_WITH_GAPS"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class IssueType(str, Enum):
    """Machine-consumable problem classification in a VerificationReport."""

    MISSING = "MISSING"
    INVALID = "INVALID"
    DUPLICATE = "DUPLICATE"
    ADJUSTMENT_MISMATCH = "ADJUSTMENT_MISMATCH"
    PIT_VIOLATION = "PIT_VIOLATION"


class Repairability(str, Enum):
    """Whether a verification issue can be fixed by a provider refetch."""

    REFETCH = "REFETCH"
    REBUILD = "REBUILD"
    MANUAL = "MANUAL"


class ReadinessStatus(str, Enum):
    """Result of a ReadinessGate evaluation for a read request."""

    READY = "READY"
    NO_GENERATION = "NO_GENERATION"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    MISSING_DATA_TYPE = "MISSING_DATA_TYPE"
    ADJUSTMENT_MISMATCH = "ADJUSTMENT_MISMATCH"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """A deterministic, reproducible synchronization plan (P5 section 4.2).

    ``plan_id`` and ``plan_fingerprint`` must be deterministically derived
    from the normalized plan inputs (dataset, adjustment, universe policy,
    target range, data types, provider identity, planner version); the same
    inputs always produce the same plan identity.
    """

    plan_id: str
    plan_version: int
    mode: SyncPlanMode
    source: SyncSource
    dataset_id: str
    adjustment: AdjustmentMethod
    universe_policy: str
    target_start: date
    target_end: date
    latest_completed_trading_day: date | None
    parent_generation: str | None
    candidate_generation_id: str | None
    required_data_types: tuple[str, ...]
    task_count: int
    plan_fingerprint: str
    status: SyncPlanStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.universe_policy, "universe_policy")
        _require_text(self.plan_fingerprint, "plan_fingerprint")
        if self.plan_version <= 0:
            raise ValueError("plan_version must be positive")
        if self.target_start > self.target_end:
            raise ValueError("target_start must not be after target_end")
        if not self.required_data_types:
            raise ValueError("required_data_types must not be empty")
        if self.task_count < 0:
            raise ValueError("task_count must be non-negative")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class SyncTask:
    """One serial fetch/write task within a sync plan (P5 section 4.3)."""

    task_id: str
    plan_id: str
    sequence_no: int
    data_type: str
    partition_key: str
    codes: tuple[str, ...]
    range_start: date
    range_end: date
    dependencies: tuple[str, ...]
    status: SyncTaskStatus
    attempt_count: int
    not_before: datetime | None
    row_count: int | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        _require_text(self.plan_id, "plan_id")
        _require_text(self.data_type, "data_type")
        _require_text(self.partition_key, "partition_key")
        if self.sequence_no < 0:
            raise ValueError("sequence_no must be non-negative")
        if not self.codes and self.data_type not in ("index_catalog", "deposit_rates", "index_daily_bars"):
            raise ValueError("codes must not be empty")
        if self.range_start > self.range_end:
            raise ValueError("range_start must not be after range_end")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")
        if self.not_before is not None:
            _require_aware(self.not_before, "not_before")
        if self.row_count is not None and self.row_count < 0:
            raise ValueError("row_count must be non-negative")
        if self.started_at is not None:
            _require_aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_aware(self.finished_at, "finished_at")
        if self.status is SyncTaskStatus.SUCCESS:
            if self.finished_at is None:
                raise ValueError("SUCCESS task requires finished_at")
            if self.error_message is not None:
                raise ValueError("SUCCESS task must not have error_message")
        if self.status is SyncTaskStatus.FAILED:
            if self.finished_at is None:
                raise ValueError("FAILED task requires finished_at")
            if self.error_message is None or not self.error_message.strip():
                raise ValueError("FAILED task requires a non-empty error_message")


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    """A candidate generation being written/verified before publish."""

    candidate_generation_id: str
    plan_id: str
    parent_generation: str | None
    write_revision: int
    status: CandidateGenerationStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.candidate_generation_id, "candidate_generation_id")
        _require_text(self.plan_id, "plan_id")
        if self.write_revision < 0:
            raise ValueError("write_revision must be non-negative")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class IngestBatch:
    """An immutable data batch bound to a candidate generation (P5 section 6)."""

    batch_id: str
    candidate_generation_id: str
    data_type: str
    partition_key: str
    codes: tuple[str, ...]
    range_start: date
    range_end: date
    row_count: int
    source: str
    batch_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.batch_id, "batch_id")
        _require_text(self.candidate_generation_id, "candidate_generation_id")
        _require_text(self.data_type, "data_type")
        _require_text(self.partition_key, "partition_key")
        _require_text(self.source, "source")
        _require_text(self.batch_sha256, "batch_sha256")
        if not self.codes and self.data_type not in ("index_catalog", "deposit_rates", "index_daily_bars"):
            raise ValueError("codes must not be empty")
        if self.range_start > self.range_end:
            raise ValueError("range_start must not be after range_end")
        if self.row_count < 0:
            raise ValueError("row_count must be non-negative")
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class CoverageVerification:
    """Evidence for one data type/partition verification (P5 section 7.6)."""

    candidate_generation_id: str
    data_type: str
    partition_key: str
    expected_count: int
    actual_count: int
    distinct_count: int
    duplicate_count: int
    invalid_count: int
    coverage_ratio: Decimal
    missing_items: tuple[str, ...]
    status: VerificationStatus
    verified_revision: int
    manifest_sha256: str
    verified_at: datetime
    details_json: str

    def __post_init__(self) -> None:
        _require_text(self.candidate_generation_id, "candidate_generation_id")
        _require_text(self.data_type, "data_type")
        _require_text(self.partition_key, "partition_key")
        _require_text(self.manifest_sha256, "manifest_sha256")
        counts = (
            self.expected_count,
            self.actual_count,
            self.distinct_count,
            self.duplicate_count,
            self.invalid_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("verification counts must be non-negative")
        if self.coverage_ratio < Decimal("0") or self.coverage_ratio > Decimal("1"):
            raise ValueError("coverage_ratio must be within [0, 1]")
        if self.verified_revision < 0:
            raise ValueError("verified_revision must be non-negative")
        _require_aware(self.verified_at, "verified_at")


@dataclass(frozen=True, slots=True)
class GenerationPartition:
    """Immutable mapping of one generation partition to a batch."""

    generation: str
    data_type: str
    partition_key: str
    batch_id: str

    def __post_init__(self) -> None:
        _require_text(self.generation, "generation")
        _require_text(self.data_type, "data_type")
        _require_text(self.partition_key, "partition_key")
        _require_text(self.batch_id, "batch_id")


@dataclass(frozen=True, slots=True)
class PublishedGeneration:
    """A generation that has been atomically published and is immutable."""

    generation: str
    dataset_id: str
    adjustment: AdjustmentMethod
    parent_generation: str | None
    manifest_sha256: str
    published_at: datetime
    status: CandidateGenerationStatus

    def __post_init__(self) -> None:
        _require_text(self.generation, "generation")
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.manifest_sha256, "manifest_sha256")
        _require_aware(self.published_at, "published_at")
        if self.status not in (CandidateGenerationStatus.PUBLISHED, CandidateGenerationStatus.SUPERSEDED):
            raise ValueError("published generation status must be PUBLISHED or SUPERSEDED")


@dataclass(frozen=True, slots=True)
class ActiveGeneration:
    """The current readable generation pointer for one dataset/adjustment."""

    dataset_id: str
    adjustment: AdjustmentMethod
    generation: str
    activated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.generation, "generation")
        _require_aware(self.activated_at, "activated_at")


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    """One machine-consumable problem entry in a VerificationReport (P5 7.7)."""

    issue_id: str
    candidate_generation_id: str
    data_type: str
    partition_key: str
    trading_day: date | None
    codes: tuple[str, ...]
    issue_type: IssueType
    expected_count: int
    actual_count: int
    repairability: Repairability
    details: str

    def __post_init__(self) -> None:
        _require_text(self.issue_id, "issue_id")
        _require_text(self.candidate_generation_id, "candidate_generation_id")
        _require_text(self.data_type, "data_type")
        _require_text(self.partition_key, "partition_key")
        _require_text(self.details, "details")
        if self.expected_count < 0 or self.actual_count < 0:
            raise ValueError("issue counts must be non-negative")


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Structured output of a coverage verification run (P5 section 7.7)."""

    candidate_generation_id: str
    issues: tuple[VerificationIssue, ...]
    generated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.candidate_generation_id, "candidate_generation_id")
        _require_aware(self.generated_at, "generated_at")
        for issue in self.issues:
            if issue.candidate_generation_id != self.candidate_generation_id:
                raise ValueError("issue candidate must match the report candidate")


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """Outcome of a ReadinessGate evaluation (P5 section 8)."""

    status: ReadinessStatus
    dataset_id: str
    adjustment: AdjustmentMethod
    generation: str | None
    reason: str | None

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        if self.status is ReadinessStatus.READY:
            if self.generation is None:
                raise ValueError("READY requires a generation")
            if self.reason is not None:
                raise ValueError("READY must not carry a reason")
        elif self.generation is not None:
            raise ValueError("non-READY result must not carry a generation")
        if self.status is not ReadinessStatus.READY:
            if self.reason is None or not self.reason.strip():
                raise ValueError("non-READY result requires a reason")
