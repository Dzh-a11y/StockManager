"""End-to-end sync pipeline orchestrator (P5 DataSync integration).

``SyncPipeline`` executes a deterministic SyncPlan through the P5 components:
persist plan/tasks/candidate, run each task through the SerialFetchWorker,
stage rows via StagingWriter, verify staged data, and atomically publish the
generation. It owns the plan/task lifecycle: retries respect cooldown,
interrupted runs resume from incomplete tasks, and verification issues close
the loop back to a REPAIR plan. The pipeline never calls the provider
directly — it only talks to the worker — and never touches published tables
except through the committer's publish transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    SyncPlan,
    SyncPlanMode,
    SyncPlanStatus,
    SyncSource,
    SyncTask,
    SyncTaskStatus,
)
from stock_manager.sync.committer import GenerationCommitter, ReadinessGate
from stock_manager.sync.legacy import LegacyImporter
from stock_manager.sync.planner import PlannedOutput, SyncPlanner
from stock_manager.sync.staging import StagingWriter
from stock_manager.sync.verifier import CoverageVerifier
from stock_manager.sync.worker import (
    ProviderFetchError,
    SerialFetchWorker,
    TaskExecutionResult,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot proceed."""


class RetryCooldownError(RuntimeError):
    """Raised when an explicit retry is attempted before cooldown expires."""


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """Result of one pipeline run for a plan."""

    plan_id: str
    plan_status: SyncPlanStatus
    candidate: CandidateGeneration | None
    task_statuses: tuple[tuple[str, SyncTaskStatus], ...]
    published: bool
    report_issues: int
    warning: str | None


class SyncPipeline:
    """Coordinates the P5 components for one plan's execution (P5-RD integration)."""

    def __init__(
        self,
        *,
        repository: object,
        planner: SyncPlanner,
        worker_factory: Callable[[], SerialFetchWorker],
        staging: StagingWriter,
        verifier: CoverageVerifier,
        committer: GenerationCommitter,
        gate: ReadinessGate,
        legacy: LegacyImporter,
        now: Callable[[], datetime],
        retry_cooldown: timedelta = timedelta(minutes=5),
        max_attempts: int = 3,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._worker_factory = worker_factory
        self._staging = staging
        self._verifier = verifier
        self._committer = committer
        self._gate = gate
        self._legacy = legacy
        self._now = now
        self._retry_cooldown = retry_cooldown
        self._max_attempts = max_attempts

    # -- public entry points --------------------------------------------------

    def plan(
        self,
        *,
        mode: SyncPlanMode,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
        required_data_types: Sequence[str] = ("stocks", "daily_bars", "fundamentals"),
        batch_size: int = 20,
    ) -> PlannedOutput:
        """Create and persist a plan (plus tasks and candidate).

        Idempotent: when a plan with the same deterministic ``plan_id``
        already exists, it is returned unchanged (its tasks/candidate are
        kept) so a watchdog restart never resets progress to zero. Only a
        brand-new plan is persisted.
        """
        existing = self._repository.get_sync_plan(
            self._planner_fingerprint_plan_id(
                mode, dataset_id, adjustment, target_start, target_end,
                tuple(required_data_types), batch_size,
            )
        )
        if existing is not None:
            tasks = self._repository.list_sync_tasks(existing.plan_id)
            candidate = self._repository.get_candidate_generation(
                existing.candidate_generation_id
            )
            if candidate is None:
                raise PipelineError(
                    f"candidate missing for existing plan {existing.plan_id}"
                )
            return PlannedOutput(existing, tuple(tasks), candidate)
        if mode is SyncPlanMode.BOOTSTRAP:
            output = self._planner.plan_bootstrap(
                dataset_id=dataset_id,
                adjustment=adjustment,
                target_start=target_start,
                target_end=target_end,
                required_data_types=tuple(required_data_types),
                batch_size=batch_size,
            )
        elif mode is SyncPlanMode.LEGACY_IMPORT:
            output = self._planner.plan_legacy_import(
                dataset_id=dataset_id,
                adjustment=adjustment,
                target_start=target_start,
                target_end=target_end,
                required_data_types=tuple(required_data_types),
                batch_size=batch_size,
            )
        elif mode is SyncPlanMode.INCREMENTAL:
            active = self._repository.get_active_generation(dataset_id, adjustment)
            if active is None:
                raise PipelineError("INCREMENTAL requires an active generation")
            output = self._planner.plan_incremental(
                dataset_id=dataset_id,
                adjustment=adjustment,
                active_generation=active.generation,
                coverage_end=target_start,
                target_end=target_end,
                required_data_types=tuple(required_data_types),
                batch_size=batch_size,
            )
        else:
            raise PipelineError(f"cannot plan mode {mode.value} directly")
        self._persist_plan(output)
        return output

    def execute(self, plan_id: str) -> PipelineRun:
        """Execute an existing plan's PENDING tasks and publish when verified."""
        plan = self._repository.get_sync_plan(plan_id)
        if plan is None:
            raise PipelineError(f"plan not found: {plan_id}")
        if plan.status is SyncPlanStatus.SUCCEEDED:
            return PipelineRun(
                plan_id, plan.status, None, (), False, 0,
                "plan already succeeded; skipping",
            )
        self._repository.update_sync_plan_status(
            plan_id, SyncPlanStatus.RUNNING, self._now()
        )
        candidate = self._repository.get_candidate_generation(
            plan.candidate_generation_id
        )
        if candidate is None:
            raise PipelineError(
                f"candidate missing for plan {plan_id}"
            )
        try:
            result = self._run_tasks(plan, candidate)
            if result.warning is not None:
                return result
            published = self._verify_and_publish(plan, candidate)
            self._repository.update_sync_plan_status(
                plan_id, SyncPlanStatus.SUCCEEDED, self._now()
            )
            final_candidate = self._repository.get_candidate_generation(
                candidate.candidate_generation_id
            )
            return PipelineRun(
                plan_id,
                SyncPlanStatus.SUCCEEDED,
                final_candidate,
                result.task_statuses,
                published,
                result.report_issues,
                None,
            )
        except Exception:
            self._repository.update_sync_plan_status(
                plan_id, SyncPlanStatus.FAILED, self._now()
            )
            raise

    def retry(self, plan_id: str) -> PipelineRun:
        """Explicitly reset FAILED/NEEDS_REPAIR tasks subject to cooldown."""
        plan = self._repository.get_sync_plan(plan_id)
        if plan is None:
            raise PipelineError(f"plan not found: {plan_id}")
        now = self._now()
        failed = self._repository.tasks_by_status(
            plan_id, (SyncTaskStatus.FAILED, SyncTaskStatus.INTERRUPTED)
        )
        pending_repair = False
        candidate = self._repository.get_candidate_generation(
            plan.candidate_generation_id
        )
        if candidate is not None and candidate.status is CandidateGenerationStatus.NEEDS_REPAIR:
            pending_repair = True
        if not failed and not pending_repair:
            raise PipelineError("nothing to retry for this plan")
        for task in failed:
            if (
                task.not_before is not None
                and task.not_before > now
            ):
                raise RetryCooldownError(
                    f"task {task.task_id} cooldown until {task.not_before.isoformat()}"
                )
            if task.attempt_count >= self._max_attempts:
                raise PipelineError(
                    f"task {task.task_id} exhausted {self._max_attempts} attempts"
                )
        for task in failed:
            self._repository.update_sync_task_status(
                replace(task, status=SyncTaskStatus.PENDING, error_message=None)
            )
        if pending_repair and candidate is not None:
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.WRITING,
                now,
            )
        return self.execute(plan_id)

    def mark_interrupted(self) -> int:
        """Mark RUNNING plans/tasks INTERRUPTED (call after process restart)."""
        count = 0
        for plan in self._repository.list_sync_plans(
            "market", AdjustmentMethod.QFQ
        ):
            if plan.status is SyncPlanStatus.RUNNING:
                self._repository.update_sync_plan_status(
                    plan.plan_id, SyncPlanStatus.PLANNED, self._now()
                )
            for task in self._repository.tasks_by_status(
                plan.plan_id, (SyncTaskStatus.RUNNING,)
            ):
                self._repository.update_sync_task_status(
                    replace(
                        task,
                        status=SyncTaskStatus.INTERRUPTED,
                        error_message="interrupted by process restart",
                        finished_at=self._now(),
                    )
                )
                count += 1
        return count

    # -- internals ------------------------------------------------------------

    def _on_fetch_progress(
        self, task: SyncTask, event: dict[str, object]
    ) -> None:
        """Provider 逐码进度 → 写回任务 progress_json(批次内进度条)。"""
        index = int(event.get("index", 0) or 0)
        total = int(event.get("total", 0) or 0)
        code = str(event.get("current_code", ""))
        self._persist_task_progress(
            task, completed=index, total=total or len(task.codes),
            current_code=code,
        )

    def _persist_task_progress(
        self,
        task: SyncTask,
        *,
        completed: int,
        total: int,
        current_code: str,
    ) -> None:
        """Persist live per-task progress so the Web page can show it."""
        import json as _json

        from datetime import datetime as _datetime
        from zoneinfo import ZoneInfo as _ZoneInfo

        payload = _json.dumps(
            {
                "data_type": task.data_type,
                "partition_key": task.partition_key,
                "completed": completed,
                "total": total,
                "current_code": current_code,
                "status": task.status.value,
                "updated_at": _datetime.now(
                    _ZoneInfo("Asia/Shanghai")
                ).isoformat(),
            },
            ensure_ascii=False,
        )
        try:
            self._repository.update_task_progress(task.task_id, payload)
        except Exception:
            pass  # 进度持久化失败不影响同步主流程

    def _planner_fingerprint_plan_id(
        self,
        mode: SyncPlanMode,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
        required_data_types: tuple[str, ...],
        batch_size: int,
    ) -> str:
        """Deterministic plan_id for the given inputs (independent of now())."""
        from stock_manager.sync.planner import PlanInput

        source = (
            SyncSource.LEGACY_DATABASE
            if mode is SyncPlanMode.LEGACY_IMPORT
            else SyncSource.BAOSTOCK
        )
        input_ = PlanInput(
            mode, source, dataset_id, adjustment, "a-share",
            target_start, target_end, required_data_types,
            batch_size=batch_size,
        )
        return input_.plan_id

    def _persist_plan(self, output: PlannedOutput) -> None:
        self._repository.save_sync_plan(output.plan)
        self._repository.save_candidate_generation(output.candidate)
        for task in output.tasks:
            self._repository.save_sync_task(task)

    def _run_tasks(
        self, plan: SyncPlan, candidate: CandidateGeneration
    ) -> PipelineRun:
        """Execute PENDING tasks; returns a warning result when nothing to do."""
        pending = self._repository.tasks_by_status(
            plan.plan_id, (SyncTaskStatus.PENDING, SyncTaskStatus.INTERRUPTED)
        )
        if not pending:
            return PipelineRun(
                plan.plan_id, plan.status, candidate, (), False, 0,
                "no pending tasks; nothing to fetch",
            )
        if candidate.status is CandidateGenerationStatus.PLANNED:
            self._staging.begin_candidate(candidate, plan.source.value)
            candidate = self._repository.get_candidate_generation(
                candidate.candidate_generation_id
            )
            if candidate is None:
                raise PipelineError("candidate missing after begin")
        worker = self._worker_factory()
        statuses: list[tuple[str, SyncTaskStatus]] = []
        for task in pending:
            if task.status is SyncTaskStatus.INTERRUPTED:
                self._repository.update_sync_task_status(
                    replace(task, status=SyncTaskStatus.PENDING, error_message=None)
                )
                task = replace(task, status=SyncTaskStatus.PENDING)
            running = replace(
                task,
                status=SyncTaskStatus.RUNNING,
                attempt_count=task.attempt_count + 1,
                started_at=self._now(),
            )
            self._repository.update_sync_task_status(running)
            self._persist_task_progress(
                running, completed=0, total=len(task.codes),
                current_code=task.codes[0] if task.codes else "",
            )
            try:
                # 批次内实时进度:临时挂 provider 逐码回调,写回任务 progress_json。
                provider = getattr(worker, "_provider", None)
                original_callback = None
                if provider is not None and hasattr(provider, "_progress_callback"):
                    original_callback = provider._progress_callback
                    provider._progress_callback = (
                        lambda event, task=running: self._on_fetch_progress(
                            task, event
                        )
                    )
                try:
                    result: TaskExecutionResult = worker.execute(running)
                finally:
                    if provider is not None and original_callback is not None:
                        provider._progress_callback = original_callback
                rows = result.rows
                adjustment = (
                    plan.adjustment
                    if running.data_type == "daily_bars"
                    else None
                )
                self._staging.write_batch(
                    candidate,
                    running,
                    rows,
                    source=plan.source.value,
                    adjustment=adjustment,
                )
                done = replace(
                    running,
                    status=SyncTaskStatus.SUCCESS,
                    finished_at=result.finished_at,
                    row_count=result.row_count,
                    error_message=None,
                )
                self._repository.update_sync_task_status(done)
                self._persist_task_progress(
                    done, completed=len(task.codes), total=len(task.codes),
                    current_code=task.codes[-1] if task.codes else "",
                )
                statuses.append((task.task_id, SyncTaskStatus.SUCCESS))
            except (ProviderFetchError, Exception) as error:
                failed_at = self._now()
                failed = replace(
                    running,
                    status=SyncTaskStatus.FAILED,
                    finished_at=failed_at,
                    error_code=type(error).__name__,
                    error_message=str(error),
                    not_before=failed_at + self._retry_cooldown,
                )
                self._repository.update_sync_task_status(failed)
                statuses.append((task.task_id, SyncTaskStatus.FAILED))
                raise PipelineError(
                    f"task {task.task_id} failed: {error}"
                ) from error
        self._staging.finish_candidate(candidate)
        return PipelineRun(
            plan.plan_id, plan.status, candidate, tuple(statuses), False, 0, None
        )

    def _verify_and_publish(
        self, plan: SyncPlan, candidate: CandidateGeneration
    ) -> bool:
        """Verify staged partitions and publish when all COMPLETE."""
        # 重新读取 candidate,确保 verified_revision 等于最新 write_revision。
        current = self._repository.get_candidate_generation(
            candidate.candidate_generation_id
        )
        if current is None:
            raise PipelineError("candidate missing before verification")
        candidate = current
        tasks = self._repository.list_sync_tasks(plan.plan_id)
        outcome = self._verifier.verify(
            candidate,
            adjustment=plan.adjustment,
            target_start=plan.target_start,
            target_end=plan.target_end,
            tasks=tasks,
        )
        for record in outcome.records:
            self._repository.save_coverage_verification(record)
        if outcome.report.issues:
            repairable = any(
                issue.repairability.value == "REFETCH"
                for issue in outcome.report.issues
            )
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                (
                    CandidateGenerationStatus.NEEDS_REPAIR
                    if repairable
                    else CandidateGenerationStatus.VERIFICATION_FAILED
                ),
                self._now(),
            )
            return False
        self._repository.update_candidate_status(
            candidate.candidate_generation_id,
            CandidateGenerationStatus.VERIFIED,
            self._now(),
        )
        verified = self._repository.get_candidate_generation(
            candidate.candidate_generation_id
        )
        if verified is None:
            raise PipelineError("candidate missing after VERIFIED")
        partitions = self._legacy.partitions_for(
            verified.candidate_generation_id,
            generation=verified.candidate_generation_id,
        )
        published = self._committer.publish(
            verified,
            dataset_id=plan.dataset_id,
            adjustment=plan.adjustment,
            verifications=outcome.records,
            partitions=partitions,
        )
        return published.generation == verified.candidate_generation_id


def _as_interrupted(task: SyncTask, finished_at: datetime) -> SyncTask:
    """Return a copy of ``task`` marked INTERRUPTED (runner gone)."""
    return replace(
        task,
        status=SyncTaskStatus.INTERRUPTED,
        finished_at=finished_at,
        error_message="runner process stopped without cleanup",
    )
