"""Concurrent sharded read orchestration over MarketDataReaderProtocol (P4-3).

MarketDataReadService splits sorted codes into bounded shards, reads each
shard with its own reader (own connection) through a controlled thread pool,
merges deterministically, and never returns partial results when a shard
fails. 'max_workers=1' reuses the exact same logic on a serial path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from stock_manager.domain import (
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
)
from stock_manager.read.contracts import (
    DatasetReadSnapshot,
    MarketDataBatch,
    MarketDataReadRequest,
)
from stock_manager.read.errors import (
    ShardReadError,
    SnapshotConsistencyError,
)
from stock_manager.read.protocols import (
    MarketDataReaderFactoryProtocol,
    MarketDataReaderProtocol,
)


# SQLite 实测（2026-08-31, Apple M5 Pro）：只读并发读取受 GIL 与磁盘带宽限制，
# 线程池并发无收益甚至更慢（串行 5.7s vs 4 workers 12.9s vs 8 workers 50.2s）。
# 因此读取服务默认串行；并发能力保留，供未来 DuckDB/PostgreSQL 等后端使用。
DEFAULT_MAX_WORKERS = 1
DEFAULT_BATCH_SIZE = 500
DEFAULT_SERIAL_THRESHOLD = 0


@dataclass(frozen=True, slots=True)
class MarketDataReadResult:
    """Merged, deterministically ordered result of a concurrent read."""

    snapshot: DatasetReadSnapshot
    bars: tuple[DailyBar, ...]
    fundamentals: tuple[FundamentalSnapshot, ...]
    dividends: tuple[DividendRecord, ...]
    shard_count: int
    serial_fallback: bool


class MarketDataReadService:
    """Split codes, read shards concurrently, merge in stable order.

    Concurrency is bounded: worker count, batch size and a small-data serial
    threshold are all configurable. Readers are created through the injected
    factory so every task owns an independent connection.
    """

    def __init__(
        self,
        reader_factory: MarketDataReaderFactoryProtocol,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        small_data_serial_threshold: int = DEFAULT_SERIAL_THRESHOLD,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if small_data_serial_threshold < 0:
            raise ValueError("small_data_serial_threshold must be non-negative")
        self._reader_factory = reader_factory
        self._max_workers = max_workers
        self._batch_size = batch_size
        self._serial_threshold = small_data_serial_threshold

    def read(
        self,
        request: MarketDataReadRequest,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> MarketDataReadResult:
        """Read the full request deterministically.

        The frozen snapshot is captured first; every shard verifies its own
        snapshot against it and a version change raises
        'SnapshotConsistencyError' instead of returning mixed data. Any
        shard failure cancels not-yet-started tasks and raises with the shard
        identity attached.
        """
        snapshot = self._capture_snapshot(request)
        shards = self._split_shards(request.codes)
        serial = self._should_run_serial(shards)
        if serial:
            batches = []
            for index, shard in enumerate(shards):
                try:
                    batches.append(self._read_shard_sync(snapshot, request, shard))
                except Exception as error:
                    if isinstance(error, SnapshotConsistencyError):
                        raise
                    if isinstance(error, ShardReadError) and error.shard_index != index:
                        raise ShardReadError(
                            index,
                            f"shard {index} read failed for dataset {request.dataset_id}",
                            error,
                        ) from error
                    raise ShardReadError(
                        index,
                        f"shard {index} read failed for dataset {request.dataset_id}",
                        error,
                    ) from error
        else:
            batches = self._read_shards_concurrent(snapshot, request, shards)
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "read",
                    "done": len(shards),
                    "total": len(shards),
                }
            )
        return self._merge(
            snapshot, batches, shard_count=len(shards), serial_fallback=serial
        )

    def _capture_snapshot(self, request: MarketDataReadRequest) -> DatasetReadSnapshot:
        with self._reader_factory.create() as reader:
            return reader.read_snapshot(
                request.dataset_id,
                request.end,
                request.adjustment.value,
            )

    def _split_shards(self, codes: Sequence[str]) -> tuple[tuple[str, ...], ...]:
        size = max(1, self._batch_size)
        return tuple(
            tuple(codes[index : index + size])
            for index in range(0, len(codes), size)
        )

    def _should_run_serial(self, shards: Sequence[tuple[str, ...]]) -> bool:
        if self._max_workers == 1:
            return True
        total_codes = sum(len(shard) for shard in shards)
        return total_codes <= self._serial_threshold

    def _read_shard_sync(
        self,
        frozen: DatasetReadSnapshot,
        request: MarketDataReadRequest,
        codes: tuple[str, ...],
    ) -> MarketDataBatch:
        with self._reader_factory.create() as reader:
            return self._read_one(reader, frozen, request, codes)

    def _read_shards_concurrent(
        self,
        frozen: DatasetReadSnapshot,
        request: MarketDataReadRequest,
        shards: tuple[tuple[str, ...], ...],
    ) -> list[MarketDataBatch]:
        results: list[MarketDataBatch | None] = [None] * len(shards)
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(
                    self._read_shard_sync,
                    frozen,
                    request,
                    shard,
                ): index
                for index, shard in enumerate(shards)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as error:
                    for other in futures:
                        other.cancel()
                    if isinstance(error, SnapshotConsistencyError):
                        raise
                    if isinstance(error, ShardReadError) and error.shard_index != index:
                        raise ShardReadError(
                            index,
                            f"shard {index} read failed for dataset {request.dataset_id}",
                            error,
                        ) from error
                    raise ShardReadError(
                        index,
                        f"shard {index} read failed for dataset {request.dataset_id}",
                        error,
                    ) from error
        return [item for item in results if item is not None]

    def _read_one(
        self,
        reader: MarketDataReaderProtocol,
        frozen: DatasetReadSnapshot,
        request: MarketDataReadRequest,
        codes: tuple[str, ...],
    ) -> MarketDataBatch:
        shard_request = MarketDataReadRequest(
            request.dataset_id,
            codes,
            request.start,
            request.end,
            request.adjustment,
            request.batch_size,
            include_fundamentals=request.include_fundamentals,
            dividends_start=request.dividends_start,
        )
        batch = reader.read_batch(shard_request)
        self._verify_snapshot(frozen, batch.snapshot, codes)
        return batch

    @staticmethod
    def _verify_snapshot(
        frozen: DatasetReadSnapshot,
        actual: DatasetReadSnapshot,
        codes: Sequence[str],
    ) -> None:
        if actual != frozen:
            names = ", ".join(codes[:3])
            raise SnapshotConsistencyError(
                f"dataset version changed during read (shard codes: {names})"
            )

    @staticmethod
    def _merge(
        snapshot: DatasetReadSnapshot,
        batches: Sequence[MarketDataBatch],
        *,
        shard_count: int,
        serial_fallback: bool,
    ) -> MarketDataReadResult:
        bars: list[DailyBar] = []
        fundamentals: list[FundamentalSnapshot] = []
        dividends: list[DividendRecord] = []
        for batch in batches:
            bars.extend(batch.bars)
            fundamentals.extend(batch.fundamentals)
            dividends.extend(batch.dividends)
        return MarketDataReadResult(
            snapshot,
            tuple(sorted(bars, key=lambda item: (item.code, item.trading_day))),
            tuple(sorted(fundamentals, key=lambda item: item.code)),
            tuple(sorted(dividends, key=lambda item: (item.code, item.ex_date))),
            shard_count,
            serial_fallback,
        )
