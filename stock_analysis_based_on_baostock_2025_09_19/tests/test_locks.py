"""Cross-platform file lock behavior tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from threading import Event, Thread

from stock_manager.sync.locks import (
    dataset_lock_path,
    is_file_lock_held,
    persistent_file_lock,
)


def test_dataset_lock_path_is_stable(tmp_path: Path) -> None:
    path = dataset_lock_path(tmp_path, "market", date(2026, 8, 25))
    assert path == tmp_path / "market.2026-08-25.lock"
    assert dataset_lock_path(tmp_path, "market", date(2026, 8, 25)) == path


def test_lock_is_held_while_acquired_and_released_after(tmp_path: Path) -> None:
    path = tmp_path / "sample.lock"
    assert is_file_lock_held(path) is False  # not created yet
    with persistent_file_lock(path):
        assert path.exists()
        assert is_file_lock_held(path) is True
    assert is_file_lock_held(path) is False


def test_lock_blocks_a_second_holder_until_released(tmp_path: Path) -> None:
    path = tmp_path / "blocking.lock"
    acquired_second = Event()

    def second_holder() -> None:
        with persistent_file_lock(path):
            acquired_second.set()

    with persistent_file_lock(path):
        thread = Thread(target=second_holder)
        thread.start()
        assert acquired_second.wait(0.2) is False  # blocked by the outer lock
    assert acquired_second.wait(2.0) is True  # released once the outer lock exits
    thread.join()
