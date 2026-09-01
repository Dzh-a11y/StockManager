"""Deterministic synchronization planning for the P5 DataSync reconstruction.

The planner converts a target window, local published coverage, the trading
calendar and (for REPAIR) a structured VerificationReport into a stable,
ordered list of SyncTasks. It never calls a Provider, never writes market
data and never judges data correctness: it only consumes evidence and emits
a deterministic plan (P5-RD-2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    Repairability,
    SyncPlan,
    SyncPlanMode,
    SyncPlanStatus,
    SyncSource,
    SyncTask,
    SyncTaskStatus,
    VerificationIssue,
    VerificationReport,
)

#: Data types the planner understands, in a stable dependency order.
DATA_TYPE_ORDER: tuple[str, ...] = ("stocks", "daily_bars", "fundamentals", "dividends")

UNIVERSE_POLICY_DEFAULT = "a-share"


class PlanRejectedError(ValueError):
    """A plan request is not plannable (manual repair, corrupt input, ...)."""


@dataclass(frozen=True, slots=True)
class PlanInput:
    """Normalized inputs that fully determine a sync plan (P5 section 4.2)."""

    mode: SyncPlanMode
    source: SyncSource
    dataset_id: str
    adjustment: AdjustmentMethod
    universe_policy: str
    target_start: date
    target_end: date
    required_data_types: tuple[str, ...]
    planner_version: str = "p5-rd2-2"
    batch_size: int = 20

    def __post_init__(self) -> None:
        if self.target_start > self.target_end:
            raise ValueError("target_start must not be after target_end")
        if not self.required_data_types:
            raise ValueError("required_data_types must not be empty")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        unknown = set(self.required_data_types) - set(DATA_TYPE_ORDER)
        if unknown:
            raise ValueError(f"unknown data types: {sorted(unknown)}")
        normalized = tuple(
            t for t in DATA_TYPE_ORDER if t in self.required_data_types
        )
        if len(normalized) != len(set(self.required_data_types)):
            raise ValueError("required_data_types must not contain duplicates")
        object.__setattr__(self, "required_data_types", normalized)

    @property
    def fingerprint(self) -> str:
        """Deterministic digest over every normalized input field."""
        digest = hashlib.sha256()
        parts = (
            self.mode.value,
            self.source.value,
            self.dataset_id,
            self.adjustment.value,
            self.universe_policy,
            self.target_start.isoformat(),
            self.target_end.isoformat(),
            ",".join(self.required_data_types),
            self.planner_version,
            str(self.batch_size),
        )
        digest.update("|".join(parts).encode("utf-8"))
        return digest.hexdigest()

    @property
    def plan_id(self) -> str:
        """Deterministic plan identity (stable prefix of the fingerprint)."""
        return f"plan-{self.fingerprint[:16]}"


@dataclass(frozen=True, slots=True)
class PlannedOutput:
    """A deterministic plan plus its ordered tasks and candidate."""

    plan: SyncPlan
    tasks: tuple[SyncTask, ...]
    candidate: CandidateGeneration


class SyncPlanner:
    """Builds deterministic SyncPlans from coverage evidence (P5-RD-2)."""

    def __init__(
        self,
        *,
        calendar: Callable[[date, date], Sequence[date]],
        coverage: Callable[[AdjustmentMethod, str], tuple[date | None, date | None]],
        universe_codes: Callable[[date], Sequence[str]],
        now: Callable[[], datetime],
    ) -> None:
        self._calendar = calendar
        self._coverage = coverage
        self._universe_codes = universe_codes
        self._now = now

    # -- public entry points -------------------------------------------------

    def plan_bootstrap(
        self,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
        universe_policy: str = UNIVERSE_POLICY_DEFAULT,
        required_data_types: Sequence[str] = DATA_TYPE_ORDER,
        batch_size: int = 20,
    ) -> PlannedOutput:
        """Plan a full first-generation build from an external source."""
        input_ = PlanInput(
            SyncPlanMode.BOOTSTRAP,
            SyncSource.BAOSTOCK,
            dataset_id,
            adjustment,
            universe_policy,
            target_start,
            target_end,
            tuple(required_data_types),
            batch_size=batch_size,
        )
        return self._emit(input_, parent_generation=None)

    def plan_incremental(
        self,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        active_generation: str,
        coverage_end: date,
        target_end: date,
        universe_policy: str = UNIVERSE_POLICY_DEFAULT,
        required_data_types: Sequence[str] = DATA_TYPE_ORDER,
        batch_size: int = 20,
    ) -> PlannedOutput:
        """Plan tail catch-up after the current active generation.

        The planned window starts at the first trading day strictly after
        ``coverage_end`` (never a natural-day guess) and runs to
        ``target_end``.
        """
        if coverage_end >= target_end:
            raise PlanRejectedError(
                "coverage_end already reaches target_end; nothing to plan"
            )
        tail_days = tuple(
            day
            for day in sorted(self._calendar(coverage_end, target_end))
            if day > coverage_end
        )
        if not tail_days:
            raise PlanRejectedError(
                "no trading day after coverage_end in the target window"
            )
        input_ = PlanInput(
            SyncPlanMode.INCREMENTAL,
            SyncSource.BAOSTOCK,
            dataset_id,
            adjustment,
            universe_policy,
            tail_days[0],
            target_end,
            tuple(required_data_types),
            batch_size=batch_size,
        )
        return self._emit(input_, parent_generation=active_generation)

    def plan_repair(
        self,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        report: VerificationReport,
        active_generation: str,
        universe_policy: str = UNIVERSE_POLICY_DEFAULT,
    ) -> PlannedOutput:
        """Convert a VerificationReport into a deterministic REPAIR plan.

        Only issues whose ``repairability`` is ``REFETCH`` become provider
        tasks. ``MANUAL`` and ``REBUILD`` issues never reach the provider;
        corrupt/SHA/schema problems raise ``PlanRejectedError`` so the caller
        treats them as REJECTED, not as a normal refetch.
        """
        refetchable = tuple(
            issue
            for issue in report.issues
            if issue.repairability is Repairability.REFETCH
        )
        manual = tuple(
            issue
            for issue in report.issues
            if issue.repairability is Repairability.MANUAL
        )
        if manual:
            raise PlanRejectedError(
                "verification report contains MANUAL issues that must be "
                "resolved by hand before planning"
            )
        rebuild = tuple(
            issue
            for issue in report.issues
            if issue.repairability is Repairability.REBUILD
        )
        if rebuild:
            raise PlanRejectedError(
                "verification report contains REBUILD issues; a plain provider "
                "refetch cannot repair them"
            )
        if not refetchable:
            raise PlanRejectedError(
                "verification report has no REFETCH-able issues"
            )
        start = min(date.fromisoformat(i.partition_key) for i in refetchable)
        end = max(date.fromisoformat(i.partition_key) for i in refetchable)
        data_types = tuple(sorted({i.data_type for i in refetchable}))
        input_ = PlanInput(
            SyncPlanMode.REPAIR,
            SyncSource.BAOSTOCK,
            dataset_id,
            adjustment,
            universe_policy,
            start,
            end,
            data_types,
        )
        tasks = self._repair_tasks(input_.plan_id, refetchable)
        plan, candidate = self._plan_and_candidate(
            input_,
            parent_generation=active_generation,
            task_count=len(tasks),
        )
        return PlannedOutput(plan, tasks, candidate)
    def plan_legacy_import(
        self,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
        universe_policy: str = UNIVERSE_POLICY_DEFAULT,
        required_data_types: Sequence[str] = DATA_TYPE_ORDER,
        batch_size: int = 20,
    ) -> PlannedOutput:
        """Plan a one-time import of the legacy shared tables."""
        input_ = PlanInput(
            SyncPlanMode.LEGACY_IMPORT,
            SyncSource.LEGACY_DATABASE,
            dataset_id,
            adjustment,
            universe_policy,
            target_start,
            target_end,
            tuple(required_data_types),
            batch_size=batch_size,
        )
        return self._emit(input_, parent_generation=None)

    # -- internal helpers ----------------------------------------------------

    def _emit(
        self,
        input_: PlanInput,
        *,
        parent_generation: str | None,
    ) -> PlannedOutput:
        tasks = self._window_tasks(input_)
        plan, candidate = self._plan_and_candidate(
            input_, parent_generation=parent_generation, task_count=len(tasks)
        )
        return PlannedOutput(plan, tasks, candidate)

    def _plan_and_candidate(
        self,
        input_: PlanInput,
        *,
        parent_generation: str | None,
        task_count: int = 0,
    ) -> tuple[SyncPlan, CandidateGeneration]:
        now = self._now()
        candidate_id = f"cand-{input_.fingerprint[:16]}-{now.strftime('%Y%m%d%H%M%S%f')}"
        candidate = CandidateGeneration(
            candidate_generation_id=candidate_id,
            plan_id=input_.plan_id,
            parent_generation=parent_generation,
            write_revision=0,
            status=CandidateGenerationStatus.PLANNED,
            created_at=now,
            updated_at=now,
        )
        plan = SyncPlan(
            plan_id=input_.plan_id,
            plan_version=1,
            mode=input_.mode,
            source=input_.source,
            dataset_id=input_.dataset_id,
            adjustment=input_.adjustment,
            universe_policy=input_.universe_policy,
            target_start=input_.target_start,
            target_end=input_.target_end,
            latest_completed_trading_day=None,
            parent_generation=parent_generation,
            candidate_generation_id=candidate.candidate_generation_id,
            required_data_types=input_.required_data_types,
            task_count=task_count,
            plan_fingerprint=input_.fingerprint,
            status=SyncPlanStatus.PLANNED,
            created_at=now,
            updated_at=now,
        )
        return plan, candidate

    def _window_tasks(self, input_: PlanInput) -> tuple[SyncTask, ...]:
        """Generate batch-granularity tasks over the target window.

        Batch granularity (P5 v2-equivalent, not per-stock-per-day): the
        universe is sorted and chunked by ``batch_size``; ``daily_bars`` gets
        one task per code batch covering the whole target range (one serial
        request per batch, like the legacy v2 path), ``fundamentals`` one task
        per code batch with ``as_of = target_end``, and ``stocks`` one task
        per day (snapshot semantics). For an 8-year window over ~5,214 stocks
        this yields ~261 tasks per data type instead of ~10 million.
        """
        days = tuple(
            sorted(self._calendar(input_.target_start, input_.target_end))
        )
        if not days:
            raise PlanRejectedError(
                "trading calendar is empty for the target window"
            )
        batch_size = input_.batch_size
        tasks: list[SyncTask] = []
        seq = 0
        universe_cache: dict[date, tuple[str, ...]] = {}
        # 全窗口统一使用 target_end 的股票池(与 v2 一致:历史日无独立快照,
        # 只有终点快照)。daily_bars/fundamentals/stocks 共用这一代码集。
        universe_day = input_.target_end
        universe = universe_cache.get(universe_day)
        if universe is None:
            universe = tuple(sorted(self._universe_codes(universe_day)))
            universe_cache[universe_day] = universe
        if not universe:
            raise PlanRejectedError(
                f"stock universe is empty for {universe_day.isoformat()}; "
                "cannot plan tasks"
            )
        for data_type in DATA_TYPE_ORDER:
            if data_type not in input_.required_data_types:
                continue
            if data_type == "stocks":
                # 快照语义:只拉 target_end 一次(历史日无独立快照,复用终点
                # 股票池)。一个任务覆盖整个窗口的股票快照,避免每交易日
                # 一次全市场请求。
                tasks.append(
                    self._make_task(
                        input_, seq, data_type, input_.target_end.isoformat(),
                        universe, input_.target_end, input_.target_end,
                    )
                )
                seq += 1
                continue
            # daily_bars / fundamentals:按代码分批,区间为整个目标窗口
            # (daily_bars)或 as_of=target_end(fundamentals)。
            codes = universe
            for offset in range(0, len(codes), batch_size):
                chunk = codes[offset : offset + batch_size]
                if not chunk:
                    continue
                if data_type == "daily_bars":
                    tasks.append(
                        self._make_task(
                            input_, seq, data_type,
                            f"{input_.target_start.isoformat()}..{input_.target_end.isoformat()}",
                            chunk, input_.target_start, input_.target_end,
                        )
                    )
                else:  # fundamentals
                    tasks.append(
                        self._make_task(
                            input_, seq, data_type,
                            input_.target_end.isoformat(),
                            chunk, input_.target_end, input_.target_end,
                        )
                    )
                seq += 1
        return tuple(tasks)

    @staticmethod
    def _make_task(
        input_: PlanInput,
        seq: int,
        data_type: str,
        partition_key: str,
        codes: tuple[str, ...],
        range_start: date,
        range_end: date,
    ) -> SyncTask:
        return SyncTask(
            task_id=f"{input_.plan_id}:{seq:05d}",
            plan_id=input_.plan_id,
            sequence_no=seq,
            data_type=data_type,
            partition_key=partition_key,
            codes=codes,
            range_start=range_start,
            range_end=range_end,
            dependencies=(),
            status=SyncTaskStatus.PENDING,
            attempt_count=0,
            not_before=None,
            row_count=None,
            error_code=None,
            error_message=None,
            started_at=None,
            finished_at=None,
        )

    def _repair_tasks(
        self,
        plan_id: str,
        issues: Sequence[VerificationIssue],
    ) -> tuple[SyncTask, ...]:
        """Convert REFETCH-able issues into deduplicated, stably-sorted tasks.

        Issues are deduplicated by (data_type, partition_key, sorted codes)
        and ordered by (data_type, partition_key, codes).
        """
        grouped: dict[tuple[str, str, tuple[str, ...]], VerificationIssue] = {}
        for issue in issues:
            key = (issue.data_type, issue.partition_key, tuple(sorted(issue.codes)))
            grouped.setdefault(key, issue)
        ordered = sorted(
            grouped.values(),
            key=lambda i: (i.data_type, i.partition_key, tuple(sorted(i.codes))),
        )
        tasks: list[SyncTask] = []
        for seq, issue in enumerate(ordered):
            day = date.fromisoformat(issue.partition_key)
            tasks.append(
                SyncTask(
                    task_id=f"{plan_id}:repair:{seq:05d}",
                    plan_id=plan_id,
                    sequence_no=seq,
                    data_type=issue.data_type,
                    partition_key=issue.partition_key,
                    codes=tuple(sorted(issue.codes)),
                    range_start=day,
                    range_end=day,
                    dependencies=(),
                    status=SyncTaskStatus.PENDING,
                    attempt_count=0,
                    not_before=None,
                    row_count=None,
                    error_code=None,
                    error_message=None,
                    started_at=None,
                    finished_at=None,
                )
            )
        return tuple(tasks)
