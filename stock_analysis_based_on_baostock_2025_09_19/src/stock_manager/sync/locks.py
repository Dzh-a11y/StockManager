"""Process and persistent file locks used by synchronization."""

import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path


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


@contextmanager
def persistent_file_lock(path: Path) -> Iterator[None]:
    """Hold an OS-backed exclusive lock whose file persists for diagnostics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_file_lock_held(path: Path) -> bool:
    """Report whether another process currently holds an existing lock file."""
    if not path.exists():
        return False
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False
