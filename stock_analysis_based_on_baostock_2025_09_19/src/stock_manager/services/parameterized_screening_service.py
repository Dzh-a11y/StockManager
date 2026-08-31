"""Plan-driven, offline screening orchestration (P4-4 sharded execution).

The service keeps its public contract (Web/CLI) unchanged: screen() still
takes a compiled ScreeningPlan and returns ordered ParameterizedScreeningResult
objects. Internally, data reads and RuleEngine evaluation are delegated to the
ScreeningShardExecutor so each shard worker owns its read-only connection and
evaluates rules inside the worker; a serial fallback path (same per-code
semantics) is used when no SQLite database_path is available or when
max_workers=1/small-data thresholds apply.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    ParameterizedScreeningResult,
    StockIdentity,
)
from stock_manager.protocols import LocalRepositoryProtocol
from stock_manager.read.plan_view import PicklableScreeningPlan
from stock_manager.read.protocols import MarketDataReaderFactoryProtocol
from stock_manager.read.sqlite_reader import SQLiteMarketDataReaderFactory
from stock_manager.rules.base import RuleContext
from stock_manager.rules.engine import RuleEngine
from stock_manager.rules.registry import RuleRegistry
from stock_manager.services.screening_data import ScreeningDataPlan, ScreeningDataPlanner
from stock_manager.services.screening_service import (
    DatasetUnavailableError,
    StockNotFoundError,
)
from stock_manager.services.screening_shard_executor import ScreeningShardExecutor
from stock_manager.templates.models import ScreeningPlan


MAX_WORKERS_LIMIT = 16


class ParameterizedScreeningService:
    def __init__(
        self,
        repository: LocalRepositoryProtocol,
        registry: RuleRegistry,
        *,
        reader_factory: MarketDataReaderFactoryProtocol | None = None,
        max_workers: int = MAX_WORKERS_LIMIT,
        batch_size: int = 500,
        small_data_serial_threshold: int = 50,
    ) -> None:
        self._repository = repository
        self._planner = ScreeningDataPlanner()
        self._engine = RuleEngine(registry)
        self._registry = registry
        database_path = getattr(repository, "database_path", None)
        if reader_factory is None and isinstance(database_path, Path):
            reader_factory = SQLiteMarketDataReaderFactory(database_path)
        self._executor: ScreeningShardExecutor | None = (
            ScreeningShardExecutor(
                reader_factory,
                registry,
                max_workers=max_workers,
                batch_size=batch_size,
                small_data_serial_threshold=small_data_serial_threshold,
            )
            if reader_factory is not None
            else None
        )

    def screen(
        self,
        plan: ScreeningPlan,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        codes: Sequence[str] = (),
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        max_workers: int | None = None,
    ) -> tuple[ParameterizedScreeningResult, ...]:
        normalized_dataset_id = dataset_id.strip()
        if not normalized_dataset_id:
            raise ValueError("dataset_id must not be empty")
        if adjustment is not plan.adjustment:
            raise ValueError("requested adjustment does not match screening plan")
        metadata = self._repository.get_dataset_metadata(
            normalized_dataset_id, trading_day, adjustment
        )
        if metadata is None:
            raise DatasetUnavailableError(
                f"local dataset {normalized_dataset_id!r} is unavailable for "
                f"{trading_day.isoformat()} with adjustment {adjustment.value}"
            )
        stocks = {item.code: item for item in self._repository.get_stocks(trading_day)}
        requested = {item.strip() for item in codes if item.strip()}
        selected = tuple(sorted(requested or stocks))
        missing = tuple(code for code in selected if code not in stocks)
        if missing:
            raise StockNotFoundError(
                f"stock code(s) absent from local snapshot: {', '.join(missing)}"
            )
        if not selected:
            return ()
        trading_days = tuple(self._repository.get_trading_days(date.min, trading_day))
        if not trading_days or trading_days[-1] != trading_day:
            raise DatasetUnavailableError(
                f"local trading calendar does not contain {trading_day.isoformat()}"
            )
        data_plan = self._planner.plan(plan, trading_day, trading_days)
        if self._executor is not None:
            return self._executor.execute(
                PicklableScreeningPlan.from_plan(plan),
                normalized_dataset_id,
                trading_day,
                adjustment,
                metadata,
                tuple(stocks.values()),
                selected,
                data_plan,
                progress_callback,
                max_workers,
            )
        return self._serial_screen(
            plan,
            normalized_dataset_id,
            trading_day,
            adjustment,
            stocks,
            selected,
            data_plan,
            metadata,
            progress_callback,
        )

    def _serial_screen(
        self,
        plan: ScreeningPlan,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        stocks: dict[str, StockIdentity],
        selected: tuple[str, ...],
        data_plan: ScreeningDataPlan,
        metadata: DatasetMetadata,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> tuple[ParameterizedScreeningResult, ...]:
        """Legacy serial fallback used when no reader factory is available."""

        del dataset_id
        bars = self._repository.get_daily_bars(
            selected, data_plan.market_start, trading_day, adjustment
        )
        fundamentals = (
            self._repository.get_fundamentals(selected, trading_day)
            if data_plan.needs_fundamental
            else ()
        )
        dividends = (
            self._repository.get_dividends(
                selected, data_plan.dividend_start, trading_day
            )
            if data_plan.dividend_start is not None
            else ()
        )
        bars_by_code = self._group_bars(bars)
        fundamentals_by_code = {item.code: item for item in fundamentals}
        dividends_by_code = self._group_dividends(dividends)
        output: list[ParameterizedScreeningResult] = []
        total = len(selected)
        for index, code in enumerate(selected):
            if progress_callback is not None:
                progress_callback(
                    {
                        "done": index + 1,
                        "total": total,
                        "current_code": code,
                        "phase": "screening",
                    }
                )
            stock = stocks[code]
            assert hasattr(stock, "name")
            context = RuleContext(
                stock,  # type: ignore[arg-type]
                trading_day,
                adjustment,
                metadata,
                bars_by_code.get(code, ()),
                fundamentals_by_code.get(code),
                dividends_by_code.get(code, ()),
            )
            passed, executions = self._engine.evaluate(context, plan)
            output.append(
                ParameterizedScreeningResult(
                    code,
                    stock.name,  # type: ignore[attr-defined]
                    trading_day,
                    passed,
                    executions,
                    metadata,
                    plan.template_id,
                    plan.revision,
                )
            )
        return tuple(output)

    @staticmethod
    def _group_bars(items: Sequence[DailyBar]) -> dict[str, tuple[DailyBar, ...]]:
        grouped: dict[str, list[DailyBar]] = defaultdict(list)
        for item in items:
            grouped[item.code].append(item)
        return {
            code: tuple(sorted(values, key=lambda value: value.trading_day))
            for code, values in grouped.items()
        }

    @staticmethod
    def _group_dividends(
        items: Sequence[DividendRecord],
    ) -> dict[str, tuple[DividendRecord, ...]]:
        grouped: dict[str, list[DividendRecord]] = defaultdict(list)
        for item in items:
            grouped[item.code].append(item)
        return {code: tuple(values) for code, values in grouped.items()}
