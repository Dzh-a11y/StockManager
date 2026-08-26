"""Process and persistent file locks used by synchronization.

The persistent file lock is OS-backed and therefore platform-specific:
``fcntl.flock`` on POSIX and ``msvcrt.locking`` (byte-range LockFileEx) on
Windows. Both semantics are the same: an exclusive, blocking lock on one lock
file whose existence is kept for diagnostics.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:  # pragma: no cover - branch depends on the host OS
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


_REGISTRY_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def dataset_lock_path(lock_directory: Path, dataset_id: str, trading_day: date) -> Path:
    """Build the stable diagnostic lock path for one dataset/day."""
    safe_dataset_id = "".join(
        character if character.isalnum() else "_" for character in dataset_id
    )
    return lock_directory / f"{safe_dataset_id}.{trading_day.isoformat()}.lock"


def process_lock(key: str) -> threading.Lock:
    """Return the process-wide lock for a stable synchronization key."""
    with _REGISTRY_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _ensure_byte(lock_file) -> None:
    """Guarantee at least one byte exists so byte-range locking is well-defined."""
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.write("\n")
        lock_file.flush()


def _lock_exclusive(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    else:  # pragma: no cover - Windows
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _try_lock_exclusive(lock_file) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    else:  # pragma: no cover - Windows
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True


@contextmanager
def persistent_file_lock(path: Path) -> Iterator[None]:
    """Hold an OS-backed exclusive lock whose file persists for diagnostics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        _ensure_byte(lock_file)
        _lock_exclusive(lock_file)
        try:
            yield
        finally:
            _unlock(lock_file)


def is_file_lock_held(path: Path) -> bool:
    """Report whether another process currently holds an existing lock file."""
    if not path.exists():
        return False
    with path.open("a+", encoding="utf-8") as lock_file:
        _ensure_byte(lock_file)
        if not _try_lock_exclusive(lock_file):
            return True
        _unlock(lock_file)
        return False
