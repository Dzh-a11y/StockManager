"""Immutable read-intent contracts for the swappable data-read layer (P4-1).

These domain objects describe *what* to read and *how to interpret* the result.
They contain no SQL, no sqlite3 references and no provider logic, so a future
DuckDB/PostgreSQL reader can implement the same contracts unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
)


@dataclass(frozen=True, slots=True)
class MarketDataReadRequest:
    """A validated, immutable read intent for one dataset window.

    Sorting contract: results are ordered by ``code ASC, trading_day ASC``.
    Codes are normalized (whitespace stripped, deduplicated) at construction.
    """

    dataset_id: str
    codes: tuple[str, ...]
    start: date
    end: date
    adjustment: AdjustmentMethod
    batch_size: int
    include_fundamentals: bool = False
    dividends_start: date | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.dividends_start is not None and self.dividends_start > self.end:
            raise ValueError("dividends_start must not be after end")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if not isinstance(self.adjustment, AdjustmentMethod):
            raise ValueError("adjustment must be provided explicitly")
        normalized = tuple(
            dict.fromkeys(code.strip() for code in self.codes if code.strip())
        )
        object.__setattr__(self, "codes", normalized)


@dataclass(frozen=True, slots=True)
class DatasetReadSnapshot:
    """Frozen dataset metadata captured before a concurrent read starts.

    Every shard must verify its own read against this snapshot; a version change
    during the read fails loudly instead of silently mixing two versions.
    """

    dataset_id: str
    trading_day: date
    adjustment: AdjustmentMethod
    source: str
    synced_at: datetime

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.synced_at.tzinfo is None or self.synced_at.utcoffset() is None:
            raise ValueError("synced_at must be timezone-aware")

    @classmethod
    def from_metadata(cls, metadata: DatasetMetadata) -> "DatasetReadSnapshot":
        return cls(
            metadata.dataset_id,
            metadata.trading_day,
            metadata.adjustment,
            metadata.source,
            metadata.synced_at,
        )


@dataclass(frozen=True, slots=True)
class MarketDataBatch:
    """One deterministic shard read: snapshot plus ordered domain objects.

    ``codes`` is the shard's requested code set (normalized). Bars,
    fundamentals and dividends must belong to that set; an empty shard
    yields empty tuples.
    """

    snapshot: DatasetReadSnapshot
    codes: tuple[str, ...]
    bars: tuple[DailyBar, ...]
    fundamentals: tuple[FundamentalSnapshot, ...]
    dividends: tuple[DividendRecord, ...]

    def __post_init__(self) -> None:
        shard = frozenset(self.codes)
        for bar in self.bars:
            if bar.code not in shard:
                raise ValueError("batch contains a bar outside the shard codes")
        for item in self.fundamentals:
            if item.code not in shard:
                raise ValueError("batch contains a fundamental outside the shard codes")
        for item in self.dividends:
            if item.code not in shard:
                raise ValueError("batch contains a dividend outside the shard codes")
