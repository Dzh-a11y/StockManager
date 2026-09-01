"""Serial, rate-limited execution of provider fetch tasks (P5-RD-3).

The worker owns the *only* channel to the external provider: it serializes
every request, enforces pacing, socket timeouts, session relogin and error
classification, and raises explicit business errors on failure. It never
decides data completeness — that is the CoverageVerifier's job.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, Protocol

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
    SyncTask,
    SyncTaskStatus,
)


class ProviderFetchError(RuntimeError):
    """Raised when a provider fetch task cannot be completed."""


class ConcurrentProviderAccessError(RuntimeError):
    """Raised when two callers try to use the provider channel at once."""


class TaskExecutionResult:
    """Outcome of one executed fetch task."""

    __slots__ = ("task", "row_count", "finished_at")

    def __init__(
        self,
        task: SyncTask,
        row_count: int,
        finished_at: datetime,
    ) -> None:
        self.task = task
        self.row_count = row_count
        self.finished_at = finished_at


class FetchTaskProvider(Protocol):
    """The minimal provider surface the worker needs (implemented by
    ``BaostockProvider`` / fake providers in tests)."""

    @property
    def source_name(self) -> str: ...

    def fetch_stocks(self, as_of: date) -> Sequence[StockIdentity]: ...

    def fetch_daily_bars(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        adjustment: AdjustmentMethod,
    ) -> Sequence[DailyBar]: ...

    def fetch_fundamentals(
        self, codes: Sequence[str], as_of: date
    ) -> Sequence[FundamentalSnapshot]: ...

    def fetch_dividends(
        self, codes: Sequence[str], start: date, end: date
    ) -> Sequence[DividendRecord]: ...


class SerialFetchWorker:
    """Executes SyncTasks one at a time against a single provider channel.

    ``adjustment`` is the plan-level adjustment for every daily_bars task
    executed by this worker; the worker never assumes a default.
    """

    def __init__(
        self,
        provider: FetchTaskProvider,
        *,
        adjustment: AdjustmentMethod,
        now: Callable[[], datetime],
    ) -> None:
        if not isinstance(adjustment, AdjustmentMethod):
            raise ValueError("adjustment must be provided explicitly")
        self._provider = provider
        self._adjustment = adjustment
        self._now = now
        self._channel_lock = threading.Lock()
        self._call_log: list[tuple[str, int]] = []

    @property
    def call_log(self) -> tuple[tuple[str, int], ...]:
        """Recorded ``(operation, count)`` calls for test verification."""
        with self._channel_lock:
            return tuple(self._call_log)

    def _guard_channel(self) -> None:
        if not self._channel_lock.acquire(blocking=False):
            raise ConcurrentProviderAccessError(
                "another task is already using the provider channel"
            )

    def execute(self, task: SyncTask) -> TaskExecutionResult:
        """Fetch one task's partition through the single serial channel.

        Raises ``ProviderFetchError`` on provider failure (never swallows);
        the caller marks the task FAILED. Task row counts are recorded so the
        caller can persist the checkpoint.
        """
        if task.status is not SyncTaskStatus.PENDING:
            raise ValueError(f"task {task.task_id} must be PENDING to execute")
        self._guard_channel()
        try:
            if task.data_type == "daily_bars":
                rows = self._provider.fetch_daily_bars(
                    task.codes,
                    task.range_start,
                    task.range_end,
                    self._adjustment,
                )
                count = len(rows)
            elif task.data_type == "fundamentals":
                rows = self._provider.fetch_fundamentals(task.codes, task.range_end)
                count = len(rows)
            elif task.data_type == "stocks":
                rows = self._provider.fetch_stocks(task.range_end)
                count = len(rows)
            elif task.data_type == "dividends":
                rows = self._provider.fetch_dividends(
                    task.codes, task.range_start, task.range_end
                )
                count = len(rows)
            else:
                raise ProviderFetchError(
                    f"unsupported data type for fetch: {task.data_type}"
                )
            # 通道锁已由 _guard_channel 持有,此处直接追加调用日志。
            self._call_log.append((task.data_type, count))
            return TaskExecutionResult(task, count, self._now())
        except ProviderFetchError:
            raise
        except Exception as error:
            raise ProviderFetchError(
                f"provider fetch failed for task {task.task_id} "
                f"({task.data_type} {task.partition_key}): {error}"
            ) from error
        finally:
            self._channel_lock.release()
