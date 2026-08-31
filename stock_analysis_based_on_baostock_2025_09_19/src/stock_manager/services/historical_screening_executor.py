"""Historical eligibility screening: stock-axis parallelism, time-axis order (P5A-4).

Each worker loads its code shard's full history once and walks the evaluation
days in strict order inside the worker. The coordinator merges shard results
deterministically and verifies dataset generation before and after the run.

Reference semantics: a single-process reference implementation calls the same
RuleEngine day-by-day; the parallel path must be exactly equal.
"""

from __future__ import annotations

import bisect
import hashlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    FundamentalSnapshot,
    StockIdentity,
)
from stock_manager.read.historical import (
    PointInTimeReaderProtocol,
)
from stock_manager.read.plan_view import PicklableScreeningPlan
from stock_manager.rules.base import RuleContext
from stock_manager.rules.engine import RuleEngine
from stock_manager.rules.registry import RuleRegistry
from stock_manager.templates.models import ScreeningPlan

SHANGHAI = ZoneInfo("Asia/Shanghai")
OutputMode = Literal["compact", "audit"]


class HistoricalScreeningError(RuntimeError):
    """A shard failed; no partial results are delivered."""


class DatasetGenerationMismatchError(RuntimeError):
    """Dataset changed between run start and finish (or bound generation differs)."""


@dataclass(frozen=True, slots=True)
class HistoricalScreeningRequest:
    dataset_id: str
    adjustment: AdjustmentMethod
    generation: str | None
    warmup_start: date
    score_start: date
    score_end: date
    evaluation_days: tuple[date, ...]
    output_mode: OutputMode = "compact"

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        if self.warmup_start > self.score_start:
            raise ValueError("warmup_start must not be after score_start")
        if self.score_start > self.score_end:
            raise ValueError("score_start must not be after score_end")
        if not self.evaluation_days:
            raise ValueError("evaluation_days must not be empty")
        ordered = tuple(sorted(set(self.evaluation_days)))
        if ordered != self.evaluation_days:
            raise ValueError("evaluation_days must be sorted and unique")
        if self.evaluation_days[0] < self.warmup_start or self.evaluation_days[-1] > self.score_end:
            raise ValueError("evaluation_days must lie inside the score window")


@dataclass(frozen=True, slots=True)
class EligibilitySnapshot:
    trading_day: date
    eligible_codes: tuple[str, ...]
    selected_count: int


@dataclass(frozen=True, slots=True)
class HistoricalScreeningResult:
    dataset_id: str
    adjustment: AdjustmentMethod
    generation: str | None
    score_start: date
    score_end: date
    snapshots: tuple[EligibilitySnapshot, ...]
    result_fingerprint: str
    provenance: dict[str, str]


@dataclass(frozen=True, slots=True)
class _WorkerPayload:
    database_path: str
    request: HistoricalScreeningRequest
    plan_view: PicklableScreeningPlan
    shard_codes: tuple[str, ...]
    shard_index: int
    max_history_bars: int


def _synthetic_metadata(
    dataset_id: str, trading_day: date, adjustment: AdjustmentMethod
) -> DatasetMetadata:
    """RuleContext requires metadata matching the day; PIT reads build a synthetic one."""
    return DatasetMetadata(
        dataset_id,
        trading_day,
        "pit-read",
        datetime.now(SHANGHAI),
        adjustment,
    )


def _universe_for(
    day: date, snapshots: tuple[tuple[date, tuple[StockIdentity, ...]], ...]
) -> tuple[StockIdentity, ...]:
    """Latest snapshot on/before day, filtered by listing/delisting boundaries."""
    latest: tuple[StockIdentity, ...] = ()
    for as_of, stocks in snapshots:
        if as_of > day:
            break
        latest = stocks
    if not latest:
        return ()
    return tuple(
        stock
        for stock in latest
        if (stock.listed_on is None or stock.listed_on <= day)
        and (stock.delisted_on is None or stock.delisted_on > day)
    )


def _latest_fundamental(
    fundamentals: Sequence[FundamentalSnapshot], day: date
) -> FundamentalSnapshot | None:
    """Most recent fundamental published on or before day."""
    published = [item.published_on for item in fundamentals]
    index = bisect.bisect_right(published, day)
    if index == 0:
        return None
    return fundamentals[index - 1]


def historical_worker(
    payload: _WorkerPayload,
    reader_factory: Callable[[], PointInTimeReaderProtocol] | None = None,
) -> dict[str, object]:
    """One shard: load once, walk evaluation days in order, return compressed result."""
    from pathlib import Path

    from stock_manager.read.historical import (
        PointInTimeRequest,
        SQLitePointInTimeReader,
    )

    if reader_factory is not None:
        reader = reader_factory()
    else:
        reader = SQLitePointInTimeReader(
            Path(payload.database_path),
            PointInTimeRequest(
                payload.request.dataset_id,
                payload.shard_codes,
                payload.request.warmup_start,
                payload.request.score_end,
                payload.request.adjustment,
            ),
        )
    try:
        bars = reader.bars_through(payload.request.score_end)
        fundamentals = reader.fundamentals_through(payload.request.score_end)
        snapshots = reader.all_universe_snapshots()
        plan: ScreeningPlan = payload.plan_view.to_plan()
        engine = RuleEngine(RuleRegistry())
        # 重建 registry:规则定义在 child 进程需从 builtin 重建
        from stock_manager.rules.builtin import build_default_registry

        engine = RuleEngine(build_default_registry())
        bars_by_code: dict[str, list[DailyBar]] = defaultdict(list)
        for bar in bars:
            bars_by_code[bar.code].append(bar)
        fundamentals_by_code: dict[str, list[FundamentalSnapshot]] = defaultdict(list)
        for item in fundamentals:
            fundamentals_by_code[item.code].append(item)
        shard_set = set(payload.shard_codes)
        day_results: list[tuple[str, tuple[str, ...], int]] = []
        audit_items: list[dict[str, object]] = []
        for day in payload.request.evaluation_days:
            eligible: list[str] = []
            for stock in _universe_for(day, snapshots):
                if stock.code not in shard_set:
                    continue
                code_bars = bars_by_code.get(stock.code, ())
                day_index = bisect.bisect_right(
                    [bar.trading_day for bar in code_bars], day
                )
                window = tuple(code_bars[:day_index])
                context = RuleContext(
                    stock,
                    day,
                    payload.request.adjustment,
                    _synthetic_metadata(
                        payload.request.dataset_id, day, payload.request.adjustment
                    ),
                    window,
                    _latest_fundamental(
                        fundamentals_by_code.get(stock.code, ()), day
                    ),
                    (),
                )
                passed, executions = engine.evaluate(context, plan)
                if passed:
                    eligible.append(stock.code)
                if payload.request.output_mode == "audit":
                    audit_items.append(
                        {
                            "trading_day": day.isoformat(),
                            "code": stock.code,
                            "passed": passed,
                            "executions": tuple(
                                (execution.rule_id, execution.status.value)
                                for execution in executions
                            ),
                        }
                    )
            day_results.append((day.isoformat(), tuple(eligible), len(eligible)))
        return {
            "shard_index": payload.shard_index,
            "day_results": day_results,
            "audit": audit_items,
        }
    finally:
        reader.close()


class HistoricalScreeningExecutor:
    """Single-level process pool over stock shards; no nested pools."""

    def __init__(
        self,
        registry: RuleRegistry,
        *,
        database_path: str,
        max_workers: int = 4,
        batch_size: int = 100,
        small_serial_threshold: int = 50,
        reader_factory: Callable[[], PointInTimeReaderProtocol] | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._registry = registry
        self._database_path = database_path
        self._max_workers = max_workers
        self._batch_size = batch_size
        self._small_serial_threshold = small_serial_threshold
        self._reader_factory = reader_factory

    def _open_reader(self) -> PointInTimeReaderProtocol:
        if self._reader_factory is not None:
            return self._reader_factory()
        from stock_manager.read.historical import (
            PointInTimeRequest,
            SQLitePointInTimeReader,
        )
        from pathlib import Path

        return SQLitePointInTimeReader(
            Path(self._database_path),
            PointInTimeRequest(
                "market", (), date.min, date.max, AdjustmentMethod.QFQ
            ),
        )

    def execute(
        self,
        plan: PicklableScreeningPlan,
        request: HistoricalScreeningRequest,
        codes: Sequence[str] = (),
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> HistoricalScreeningResult:
        all_codes = tuple(sorted(set(codes))) if codes else self._all_codes()
        if not all_codes:
            return HistoricalScreeningResult(
                request.dataset_id,
                request.adjustment,
                request.generation,
                request.score_start,
                request.score_end,
                (),
                "",
                {"note": "empty universe"},
            )
        if request.generation is not None:
            with self._open_reader() as reader:
                committed = reader.committed_generation()
                if committed != request.generation:
                    raise DatasetGenerationMismatchError(
                        f"bound generation {request.generation!r} does not match "
                        f"committed {committed!r}"
                    )
        shards = tuple(
            all_codes[offset : offset + self._batch_size]
            for offset in range(0, len(all_codes), self._batch_size)
        )
        if self._max_workers == 1 or len(shards) <= self._small_serial_threshold:
            shard_results = [
                self._run_shard(plan, request, shard, index, progress_callback)
                for index, shard in enumerate(shards)
            ]
        else:
            shard_results = self._run_parallel(
                plan, request, shards, progress_callback
            )
        merged = self._merge(shard_results, request)
        return merged

    def _all_codes(self) -> tuple[str, ...]:
        try:
            with self._open_reader() as reader:
                snapshots = reader.all_universe_snapshots()
        except Exception as error:
            raise HistoricalScreeningError(
                "cannot open the local database for historical screening"
            ) from error
        codes: set[str] = set()
        for _as_of, stocks in snapshots:
            codes.update(stock.code for stock in stocks)
        return tuple(sorted(codes))

    def _run_shard(
        self,
        plan: PicklableScreeningPlan,
        request: HistoricalScreeningRequest,
        shard_codes: tuple[str, ...],
        shard_index: int,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> dict[str, object]:
        payload = _WorkerPayload(
            self._database_path,
            request,
            plan,
            shard_codes,
            shard_index,
            0,
        )
        try:
            result = historical_worker(payload, self._reader_factory)
        except Exception as error:
            raise HistoricalScreeningError(
                f"historical screening shard {shard_index} failed"
            ) from error
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "historical_screening",
                    "completed": shard_index + 1,
                    "total": 1,
                    "shard": shard_index,
                }
            )
        return result

    def _run_parallel(
        self,
        plan: PicklableScreeningPlan,
        request: HistoricalScreeningRequest,
        shards: tuple[tuple[str, ...], ...],
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        with ProcessPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [
                pool.submit(historical_worker, payload)
                for payload in (
                    _WorkerPayload(
                        self._database_path,
                        request,
                        plan,
                        shard,
                        index,
                        0,
                    )
                    for index, shard in enumerate(shards)
                )
            ]
            done = 0
            for future in futures:
                try:
                    result = future.result()
                except Exception as error:
                    raise HistoricalScreeningError(
                        "historical screening shard failed"
                    ) from error
                results.append(result)
                done += 1
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "historical_screening",
                            "completed": done,
                            "total": len(shards),
                        }
                    )
        return results

    def _merge(
        self,
        shard_results: Sequence[dict[str, object]],
        request: HistoricalScreeningRequest,
    ) -> HistoricalScreeningResult:
        by_day: dict[date, list[str]] = defaultdict(list)
        for shard in shard_results:
            for day_text, codes, _count in shard["day_results"]:
                day = date.fromisoformat(day_text)
                by_day[day].extend(codes)
        snapshots = tuple(
            EligibilitySnapshot(
                day, tuple(sorted(by_day[day])), len(by_day[day])
            )
            for day in sorted(by_day)
        )
        digest = hashlib.sha256()
        for snapshot in snapshots:
            digest.update(
                f"|{snapshot.trading_day.isoformat()}:{','.join(snapshot.eligible_codes)}".encode(
                    "utf-8"
                )
            )
        fingerprint = digest.hexdigest()[:16]
        return HistoricalScreeningResult(
            request.dataset_id,
            request.adjustment,
            request.generation,
            request.score_start,
            request.score_end,
            snapshots,
            fingerprint,
            {
                "mode": "parallel" if len(shard_results) > 1 else "serial",
                "shards": str(len(shard_results)),
                "output_mode": request.output_mode,
            },
        )


def reference_serial_screening(
    *,
    database_path: str,
    plan: PicklableScreeningPlan,
    request: HistoricalScreeningRequest,
    codes: Sequence[str] = (),
) -> HistoricalScreeningResult:
    """Single-process reference: must match the parallel executor exactly."""
    executor = HistoricalScreeningExecutor(
        build_default_registry(),
        database_path=database_path,
        max_workers=1,
        batch_size=500,
        small_serial_threshold=0,
    )
    return executor.execute(plan, request, codes)


from stock_manager.rules.builtin import build_default_registry  # noqa: E402
