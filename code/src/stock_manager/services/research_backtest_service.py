"""Research backtest orchestration service (P5A-8).

Bridges the screening template -> historical eligibility -> backtrader backtest
-> normalized result flow behind a persisted run state machine, executed on a
bounded single-job runner so HTTP requests never block on long runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from stock_manager.backtest.backtrader_engine import BacktraderBacktestEngine
from stock_manager.backtest.contracts import BacktestMarketData, BacktestResult
from stock_manager.domain import (
    AdjustmentMethod,
    HistoricalRunStatus,
    HistoricalScreeningRun,
)
from stock_manager.read.historical import (
    PointInTimeRequest,
    SQLitePointInTimeReader,
)
from stock_manager.research import (
    build_default_policy_registry,
    builtin_strategy_specs,
    plan_fingerprint,
)
from stock_manager.research.models import (
    EvaluationSchedule,
    PolicyKind,
    PolicySpec,
    ResearchStrategySpec,
)
from stock_manager.rules.historical_capability import HistoricalCapabilityValidator
import hashlib
from stock_manager.services.historical_screening_cache import historical_cache_key
from stock_manager.services.historical_screening_executor import (
    HistoricalScreeningExecutor,
    HistoricalScreeningRequest,
)
from stock_manager.services.historical_screening_run_store import (
    HistoricalScreeningRunStore,
)
from stock_manager.services.job_runner import BoundedJobRunner, DuplicateJobError
from stock_manager.storage.sqlite_repo import SQLiteRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchBacktestError(RuntimeError):
    """A research backtest could not be submitted or executed."""


class ResearchBacktestService:
    """Submit and execute research backtests with eligibility caching."""

    def __init__(
        self,
        repository: SQLiteRepository,
        registry: Any,
        *,
        database_path: str,
        template_loader: Any,
        compiler: Any,
        max_workers: int = 4,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._database_path = database_path
        self._template_loader = template_loader
        self._compiler = compiler
        self._max_workers = max_workers
        self._store = HistoricalScreeningRunStore(repository, clock=clock)
        self._runner = BoundedJobRunner(max_concurrent=1)
        self._engine = BacktraderBacktestEngine()
        # 每次提交可带 max_workers(UI 可配);执行时按 run_id 取用,默认 self._max_workers
        self._run_max_workers: dict[str, int] = {}

    def recover_interrupted_runs(self) -> int:
        """Mark runs left QUEUED/active by a previous process as INTERRUPTED.

        Called once on web startup: the in-process job runner dies with the
        process, so without this, orphaned QUEUED/BUILDING_SIGNALS runs would
        linger in the UI queue forever after a restart.
        """
        return self._store.recover_interrupted("market", AdjustmentMethod.QFQ)

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------

    def submit(
        self,
        *,
        template_id: str,
        template_revision: int,
        strategy_spec_id: str | None = None,
        backtest_start: date | None = None,
        backtest_end: date | None = None,
        window_years: int | None = None,
        policies: dict[str, dict[str, object]] | None = None,
        initial_cash: Decimal,
        max_positions: int = 20,
        max_workers: int | None = None,
        dataset_id: str = "market",
        adjustment: AdjustmentMethod = AdjustmentMethod.QFQ,
    ) -> str:
        if initial_cash <= 0:
            raise ResearchBacktestError("initial_cash must be positive")
        if max_workers is not None and not 1 <= max_workers <= 16:
            raise ResearchBacktestError("max_workers must be between 1 and 16")
        if strategy_spec_id is None and policies is None:
            raise ResearchBacktestError("strategy_spec_id or policies is required")
        if window_years is not None:
            if not 1 <= window_years <= 8:
                raise ResearchBacktestError("window_years must be between 1 and 8")
            backtest_start, backtest_end = self._window_bounds(window_years)
        elif backtest_start is None or backtest_end is None:
            raise ResearchBacktestError("backtest_start/backtest_end or window_years is required")
        if backtest_start > backtest_end:
            raise ResearchBacktestError("backtest_start must not be after backtest_end")
        template = self._template_loader.get(template_id)
        loaded_revision = template.metadata.revision
        if loaded_revision != template_revision:
            raise ResearchBacktestError(
                f"template revision mismatch: expected {template_revision}, "
                f"loaded {loaded_revision}"
            )
        plan = self._compiler.compile(template)
        plan_fp = plan_fingerprint(plan, self._registry)
        if policies is not None:
            spec = self._build_custom_spec(
                policies,
                template_id=template_id,
                template_revision=template_revision,
                plan_fingerprint=plan_fp,
                backtest_start=backtest_start,
                backtest_end=backtest_end,
                initial_cash=initial_cash,
                max_positions=max_positions,
            )
        else:
            known_specs = builtin_strategy_specs(
                template_id=template_id,
                template_revision=template_revision,
                plan_fingerprint=plan_fp,
                backtest_start=backtest_start,
                backtest_end=backtest_end,
                initial_cash=initial_cash,
                max_positions=max_positions,
            )
            assert strategy_spec_id is not None
            try:
                spec = known_specs[strategy_spec_id]
            except KeyError as error:
                raise ResearchBacktestError(
                    f"unknown strategy spec: {strategy_spec_id}"
                ) from error
        build_default_policy_registry().validate_strategy(
            entry=spec.entry_policy,
            exit=spec.exit_policy,
            rebalance=spec.rebalance_policy,
            allocation=spec.allocation_policy,
            ranking=spec.ranking_policy,
            execution=spec.execution_policy,
        )
        now = self._store._now()
        run_id = f"rb-{uuid.uuid4().hex[:12]}"
        self._run_max_workers[run_id] = (
            max_workers if max_workers is not None else self._max_workers
        )
        cache_key = historical_cache_key(
            dataset_id=dataset_id,
            generation=self._committed_generation(dataset_id, adjustment),
            adjustment=adjustment,
            universe_policy="pit_as_of",
            evaluation_start=backtest_start,
            evaluation_end=backtest_end,
            schedule=spec.evaluation_schedule,
            template_id=template_id,
            template_revision=template_revision,
            plan_fingerprint=plan_fp,
            rule_implementation_version="builtin-v1",
            calendar_fingerprint=self._calendar_fingerprint(dataset_id),
        )
        run = HistoricalScreeningRun(
            run_id=run_id,
            cache_key=cache_key,
            dataset_id=dataset_id,
            adjustment=adjustment,
            generation=self._committed_generation(dataset_id, adjustment),
            template_id=template_id,
            template_revision=template_revision,
            plan_fingerprint=plan_fp,
            rule_implementation_version="builtin-v1",
            universe_policy="pit_as_of",
            evaluation_start=backtest_start,
            evaluation_end=backtest_end,
            status=HistoricalRunStatus.QUEUED,
            progress_completed=0,
            progress_total=4,
            started_at=now,
            finished_at=None,
            error_message=None,
        )
        self._store.create(run)
        try:
            self._runner.submit(
                f"backtest:{run_id}",
                lambda: self._execute(
                    run_id, plan, spec, dataset_id=dataset_id, adjustment=adjustment
                ),
            )
        except DuplicateJobError:
            raise ResearchBacktestError("a backtest job is already running")
        return run_id

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # custom strategy assembly / window bounds
    # ------------------------------------------------------------------

    def _build_custom_spec(
        self,
        policies: dict[str, dict[str, object]],
        *,
        template_id: str,
        template_revision: int,
        plan_fingerprint: str,
        backtest_start: date,
        backtest_end: date,
        initial_cash: Decimal,
        max_positions: int,
    ) -> ResearchStrategySpec:
        """Assemble a custom strategy spec from six editor-selected policies."""
        kind_map = {
            "entry": PolicyKind.ENTRY,
            "exit": PolicyKind.EXIT,
            "rebalance": PolicyKind.REBALANCE,
            "allocation": PolicyKind.ALLOCATION,
            "ranking": PolicyKind.RANKING,
            "execution": PolicyKind.EXECUTION,
        }
        registry = build_default_policy_registry()
        selected: dict[str, PolicySpec] = {}
        for key, kind in kind_map.items():
            item = policies.get(key)
            if item is None or not isinstance(item, dict):
                raise ResearchBacktestError(f"missing policy: {key}")
            try:
                policy = PolicySpec(
                    str(item["policy_id"]),
                    int(item["version"]),
                    dict(item.get("parameters", {})),
                )
            except (KeyError, ValueError, TypeError) as error:
                raise ResearchBacktestError(f"invalid policy {key}: {error}") from error
            registry.validate_spec(policy, kind)
            selected[key] = policy
        digest = hashlib.sha256(
            json.dumps(policies, sort_keys=True).encode("utf-8")
        ).hexdigest()[:10]
        return ResearchStrategySpec(
            strategy_spec_id=f"custom-{digest}",
            screening_template_id=template_id,
            screening_template_revision=template_revision,
            screening_plan_fingerprint=plan_fingerprint,
            adjustment=AdjustmentMethod.QFQ,
            evaluation_schedule=EvaluationSchedule.DAILY,
            entry_policy=selected["entry"],
            exit_policy=selected["exit"],
            rebalance_policy=selected["rebalance"],
            allocation_policy=selected["allocation"],
            ranking_policy=selected["ranking"],
            execution_policy=selected["execution"],
            initial_cash=initial_cash,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
        )

    def _window_bounds(self, years: int) -> tuple[date, date]:
        """Resolve [end - N years, end] from the local trading calendar.

        End = latest completed trading day of the dataset; start = N x 260
        trading days back (same convention as the eight-year target).
        """
        latest = self._repository.get_latest_dataset_metadata(
            "market", AdjustmentMethod.QFQ
        )
        if latest is None:
            raise ResearchBacktestError("本地数据集不可用,无法计算回测窗口")
        end = latest.trading_day
        days = tuple(self._repository.get_trading_days(date.min, end))
        from stock_manager.sync.history_plan import trading_day_lookback

        start = trading_day_lookback(days, end, years * 260)
        return start, end

    def _execute(
        self,
        run_id: str,
        plan: Any,
        spec: ResearchStrategySpec,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> None:
        store = self._store
        try:
            store.start_validating(run_id)
            if self._runner.should_cancel(f"backtest:{run_id}"):
                store.request_cancel(run_id)
                store.finish_cancelled(run_id)
                return
            # 数据校验:generation 绑定与规则能力
            with SQLitePointInTimeReader(
                self._repository.database_path,
                PointInTimeRequest(dataset_id, (), spec.backtest_start, spec.backtest_end, adjustment),
            ) as reader:
                committed = reader.committed_generation()
                run = store.get(run_id)
                if run is not None and run.generation is not None and committed != run.generation:
                    raise ResearchBacktestError(
                        f"dataset generation changed: bound {run.generation}, "
                        f"committed {committed}"
                    )
                HistoricalCapabilityValidator(self._registry).require_ready(
                    plan, reader, dataset_id=dataset_id, adjustment=adjustment
                )
                calendar_fp = self._calendar_fingerprint(dataset_id)
            store.start_building(run_id)
            if self._runner.should_cancel(f"backtest:{run_id}"):
                store.request_cancel(run_id)
                store.finish_cancelled(run_id)
                return
            eligibility = self._build_eligibility(
                run_id, plan, spec, dataset_id=dataset_id, adjustment=adjustment
            )
            store.start_backtest(run_id)
            if self._runner.should_cancel(f"backtest:{run_id}"):
                store.request_cancel(run_id)
                store.finish_cancelled(run_id)
                return
            market_data = self._load_market_data(spec, dataset_id, adjustment)
            result = self._engine.run(spec, eligibility, market_data)
            store.start_normalizing(run_id)
            self._persist_result(run_id, spec, result)
            store.succeed(run_id, 4, 4)
        except Exception as error:
            store.fail(run_id, str(error))

    def _build_eligibility(
        self,
        run_id: str,
        plan: Any,
        spec: ResearchStrategySpec,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> Any:
        """Eligibility from cache when the cache key already succeeded."""
        run = self._store.get(run_id)
        cache_key = "" if run is None else run.cache_key
        cached = self._store.find_cached(cache_key)
        if cached is not None:
            days = self._repository.list_eligibility_days(cached.run_id)
            if days:
                return _CachedTimeline(
                    cached.run_id,
                    days,
                    self._repository,
                )
        request = HistoricalScreeningRequest(
            dataset_id=dataset_id,
            adjustment=adjustment,
            generation=self._committed_generation(dataset_id, adjustment),
            warmup_start=_warmup_start(spec.backtest_start),
            score_start=spec.backtest_start,
            score_end=spec.backtest_end,
            evaluation_days=self._evaluation_days(spec),
        )
        executor = HistoricalScreeningExecutor(
            self._registry,
            database_path=str(self._repository.database_path),
            max_workers=self._run_max_workers.pop(run_id, self._max_workers),
        )
        from stock_manager.read.plan_view import PicklableScreeningPlan

        result = executor.execute(PicklableScreeningPlan.from_plan(plan), request)
        self._store.save_eligibility(run_id, result.snapshots)
        return result

    def _load_market_data(
        self,
        spec: ResearchStrategySpec,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> BacktestMarketData:
        with SQLitePointInTimeReader(
            self._repository.database_path,
            PointInTimeRequest(
                dataset_id, (), _warmup_start(spec.backtest_start), spec.backtest_end, adjustment
            ),
        ) as reader:
            trading_days = reader.trading_days(
                _warmup_start(spec.backtest_start), spec.backtest_end
            )
            bars = reader.bars_through(spec.backtest_end)
            stocks = tuple(
                item
                for _as_of, items in reader.all_universe_snapshots()
                for item in items
            )
        return BacktestMarketData(dataset_id, adjustment, trading_days, bars, stocks)

    def _evaluation_days(self, spec: ResearchStrategySpec) -> tuple[date, ...]:
        with SQLitePointInTimeReader(
            self._repository.database_path,
            PointInTimeRequest(
                spec.screening_template_id,
                (),
                _warmup_start(spec.backtest_start),
                spec.backtest_end,
                spec.adjustment,
            ),
        ) as reader:
            days = reader.trading_days(spec.backtest_start, spec.backtest_end)
        return tuple(days)

    def _committed_generation(self, dataset_id: str, adjustment: AdjustmentMethod) -> str | None:
        with SQLitePointInTimeReader(
            self._repository.database_path,
            PointInTimeRequest(dataset_id, (), date.min, date.max, adjustment),
        ) as reader:
            return reader.committed_generation()

    def _calendar_fingerprint(self, dataset_id: str) -> str:
        with SQLitePointInTimeReader(
            self._repository.database_path,
            PointInTimeRequest(dataset_id, (), date.min, date.max, AdjustmentMethod.QFQ),
        ) as reader:
            return reader.data_fingerprint()

    def _persist_result(
        self, run_id: str, spec: ResearchStrategySpec, result: BacktestResult
    ) -> None:
        self._repository.save_backtest_result(
            run_id=run_id,
            spec_id=result.spec_id,
            plan_fingerprint=result.screening_plan_fingerprint,
            adjustment=result.adjustment,
            score_start=result.score_start,
            score_end=result.score_end,
            metrics_json=json.dumps(
                _metrics_to_jsonable(result.metrics), ensure_ascii=False
            ),
            warnings_json=json.dumps(list(result.warnings), ensure_ascii=False),
            provenance_json=json.dumps(result.provenance, ensure_ascii=False),
            created_at=self._store._now(),
        )
        self._repository.save_backtest_orders(run_id, result.trades)
        self._repository.save_backtest_equity(run_id, result.equity_curve)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def status(self, run_id: str) -> dict[str, object] | None:
        run = self._store.get(run_id)
        if run is None:
            return None
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "progress_completed": run.progress_completed,
            "progress_total": run.progress_total,
            "started_at": run.started_at.isoformat(),
            "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
            "error_message": run.error_message,
        }

    def cancel(self, run_id: str) -> bool:
        try:
            self._runner.cancel(f"backtest:{run_id}")
        except Exception:
            return False
        return True

    def equity(self, run_id: str, *, offset: int = 0, limit: int = 500) -> tuple[dict[str, object], ...]:
        return self._repository.list_backtest_equity(run_id, offset=offset, limit=limit)

    def orders(self, run_id: str, *, offset: int = 0, limit: int = 100) -> tuple[dict[str, object], ...]:
        return self._repository.list_backtest_orders(run_id, offset=offset, limit=limit)

    def result(self, run_id: str) -> dict[str, object] | None:
        return self._repository.get_backtest_result(run_id)

    def list_runs(self, *, offset: int = 0, limit: int = 20) -> tuple[dict[str, object], ...]:
        runs = self._store.list("market", AdjustmentMethod.QFQ, offset=offset, limit=limit)
        return tuple(
            {
                "run_id": run.run_id,
                "status": run.status.value,
                "template_id": run.template_id,
                "evaluation_start": run.evaluation_start.isoformat(),
                "evaluation_end": run.evaluation_end.isoformat(),
            }
            for run in runs
        )


class _CachedTimeline:
    """Eligibility timeline backed by persisted eligibility rows."""

    def __init__(self, run_id: str, days: tuple[tuple[date, int], ...], repository: Any) -> None:
        self._run_id = run_id
        self._days = days
        self._repository = repository

    @property
    def snapshots(self) -> tuple[Any, ...]:
        from stock_manager.services.historical_screening_executor import (
            EligibilitySnapshot,
        )

        return tuple(
            EligibilitySnapshot(
                day,
                self._repository.list_eligible_codes(self._run_id, day),
                count,
            )
            for day, count in self._days
        )


def _warmup_start(score_start: date) -> date:
    from datetime import timedelta

    return score_start - timedelta(days=400)


def _metrics_to_jsonable(metrics: Any) -> dict[str, object]:
    return {
        "initial_cash": str(metrics.initial_cash),
        "final_value": str(metrics.final_value),
        "total_return": (
            None if metrics.total_return is None else str(metrics.total_return)
        ),
        "annualized_return": (
            None if metrics.annualized_return is None else str(metrics.annualized_return)
        ),
        "max_drawdown": (
            None if metrics.max_drawdown is None else str(metrics.max_drawdown)
        ),
        "sharpe": None if metrics.sharpe is None else str(metrics.sharpe),
        "trade_count": metrics.trade_count,
        "win_count": metrics.win_count,
        "loss_count": metrics.loss_count,
        "total_fees": str(metrics.total_fees),
        "unavailable": list(metrics.unavailable),
    }
