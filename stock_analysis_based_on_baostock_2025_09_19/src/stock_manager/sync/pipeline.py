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

import json
import warnings
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
from stock_manager.sync.verifier import (
    CoverageVerifier,
    VerificationError,
    VerificationRejectedError,
)
from stock_manager.sync.worker import (
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
        worker_factory: Callable[[AdjustmentMethod], SerialFetchWorker],
        staging: StagingWriter,
        verifier: CoverageVerifier,
        committer: GenerationCommitter,
        gate: ReadinessGate,
        legacy: LegacyImporter,
        now: Callable[[], datetime],
        retry_cooldown: timedelta = timedelta(minutes=5),
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
        failed_tasks = self._repository.tasks_by_status(
            plan_id, (SyncTaskStatus.FAILED,)
        )
        if failed_tasks:
            return PipelineRun(
                plan_id,
                SyncPlanStatus.FAILED,
                self._repository.get_candidate_generation(
                    plan.candidate_generation_id
                ),
                tuple((task.task_id, task.status) for task in failed_tasks),
                False,
                0,
                "failed tasks require an explicit retry",
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
        if candidate.status is CandidateGenerationStatus.PUBLISHED:
            self._repository.update_sync_plan_status(
                plan_id, SyncPlanStatus.SUCCEEDED, self._now()
            )
            return PipelineRun(
                plan_id,
                SyncPlanStatus.SUCCEEDED,
                candidate,
                (),
                True,
                0,
                "candidate was already published; repaired plan status",
            )
        try:
            result = self._run_tasks(plan, candidate)
            if result.warning is not None:
                self._repository.update_sync_plan_status(
                    plan_id, SyncPlanStatus.FAILED, self._now()
                )
                return result
            published, issue_count = self._verify_and_publish(plan, candidate)
            final_status = (
                SyncPlanStatus.SUCCEEDED
                if published
                else SyncPlanStatus.FAILED
            )
            self._repository.update_sync_plan_status(
                plan_id, final_status, self._now()
            )
            final_candidate = self._repository.get_candidate_generation(
                candidate.candidate_generation_id
            )
            return PipelineRun(
                plan_id,
                final_status,
                final_candidate,
                result.task_statuses,
                published,
                issue_count,
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
        rerun_verification = False
        failed_candidate = False
        candidate = self._repository.get_candidate_generation(
            plan.candidate_generation_id
        )
        if candidate is not None and candidate.status is CandidateGenerationStatus.NEEDS_REPAIR:
            pending_repair = True
        if (
            candidate is not None
            and candidate.status is CandidateGenerationStatus.VERIFICATION_FAILED
        ):
            rerun_verification = True
        if (
            candidate is not None
            and candidate.status is CandidateGenerationStatus.FAILED
        ):
            failed_candidate = True
        if (
            not failed
            and not pending_repair
            and not rerun_verification
            and not failed_candidate
        ):
            raise PipelineError("nothing to retry for this plan")
        for task in failed:
            if (
                task.not_before is not None
                and task.not_before > now
            ):
                raise RetryCooldownError(
                    f"task {task.task_id} cooldown until {task.not_before.isoformat()}"
                )
        for task in failed:
            self._repository.update_sync_task_status(
                replace(task, status=SyncTaskStatus.PENDING, error_message=None)
            )
        if pending_repair and candidate is not None:
            repaired_tasks = self._reset_incomplete_tasks(plan, candidate)
            if repaired_tasks == 0 and not failed:
                raise PipelineError(
                    "verification reported repairable issues but no matching task"
                )
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.WRITING,
                now,
            )
        elif rerun_verification and candidate is not None:
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.VERIFYING,
                now,
            )
        elif failed_candidate and candidate is not None:
            # Historical runner versions could mark the whole candidate FAILED
            # after a task error. A user-triggered retry is the only path that
            # reopens it; normal execute() still refuses to write to it.
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.WRITING,
                now,
            )
        return self.execute(plan_id)

    def mark_interrupted(self) -> int:
        """Mark RUNNING plans/tasks INTERRUPTED (call after process restart)."""
        count = 0
        for adjustment in AdjustmentMethod:
            for plan in self._repository.list_sync_plans(
                "market", adjustment
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

    def recover_interrupted(self, plan_id: str) -> int:
        """Reset a plan whose runner died so the next execute can resume.

        A killed runner can leave RUNNING tasks behind. Only those interrupted
        tasks are reopened here. FAILED tasks/candidates deliberately remain
        failed and require :meth:`retry`, preserving the explicit-retry rule.
        A RUNNING plan is returned to PLANNED. Returns the number of tasks
        reset.
        """
        plan = self._repository.get_sync_plan(plan_id)
        if plan is None:
            return 0
        now = self._now()
        if plan.status is SyncPlanStatus.RUNNING:
            self._repository.update_sync_plan_status(
                plan_id, SyncPlanStatus.PLANNED, now
            )
        reset = 0
        for task in self._repository.tasks_by_status(
            plan_id, (SyncTaskStatus.RUNNING, SyncTaskStatus.INTERRUPTED)
        ):
            self._repository.update_sync_task_status(
                replace(task, status=SyncTaskStatus.PENDING, error_message=None)
            )
            reset += 1
        return reset

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
        payload = json.dumps(
            {
                "data_type": task.data_type,
                "partition_key": task.partition_key,
                "completed": completed,
                "total": total,
                "current_code": current_code,
                "status": task.status.value,
                "updated_at": self._now().isoformat(),
            },
            ensure_ascii=False,
        )
        try:
            self._repository.update_task_progress(task.task_id, payload)
        except Exception as error:
            warnings.warn(
                f"unable to persist sync progress for {task.task_id}: {error}",
                RuntimeWarning,
                stacklevel=2,
            )

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
        if candidate.status is CandidateGenerationStatus.PLANNED:
            self._staging.begin_candidate(candidate, plan.source.value)
            candidate = self._repository.get_candidate_generation(
                candidate.candidate_generation_id
            )
            if candidate is None:
                raise PipelineError("candidate missing after begin")
        if candidate.status not in (
            CandidateGenerationStatus.WRITING,
            CandidateGenerationStatus.VERIFYING,
            CandidateGenerationStatus.VERIFIED,
        ):
            raise PipelineError(
                f"candidate {candidate.candidate_generation_id} is "
                f"{candidate.status.value}; explicit recovery is required"
            )
        self._reconcile_successful_batches(plan, candidate)
        pending = self._repository.tasks_by_status(
            plan.plan_id, (SyncTaskStatus.PENDING, SyncTaskStatus.INTERRUPTED)
        )
        candidate = self._repository.get_candidate_generation(
            candidate.candidate_generation_id
        )
        if candidate is None:
            raise PipelineError("candidate missing after batch reconciliation")
        if pending and candidate.status in (
            CandidateGenerationStatus.VERIFYING,
            CandidateGenerationStatus.VERIFIED,
        ):
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.WRITING,
                self._now(),
            )
            candidate = self._repository.get_candidate_generation(
                candidate.candidate_generation_id
            )
            if candidate is None:
                raise PipelineError("candidate missing after verification invalidation")
        if not pending:
            tasks = self._repository.list_sync_tasks(plan.plan_id)
            if not tasks or any(task.status is not SyncTaskStatus.SUCCESS for task in tasks):
                return PipelineRun(
                    plan.plan_id, SyncPlanStatus.FAILED, candidate, (), False, 0,
                    "no runnable tasks but the plan is not complete",
                )
            if candidate.status is CandidateGenerationStatus.WRITING:
                self._staging.finish_candidate(candidate)
            return PipelineRun(
                plan.plan_id, plan.status, candidate, (), False, 0, None
            )
        if candidate.status is not CandidateGenerationStatus.WRITING:
            raise PipelineError(
                f"candidate {candidate.candidate_generation_id} is not writable"
            )
        worker = self._worker_factory(plan.adjustment)
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
                callback_attribute = (
                    provider is not None
                    and hasattr(provider, "_progress_callback")
                )
                original_callback = None
                if callback_attribute:
                    original_callback = provider._progress_callback
                    provider._progress_callback = (
                        lambda event, task=running: self._on_fetch_progress(
                            task, event
                        )
                    )
                try:
                    result: TaskExecutionResult = worker.execute(running)
                finally:
                    if callback_attribute:
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
            except Exception as error:
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
        current = self._repository.get_candidate_generation(
            candidate.candidate_generation_id
        )
        if current is None:
            raise PipelineError("candidate missing after task execution")
        self._staging.finish_candidate(current)
        return PipelineRun(
            plan.plan_id, plan.status, candidate, tuple(statuses), False, 0, None
        )

    def _verify_and_publish(
        self, plan: SyncPlan, candidate: CandidateGeneration
    ) -> tuple[bool, int]:
        """Verify staged partitions and publish when all COMPLETE."""
        # 重新读取 candidate,确保 verified_revision 等于最新 write_revision。
        current = self._repository.get_candidate_generation(
            candidate.candidate_generation_id
        )
        if current is None:
            raise PipelineError("candidate missing before verification")
        candidate = current
        tasks = self._repository.list_sync_tasks(plan.plan_id)
        try:
            outcome = self._verifier.verify(
                candidate,
                adjustment=plan.adjustment,
                target_start=plan.target_start,
                target_end=plan.target_end,
                tasks=tasks,
            )
        except VerificationRejectedError:
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.REJECTED,
                self._now(),
            )
            raise
        except VerificationError:
            self._repository.update_candidate_status(
                candidate.candidate_generation_id,
                CandidateGenerationStatus.VERIFICATION_FAILED,
                self._now(),
            )
            raise
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
            return False, len(outcome.report.issues)
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
        return (
            published.generation == verified.candidate_generation_id,
            0,
        )

    def _reconcile_successful_batches(
        self, plan: SyncPlan, candidate: CandidateGeneration
    ) -> None:
        """Recover old orphan staging rows or reset mismatches for refetch."""
        for task in self._repository.tasks_by_status(
            plan.plan_id, (SyncTaskStatus.SUCCESS,)
        ):
            batch_id = self._staging.batch_id(candidate, task)
            if self._repository.get_ingest_batch(batch_id) is not None:
                continue
            recovered = self._staging.recover_batch(
                candidate, task, source=plan.source.value
            )
            if recovered is None:
                self._repository.update_sync_task_status(
                    replace(
                        task,
                        status=SyncTaskStatus.PENDING,
                        row_count=None,
                        error_code=None,
                        error_message=None,
                        not_before=None,
                        started_at=None,
                        finished_at=None,
                    )
                )

    def _reset_incomplete_tasks(
        self, plan: SyncPlan, candidate: CandidateGeneration
    ) -> int:
        """Use persisted verifier evidence to reopen only failed partitions."""
        records = self._repository.list_coverage_verifications(
            candidate.candidate_generation_id
        )
        tasks = self._repository.list_sync_tasks(plan.plan_id)
        reset_ids: set[str] = set()
        for record in records:
            if record.status.value == "COMPLETE":
                continue
            suffix = record.partition_key.rsplit(":", maxsplit=1)[-1]
            if suffix.isdigit():
                sequence_no = int(suffix)
                matches = [
                    task
                    for task in tasks
                    if task.sequence_no == sequence_no
                    and task.data_type == record.data_type
                ]
            else:
                matches = [
                    task
                    for task in tasks
                    if task.data_type == record.data_type
                    and task.partition_key == record.partition_key
                ]
            for task in matches:
                if task.task_id in reset_ids:
                    continue
                self._repository.update_sync_task_status(
                    replace(
                        task,
                        status=SyncTaskStatus.PENDING,
                        row_count=None,
                        error_code=None,
                        error_message=None,
                        not_before=None,
                        started_at=None,
                        finished_at=None,
                    )
                )
                reset_ids.add(task.task_id)
        return len(reset_ids)


def _as_interrupted(task: SyncTask, finished_at: datetime) -> SyncTask:
    """Return a copy of ``task`` marked INTERRUPTED (runner gone)."""
    return replace(
        task,
        status=SyncTaskStatus.INTERRUPTED,
        finished_at=finished_at,
        error_message="runner process stopped without cleanup",
    )
