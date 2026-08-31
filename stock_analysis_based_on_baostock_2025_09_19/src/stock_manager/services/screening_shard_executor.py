"""Sharded parameterized screening execution (P4-4).

ScreeningShardExecutor owns exactly ONE level of concurrency for the
parameterized screening channel: the process pool is created here and workers
must not spawn further pools or thread pools. Each worker creates its own
read-only reader/connection, reads its shard's bars/fundamentals/dividends,
builds RuleContext objects and runs RuleEngine.evaluate() inside the worker.
Only structured results and progress counts cross the pool boundary - never
full raw market data.

Small universes or max_workers=1 fall back to the exact same per-code logic
executed serially in the calling process (preserving per-code progress
callbacks, which the Web layer relies on).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    ParameterizedScreeningResult,
    StockIdentity,
)
from stock_manager.read.contracts import MarketDataReadRequest
from stock_manager.read.errors import ShardReadError
from stock_manager.read.plan_view import PicklableScreeningPlan
from stock_manager.read.protocols import (
    MarketDataReaderFactoryProtocol,
    MarketDataReaderProtocol,
)
from stock_manager.rules.base import RuleContext
from stock_manager.rules.engine import RuleEngine
from stock_manager.rules.registry import RuleRegistry
from stock_manager.services.screening_data import ScreeningDataPlan


@dataclass(frozen=True, slots=True)
class ScreeningShardPayload:
    """Everything one worker needs; all fields must be picklable."""

    shard_index: int
    codes: tuple[str, ...]
    reader_factory: MarketDataReaderFactoryProtocol
    registry: RuleRegistry
    plan: PicklableScreeningPlan
    metadata: DatasetMetadata
    stocks: tuple[StockIdentity, ...]
    trading_day: date
    adjustment: AdjustmentMethod
    data_plan: ScreeningDataPlan
    dataset_id: str


@dataclass(frozen=True, slots=True)
class ScreeningShardResult:
    """Structured worker outcome; no raw bars cross the pool boundary."""

    shard_index: int
    results: tuple[ParameterizedScreeningResult, ...]
    completed: int


def _screening_shard_worker(payload: ScreeningShardPayload) -> ScreeningShardResult:
    """Module-level worker: create reader, read shard, evaluate rules."""
    stocks = {item.code: item for item in payload.stocks}
    engine = RuleEngine(payload.registry)
    plan = payload.plan.to_plan()
    bars, fundamentals, dividends = _read_shard(payload)
    bars_by_code = _group_bars(bars)
    fundamentals_by_code = {item.code: item for item in fundamentals}
    dividends_by_code = _group_dividends(dividends)
    results: list[ParameterizedScreeningResult] = []
    for code in payload.codes:
        stock = stocks[code]
        context = RuleContext(
            stock,
            payload.trading_day,
            payload.adjustment,
            payload.metadata,
            bars_by_code.get(code, ()),
            fundamentals_by_code.get(code),
            dividends_by_code.get(code, ()),
        )
        passed, executions = engine.evaluate(context, plan)
        results.append(
            ParameterizedScreeningResult(
                code,
                stock.name,
                payload.trading_day,
                passed,
                executions,
                payload.metadata,
                plan.template_id,
                plan.revision,
            )
        )
    return ScreeningShardResult(
        payload.shard_index, tuple(results), completed=len(payload.codes)
    )


def _read_shard(
    payload: ScreeningShardPayload,
) -> tuple[
    tuple[DailyBar, ...],
    tuple[FundamentalSnapshot, ...],
    tuple[DividendRecord, ...],
]:
    reader: MarketDataReaderProtocol = payload.reader_factory.create()
    try:
        request = MarketDataReadRequest(
            payload.dataset_id,
            payload.codes,
            payload.data_plan.market_start,
            payload.trading_day,
            payload.adjustment,
            batch_size=max(1, len(payload.codes)),
            include_fundamentals=payload.data_plan.needs_fundamental,
            dividends_start=payload.data_plan.dividend_start,
        )
        batch = reader.read_batch(request)
        _verify_snapshot(payload, batch)
        return batch.bars, batch.fundamentals, batch.dividends
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()


def _verify_snapshot(
    payload: ScreeningShardPayload,
    batch: object,
) -> None:
    """筛选 worker 必须验证快照与冻结元数据一致，禁止混合版本。"""
    from stock_manager.read.contracts import DatasetReadSnapshot
    from stock_manager.read.errors import SnapshotConsistencyError

    snapshot = DatasetReadSnapshot.from_metadata(payload.metadata)
    if batch.snapshot != snapshot:
        raise SnapshotConsistencyError(
            f"dataset version changed during screening shard "
            f"{payload.shard_index} (codes: {', '.join(payload.codes[:3])})"
        )


def _group_bars(items: Sequence[DailyBar]) -> dict[str, tuple[DailyBar, ...]]:
    grouped: dict[str, list[DailyBar]] = defaultdict(list)
    for item in items:
        grouped[item.code].append(item)
    return {
        code: tuple(sorted(values, key=lambda value: value.trading_day))
        for code, values in grouped.items()
    }


def _group_dividends(
    items: Sequence[DividendRecord],
) -> dict[str, tuple[DividendRecord, ...]]:
    grouped: dict[str, list[DividendRecord]] = defaultdict(list)
    for item in items:
        grouped[item.code].append(item)
    return {code: tuple(values) for code, values in grouped.items()}


class ScreeningShardExecutor:
    """Single-layer sharded execution for the parameterized screening channel."""

    def __init__(
        self,
        reader_factory: MarketDataReaderFactoryProtocol,
        registry: RuleRegistry,
        *,
        max_workers: int = 4,
        batch_size: int = 500,
        small_data_serial_threshold: int = 50,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if small_data_serial_threshold < 0:
            raise ValueError("small_data_serial_threshold must be non-negative")
        self._reader_factory = reader_factory
        self._registry = registry
        self._max_workers = max_workers
        self._batch_size = batch_size
        self._serial_threshold = small_data_serial_threshold

    def execute(
        self,
        plan: PicklableScreeningPlan,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        metadata: DatasetMetadata,
        stocks: Sequence[StockIdentity],
        codes: tuple[str, ...],
        data_plan: ScreeningDataPlan,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        max_workers: int | None = None,
    ) -> tuple[ParameterizedScreeningResult, ...]:
        """Screen codes with one level of controlled concurrency.

        Results are merged in codes order (codes are already sorted by the
        caller), matching the serial reference path exactly. Any worker failure
        propagates as-is; partial results are never returned.

        ``max_workers`` overrides the constructor default for this call when
        provided; it must be positive and is clamped to the configured upper
        bound.
        """
        if not codes:
            return ()
        stock_by_code = {item.code: item for item in stocks}
        missing_codes = tuple(code for code in codes if code not in stock_by_code)
        if missing_codes:
            raise ValueError(
                f"codes absent from the local stock snapshot: ",
                f"{', '.join(missing_codes)}",
            )
        workers = self._resolve_workers(max_workers)
        shards = self._split_shards(codes)
        serial = self._should_run_serial(shards, workers)
        if serial:
            return self._execute_serial(
                plan, dataset_id, trading_day, adjustment, metadata,
                stocks, shards, data_plan, progress_callback,
            )
        return self._execute_parallel(
            plan, dataset_id, trading_day, adjustment, metadata,
            stocks, shards, data_plan, progress_callback,
            workers,
        )

    def _resolve_workers(self, requested: int | None) -> int:
        if requested is None:
            return self._max_workers
        if requested <= 0:
            raise ValueError("max_workers must be positive")
        return min(requested, self._max_workers)

    def _should_run_serial(
        self,
        shards: Sequence[tuple[str, ...]],
        workers: int,
    ) -> bool:
        if workers == 1:
            return True
        total = sum(len(shard) for shard in shards)
        return total <= self._serial_threshold

    def _split_shards(self, codes: Sequence[str]) -> tuple[tuple[str, ...], ...]:
        size = max(1, self._batch_size)
        return tuple(
            tuple(codes[index : index + size])
            for index in range(0, len(codes), size)
        )


    def _execute_serial(
        self,
        plan: PicklableScreeningPlan,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        metadata: DatasetMetadata,
        stocks: Sequence[StockIdentity],
        shards: Sequence[tuple[str, ...]],
        data_plan: ScreeningDataPlan,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> tuple[ParameterizedScreeningResult, ...]:
        # 串行参考路径：与旧 ParameterizedScreeningService 逐股语义一致。
        all_results: list[ParameterizedScreeningResult] = []
        done = 0
        stock_by_code = {item.code: item for item in stocks}
        total = sum(len(shard) for shard in shards)
        for shard_index, codes in enumerate(shards):
            payload = ScreeningShardPayload(
                shard_index,
                codes,
                self._reader_factory,
                self._registry,
                plan,
                metadata,
                tuple(stock_by_code[code] for code in codes),
                trading_day,
                adjustment,
                data_plan,
                dataset_id,
            )
            result = _screening_shard_worker(payload)
            all_results.extend(result.results)
            for offset, code in enumerate(codes):
                if progress_callback is not None:
                    progress_callback(
                        {
                            "done": done + offset + 1,
                            "total": total,
                            "current_code": code,
                            "phase": "screening",
                        }
                    )
            done += result.completed
        return tuple(all_results)

    def _execute_parallel(
        self,
        plan: PicklableScreeningPlan,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        metadata: DatasetMetadata,
        stocks: Sequence[StockIdentity],
        shards: Sequence[tuple[str, ...]],
        data_plan: ScreeningDataPlan,
        progress_callback: Callable[[dict[str, object]], None] | None,
        workers: int,
    ) -> tuple[ParameterizedScreeningResult, ...]:
        stock_by_code = {item.code: item for item in stocks}
        payloads = [
            ScreeningShardPayload(
                index,
                codes,
                self._reader_factory,
                self._registry,
                plan,
                metadata,
                tuple(stock_by_code[code] for code in codes),
                trading_day,
                adjustment,
                data_plan,
                dataset_id,
            )
            for index, codes in enumerate(shards)
        ]
        ordered: dict[int, ScreeningShardResult] = {}
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_screening_shard_worker, payload): payload.shard_index
                for payload in payloads
            }
            done = 0
            total = sum(len(shard) for shard in shards)
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as error:
                    # M3: 取消未完成任务，不等待全部跑完
                    for other in futures:
                        other.cancel()
                    # M4: 带分片标识重包，保留原始异常链
                    index = futures[future]
                    if isinstance(error, ShardReadError) and error.shard_index != index:
                        raise ShardReadError(
                            index,
                            f"screening shard {index} failed",
                            error,
                        ) from error
                    if isinstance(error, ShardReadError):
                        raise
                    raise ShardReadError(
                        index,
                        f"screening shard {index} failed",
                        error,
                    ) from error
                ordered[result.shard_index] = result
                done += result.completed
                if progress_callback is not None:
                    progress_callback(
                        {
                            "done": done,
                            "total": total,
                            "current_code": result.results[-1].code if result.results else None,
                            "phase": "screening",
                        }
                    )
        merged: list[ParameterizedScreeningResult] = []
        for index in range(len(shards)):
            merged.extend(ordered[index].results)
        return tuple(merged)
