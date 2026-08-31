"""Bounded local job runner (P5A-8): one heavy job at a time by default."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any


class DuplicateJobError(RuntimeError):
    """A job with the same key is already queued or running."""


class JobNotFoundError(RuntimeError):
    """No job exists for the given key."""


class BoundedJobRunner:
    """Single worker thread consuming a FIFO queue.

    The same key cannot be enqueued twice while queued/running, which stops
    repeated clicks from creating duplicate backtest runs.
    """

    def __init__(self, max_concurrent: int = 1) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self._max_concurrent = max_concurrent
        self._queue: deque[str] = deque()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._loop, name="stockmanager-job-runner", daemon=True
        )
        self._worker.start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                key = self._queue.popleft()
                job = self._jobs[key]
            try:
                job["status"] = "running"
                job["fn"]()
                with self._lock:
                    job["status"] = "done"
            except Exception as error:
                with self._lock:
                    job["status"] = "failed"
                    job["error"] = str(error)

    def submit(self, key: str, fn: Callable[[], None]) -> None:
        if not key.strip():
            raise ValueError("job key must not be empty")
        with self._lock:
            existing = self._jobs.get(key)
            if existing is not None and existing["status"] in ("queued", "running"):
                raise DuplicateJobError(f"job {key} is already queued or running")
            self._jobs[key] = {"status": "queued", "fn": fn}
            self._queue.append(key)
        self._ensure_worker()

    def cancel(self, key: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                raise JobNotFoundError(f"job {key} does not exist")
            if job["status"] == "queued":
                job["status"] = "cancelled"
                return
            job["cancel"] = True

    def should_cancel(self, key: str) -> bool:
        with self._lock:
            return bool(self._jobs.get(key, {}).get("cancel"))

    def status(self, key: str) -> str | None:
        with self._lock:
            job = self._jobs.get(key)
            return None if job is None else job["status"]
