"""Plan-driven, offline screening orchestration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    ParameterizedScreeningResult,
)
from stock_manager.protocols import LocalRepositoryProtocol
from stock_manager.rules.base import RuleContext
from stock_manager.rules.engine import RuleEngine
from stock_manager.rules.registry import RuleRegistry
from stock_manager.services.screening_data import ScreeningDataPlanner
from stock_manager.services.screening_service import (
    DatasetUnavailableError,
    StockNotFoundError,
)
from stock_manager.templates.models import ScreeningPlan


class ParameterizedScreeningService:
    def __init__(
        self,
        repository: LocalRepositoryProtocol,
        registry: RuleRegistry,
    ) -> None:
        self._repository = repository
        self._planner = ScreeningDataPlanner()
        self._engine = RuleEngine(registry)

    def screen(
        self,
        plan: ScreeningPlan,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        codes: Sequence[str] = (),
        progress_callback: Callable[[dict[str, object]], None] | None = None,
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
            context = RuleContext(
                stocks[code],
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
                    stocks[code].name,
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
