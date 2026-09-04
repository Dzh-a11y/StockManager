"""Research backtest orchestration service (P5A-8).

Bridges the screening template -> historical eligibility -> backtrader backtest
-> normalized result flow behind a persisted run state machine, executed on a
bounded single-job runner so HTTP requests never block on long runs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence
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
    PolicyOperator,
    PolicySpec,
    ResearchStrategySpec,
    TakeProfitTierSpec,
)
from stock_manager.research.strategies import (
    StrategyTemplateError,
    _policy_from_dict,
    validate_and_normalize_policies,
)
from stock_manager.rules.historical_capability import HistoricalCapabilityValidator
from stock_manager.services.historical_screening_cache import historical_cache_key
from stock_manager.services.historical_screening_executor import (
    EligibilitySnapshot,
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
        stock_codes: Sequence[str] = (),
        ignore_eligibility: bool = False,
        commission_rate: Decimal | None = None,
        stamp_duty_rate: Decimal | None = None,
        transfer_fee_rate: Decimal | None = None,
        min_commission: Decimal | None = None,
        lot_size: int | None = None,
        strategy_template_id: str | None = None,
        strategy_template_revision: int | None = None,
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
        if lot_size is not None and lot_size <= 0:
            raise ResearchBacktestError("lot_size must be positive")
        for name, value in (
            ("commission_rate", commission_rate),
            ("stamp_duty_rate", stamp_duty_rate),
            ("transfer_fee_rate", transfer_fee_rate),
            ("min_commission", min_commission),
        ):
            if value is not None and value < 0:
                raise ResearchBacktestError(f"{name} must not be negative")
        if ignore_eligibility and not stock_codes:
            raise ResearchBacktestError(
                "忽略资格模式需要至少一个股票代码"
            )
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
        spec = self._apply_run_level(
            spec,
            max_positions=max_positions,
            stock_codes=stock_codes,
            ignore_eligibility=ignore_eligibility,
            commission_rate=commission_rate,
            stamp_duty_rate=stamp_duty_rate,
            transfer_fee_rate=transfer_fee_rate,
            min_commission=min_commission,
            lot_size=lot_size,
            strategy_template_id=strategy_template_id,
            strategy_template_revision=strategy_template_revision,
        )
        registry = build_default_policy_registry()
        registry.validate_group(spec.entry_policies, PolicyKind.ENTRY)
        registry.validate_group(spec.exit_policies, PolicyKind.EXIT)
        for key, kind in (
            ("rebalance_policy", PolicyKind.REBALANCE),
            ("allocation_policy", PolicyKind.ALLOCATION),
            ("ranking_policy", PolicyKind.RANKING),
            ("execution_policy", PolicyKind.EXECUTION),
        ):
            registry.validate_spec(getattr(spec, key), kind)
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
    ) -> ResearchStrategySpec:
        """Assemble a custom strategy spec from editor-selected policies.

        Accepts both the legacy flat shape (entry/exit as single policy objects,
        P5A) and the P5C group shape (``entry``/``exit`` = ``{operator, items}``
        plus optional top-level ``take_profit_tiers``). Both are normalized to
        the canonical group payload and validated against the policy registry.
        """
        payload = dict(policies)
        alloc_raw = payload.get("allocation")
        if isinstance(alloc_raw, Mapping):
            alloc = dict(alloc_raw)
            params = dict(alloc.get("parameters") or {})
            if alloc.get("policy_id") in (
                "equal_weight_v1",
                "add_position_on_dip_v1",
            ) and "max_positions" not in params:
                # 基础数据“最大持仓”为唯一入口:参数缺省时先注入占位,
                # 随后由 _apply_run_level 以运行级数值统一覆盖。
                params["max_positions"] = 20
            alloc["parameters"] = params
            payload["allocation"] = alloc
        policies = payload
        try:
            canonical = _normalize_submit_policies(policies)
        except (StrategyTemplateError, TypeError, ValueError) as error:
            raise ResearchBacktestError(f"invalid policies: {error}") from error
        entry_items = canonical["entry"]["items"]
        exit_items = canonical["exit"]["items"]
        entry_specs = tuple(_policy_from_dict(item) for item in entry_items)
        exit_specs = tuple(_policy_from_dict(item) for item in exit_items)
        singles = {
            key: _policy_from_dict(canonical[key])
            for key in ("rebalance", "allocation", "ranking", "execution")
        }
        tiers = tuple(
            TakeProfitTierSpec(
                Decimal(str(item["take_profit_ratio"])),
                Decimal(str(item["partial_ratio"])),
            )
            for item in canonical["take_profit_tiers"]
        )
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True).encode("utf-8")
        ).hexdigest()[:10]
        return ResearchStrategySpec(
            strategy_spec_id=f"custom-{digest}",
            screening_template_id=template_id,
            screening_template_revision=template_revision,
            screening_plan_fingerprint=plan_fingerprint,
            adjustment=AdjustmentMethod.QFQ,
            evaluation_schedule=EvaluationSchedule.DAILY,
            entry_policy=entry_specs[0],
            exit_policy=exit_specs[0],
            rebalance_policy=singles["rebalance"],
            allocation_policy=singles["allocation"],
            ranking_policy=singles["ranking"],
            execution_policy=singles["execution"],
            entry_policies=entry_specs,
            exit_policies=exit_specs,
            entry_operator=PolicyOperator(canonical["entry"]["operator"]),
            exit_operator=PolicyOperator(canonical["exit"]["operator"]),
            take_profit_tiers=tiers,
            initial_cash=initial_cash,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
        )

    def _apply_run_level(
        self,
        spec: ResearchStrategySpec,
        *,
        max_positions: int,
        stock_codes: Sequence[str],
        ignore_eligibility: bool,
        commission_rate: Decimal | None,
        stamp_duty_rate: Decimal | None,
        transfer_fee_rate: Decimal | None,
        min_commission: Decimal | None,
        lot_size: int | None,
        strategy_template_id: str | None,
        strategy_template_revision: int | None,
    ) -> ResearchStrategySpec:
        """Overlay P5C run-level settings onto a built spec (base data wins)."""
        allocation = replace(
            spec.allocation_policy,
            parameters={
                **dict(spec.allocation_policy.parameters),
                "max_positions": max_positions,
            },
        )
        try:
            return replace(
                spec,
                allocation_policy=allocation,
                stock_codes=tuple(stock_codes),
                ignore_eligibility=ignore_eligibility,
                commission_rate=commission_rate,
                stamp_duty_rate=stamp_duty_rate,
                transfer_fee_rate=transfer_fee_rate,
                min_commission=min_commission,
                lot_size=lot_size,
                strategy_template_id=strategy_template_id,
                strategy_template_revision=strategy_template_revision,
            )
        except ValueError as error:
            raise ResearchBacktestError(str(error)) from error

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
        """Eligibility from cache when the cache key already succeeded.

        P5C: ignore-eligibility mode skips screening entirely (the selected
        codes are eligible every evaluation day); otherwise a non-empty code
        list narrows every day's eligible set to those codes (same PIT path as
        the full-universe run).
        """
        codes = spec.stock_codes
        code_filter: set[str] | None = set(codes) if codes else None
        if spec.ignore_eligibility:
            assert codes, "ignore_eligibility requires codes (spec invariant)"
            days = self._evaluation_days(spec)
            snapshots = tuple(
                EligibilitySnapshot(day, codes, len(codes)) for day in days
            )
            return _StaticTimeline(snapshots)
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
                    code_filter=code_filter,
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
        snapshots = _filter_snapshots(result.snapshots, code_filter)
        self._store.save_eligibility(run_id, snapshots)
        return _StaticTimeline(snapshots)

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
            run_settings_json=json.dumps(
                _run_settings_snapshot(spec), ensure_ascii=False
            ),
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
        result_rows = {
            row["run_id"]: row
            for row in self._repository.list_backtest_results(run_ids=[r.run_id for r in runs])
        }
        return tuple(
            {
                "run_id": run.run_id,
                "status": run.status.value,
                "template_id": run.template_id,
                "evaluation_start": run.evaluation_start.isoformat(),
                "evaluation_end": run.evaluation_end.isoformat(),
                "result": _run_list_item(result_rows.get(run.run_id)),
            }
            for run in runs
        )


class _CachedTimeline:
    """Eligibility timeline backed by persisted eligibility rows."""

    def __init__(
        self,
        run_id: str,
        days: tuple[tuple[date, int], ...],
        repository: Any,
        code_filter: set[str] | None = None,
    ) -> None:
        self._run_id = run_id
        self._days = days
        self._repository = repository
        self._code_filter = code_filter

    @property
    def snapshots(self) -> tuple[Any, ...]:
        from stock_manager.services.historical_screening_executor import (
            EligibilitySnapshot,
        )

        return tuple(
            EligibilitySnapshot(
                day,
                _filter_codes(
                    self._repository.list_eligible_codes(self._run_id, day),
                    self._code_filter,
                ),
                count,
            )
            for day, count in self._days
        )


class _StaticTimeline:
    """In-memory eligibility timeline (ignore-eligibility or filtered run)."""

    def __init__(self, snapshots: Sequence[Any]) -> None:
        self.snapshots = tuple(snapshots)


def _filter_codes(
    codes: Sequence[str], code_filter: set[str] | None
) -> tuple[str, ...]:
    if code_filter is None:
        return tuple(codes)
    return tuple(code for code in codes if code in code_filter)


def _filter_snapshots(
    snapshots: Sequence[Any], code_filter: set[str] | None
) -> tuple[Any, ...]:
    if code_filter is None:
        return tuple(snapshots)
    return tuple(
        EligibilitySnapshot(
            snapshot.trading_day,
            _filter_codes(snapshot.eligible_codes, code_filter),
            len(snapshot.eligible_codes),
        )
        for snapshot in snapshots
    )


def _normalize_submit_policies(
    policies: Mapping[str, object],
) -> dict[str, object]:
    """Normalize the legacy/P5C submission policies into canonical group shape."""
    def _group(raw: object) -> dict[str, object]:
        if not isinstance(raw, Mapping) or "items" not in raw:
            # legacy single policy object
            if not isinstance(raw, Mapping):
                raise StrategyTemplateError("group must be an object")
            return {"operator": "any", "items": [dict(raw)]}
        return {
            "operator": raw.get("operator", "any"),
            "items": list(raw["items"]),
        }

    payload: dict[str, object] = {}
    for kind in ("entry", "exit"):
        if kind not in policies:
            raise StrategyTemplateError(f"missing policy: {kind}")
        payload[kind] = _group(policies[kind])
    for kind in ("rebalance", "allocation", "ranking", "execution"):
        item = policies.get(kind)
        if item is None:
            raise StrategyTemplateError(f"missing policy: {kind}")
        if not isinstance(item, Mapping):
            raise StrategyTemplateError(f"{kind} policy must be an object")
        payload[kind] = dict(item)
    payload["take_profit_tiers"] = list(
        policies.get("take_profit_tiers") or ()
    )
    return validate_and_normalize_policies(payload, build_default_policy_registry())


def _run_settings_snapshot(spec: ResearchStrategySpec) -> dict[str, object]:
    """Snapshot for run history playback (P5C): run-level fields + strategy ids."""
    return {
        "template_id": spec.screening_template_id,
        "template_revision": spec.screening_template_revision,
        "strategy_template_id": spec.strategy_template_id,
        "strategy_template_revision": spec.strategy_template_revision,
        "window_start": spec.backtest_start.isoformat(),
        "window_end": spec.backtest_end.isoformat(),
        "mode": "ignore_eligibility" if spec.ignore_eligibility else "eligibility",
        "codes": list(spec.stock_codes),
        "initial_cash": str(spec.initial_cash),
        "max_positions": int(
            spec.allocation_policy.parameters.get("max_positions", 20)
        ),
        "fees": {
            "commission_rate": (
                None if spec.commission_rate is None else str(spec.commission_rate)
            ),
            "stamp_duty_rate": (
                None
                if spec.stamp_duty_rate is None
                else str(spec.stamp_duty_rate)
            ),
            "transfer_fee_rate": (
                None
                if spec.transfer_fee_rate is None
                else str(spec.transfer_fee_rate)
            ),
            "min_commission": (
                None if spec.min_commission is None else str(spec.min_commission)
            ),
            "lot_size": spec.lot_size,
        },
        "strategy": {
            "entry": {
                "operator": spec.entry_operator.value,
                "policies": [
                    {"policy_id": p.policy_id, "version": p.version}
                    for p in spec.entry_policies
                ],
            },
            "exit": {
                "operator": spec.exit_operator.value,
                "policies": [
                    {"policy_id": p.policy_id, "version": p.version}
                    for p in spec.exit_policies
                ],
            },
            "take_profit_tiers": [
                {
                    "take_profit_ratio": str(tier.take_profit_ratio),
                    "partial_ratio": str(tier.partial_ratio),
                }
                for tier in spec.take_profit_tiers
            ],
        },
    }


def _run_list_item(row: Mapping[str, object] | None) -> dict[str, object] | None:
    """Lightweight per-run summary for the run history list."""
    if row is None:
        return None
    try:
        metrics = json.loads(str(row.get("metrics_json") or "{}"))
        settings = json.loads(str(row.get("run_settings_json") or "{}"))
    except (TypeError, ValueError):
        metrics = {}
        settings = {}
    return {
        "score_start": row.get("score_start"),
        "score_end": row.get("score_end"),
        "created_at": row.get("created_at"),
        "settings": settings,
        "summary": {
            "initial_cash": metrics.get("initial_cash"),
            "final_value": metrics.get("final_value"),
            "total_return": metrics.get("total_return"),
            "trade_count": metrics.get("trade_count"),
            "total_fees": metrics.get("total_fees"),
        },
    }


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
