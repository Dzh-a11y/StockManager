"""Historical screening run store: state machine, progress, cancellation (P5A-5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    HistoricalRunStatus,
    HistoricalScreeningRun,
)
from stock_manager.protocols import LocalRepositoryProtocol

SHANGHAI = ZoneInfo("Asia/Shanghai")


class InvalidRunTransitionError(RuntimeError):
    """A state transition violates the run state machine."""


class RunNotFoundError(RuntimeError):
    """The requested run does not exist."""


_TRANSITIONS: dict[HistoricalRunStatus, frozenset[HistoricalRunStatus]] = {
    HistoricalRunStatus.QUEUED: frozenset(
        {HistoricalRunStatus.VALIDATING, HistoricalRunStatus.FAILED,
         HistoricalRunStatus.CANCEL_REQUESTED, HistoricalRunStatus.INTERRUPTED}
    ),
    HistoricalRunStatus.VALIDATING: frozenset(
        {HistoricalRunStatus.BUILDING_SIGNALS, HistoricalRunStatus.FAILED,
         HistoricalRunStatus.CANCEL_REQUESTED, HistoricalRunStatus.INTERRUPTED}
    ),
    HistoricalRunStatus.BUILDING_SIGNALS: frozenset(
        {HistoricalRunStatus.SUCCEEDED, HistoricalRunStatus.RUNNING_BACKTEST,
         HistoricalRunStatus.FAILED, HistoricalRunStatus.CANCEL_REQUESTED,
         HistoricalRunStatus.INTERRUPTED}
    ),
    HistoricalRunStatus.RUNNING_BACKTEST: frozenset(
        {HistoricalRunStatus.NORMALIZING, HistoricalRunStatus.FAILED,
         HistoricalRunStatus.CANCEL_REQUESTED, HistoricalRunStatus.INTERRUPTED}
    ),
    HistoricalRunStatus.NORMALIZING: frozenset(
        {HistoricalRunStatus.SUCCEEDED, HistoricalRunStatus.FAILED,
         HistoricalRunStatus.INTERRUPTED}
    ),
    HistoricalRunStatus.CANCEL_REQUESTED: frozenset(
        {HistoricalRunStatus.CANCELLED, HistoricalRunStatus.INTERRUPTED}
    ),
    HistoricalRunStatus.SUCCEEDED: frozenset(),
    HistoricalRunStatus.FAILED: frozenset(),
    HistoricalRunStatus.CANCELLED: frozenset(),
    HistoricalRunStatus.INTERRUPTED: frozenset({HistoricalRunStatus.QUEUED}),
}


class HistoricalScreeningRunStore:
    """Persisted lifecycle management for historical screening runs."""

    def __init__(
        self, repository: LocalRepositoryProtocol, *, clock: object | None = None
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(SHANGHAI))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    def create(self, run: HistoricalScreeningRun) -> None:
        if run.status is not HistoricalRunStatus.QUEUED:
            raise InvalidRunTransitionError(
                "a new run must start as QUEUED"
            )
        self._repository.save_historical_run(run)

    def _transition(
        self,
        run_id: str,
        target: HistoricalRunStatus,
        *,
        error_message: str | None = None,
        progress_completed: int | None = None,
        progress_total: int | None = None,
    ) -> HistoricalScreeningRun:
        current = self._repository.get_historical_run(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} does not exist")
        if target not in _TRANSITIONS[current.status]:
            raise InvalidRunTransitionError(
                f"illegal transition {current.status.value} -> {target.value}"
            )
        finished_at = self._now() if target in (
            HistoricalRunStatus.SUCCEEDED,
            HistoricalRunStatus.FAILED,
            HistoricalRunStatus.CANCELLED,
            HistoricalRunStatus.INTERRUPTED,
        ) else None
        updated = HistoricalScreeningRun(
            run_id=current.run_id,
            cache_key=current.cache_key,
            dataset_id=current.dataset_id,
            adjustment=current.adjustment,
            generation=current.generation,
            template_id=current.template_id,
            template_revision=current.template_revision,
            plan_fingerprint=current.plan_fingerprint,
            rule_implementation_version=current.rule_implementation_version,
            universe_policy=current.universe_policy,
            evaluation_start=current.evaluation_start,
            evaluation_end=current.evaluation_end,
            status=target,
            progress_completed=(
                progress_completed
                if progress_completed is not None
                else current.progress_completed
            ),
            progress_total=(
                progress_total
                if progress_total is not None
                else current.progress_total
            ),
            started_at=current.started_at,
            finished_at=finished_at,
            error_message=(
                error_message if error_message is not None else current.error_message
            ),
        )
        self._repository.save_historical_run(updated)
        return updated

    def start_validating(self, run_id: str) -> HistoricalScreeningRun:
        return self._transition(run_id, HistoricalRunStatus.VALIDATING)

    def start_building(self, run_id: str) -> HistoricalScreeningRun:
        return self._transition(run_id, HistoricalRunStatus.BUILDING_SIGNALS)

    def update_progress(
        self, run_id: str, completed: int, total: int
    ) -> HistoricalScreeningRun:
        if completed < 0 or total <= 0 or completed > total:
            raise ValueError("progress counters must satisfy 0 <= completed <= total")
        current = self._repository.get_historical_run(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} does not exist")
        if current.status is not HistoricalRunStatus.BUILDING_SIGNALS:
            raise InvalidRunTransitionError(
                "progress updates require BUILDING_SIGNALS status"
            )
        updated = replace(
            current,
            progress_completed=completed,
            progress_total=total,
        )
        self._repository.save_historical_run(updated)
        return updated

    def start_backtest(self, run_id: str) -> HistoricalScreeningRun:
        return self._transition(run_id, HistoricalRunStatus.RUNNING_BACKTEST)

    def start_normalizing(self, run_id: str) -> HistoricalScreeningRun:
        return self._transition(run_id, HistoricalRunStatus.NORMALIZING)

    def succeed(
        self, run_id: str, completed: int, total: int
    ) -> HistoricalScreeningRun:
        return self._transition(
            run_id,
            HistoricalRunStatus.SUCCEEDED,
            progress_completed=completed,
            progress_total=total,
        )

    def fail(self, run_id: str, error_message: str) -> HistoricalScreeningRun:
        if not error_message.strip():
            raise ValueError("error_message must not be empty")
        return self._transition(
            run_id, HistoricalRunStatus.FAILED, error_message=error_message
        )

    def request_cancel(self, run_id: str) -> HistoricalScreeningRun:
        return self._transition(run_id, HistoricalRunStatus.CANCEL_REQUESTED)

    def finish_cancelled(self, run_id: str) -> HistoricalScreeningRun:
        return self._transition(run_id, HistoricalRunStatus.CANCELLED)

    def recover_interrupted(
        self, dataset_id: str, adjustment: AdjustmentMethod
    ) -> int:
        """Mark all non-terminal runs INTERRUPTED (call once on startup)."""
        return self._repository.mark_interrupted_runs(
            dataset_id, adjustment, finished_at=self._now()
        )

    def enqueue_interrupted(self, run_id: str) -> HistoricalScreeningRun:
        """Re-queue one INTERRUPTED run for a fresh attempt."""
        return self._transition(run_id, HistoricalRunStatus.QUEUED)

    def get(self, run_id: str) -> HistoricalScreeningRun | None:
        return self._repository.get_historical_run(run_id)

    def list(
        self,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[HistoricalScreeningRun, ...]:
        return self._repository.list_historical_runs(
            dataset_id, adjustment, offset=offset, limit=limit
        )

    def find_cached(
        self, cache_key: str
    ) -> HistoricalScreeningRun | None:
        return self._repository.find_successful_run_by_cache_key(cache_key)

    def save_eligibility(
        self,
        run_id: str,
        snapshots: Sequence[object],
    ) -> None:
        """Persist compact eligibility snapshots for a finished run."""
        for snapshot in snapshots:
            day = snapshot.trading_day
            codes = snapshot.eligible_codes
            self._repository.save_eligibility_day(
                run_id, day, len(codes)
            )
            self._repository.save_eligibility_members(run_id, day, codes)

    def prune(self, dataset_id: str, adjustment: AdjustmentMethod, keep: int) -> int:
        return self._repository.prune_historical_runs(
            dataset_id, adjustment, keep
        )
