"""Offline tests for the persistent Baostock request budget and circuit."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.storage import SQLiteRepository
from stock_manager.sync.request_budget import (
    ProviderCircuitOpenError,
    ProviderRequestBudgetExceededError,
    SQLiteProviderRequestBudget,
)

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _budget(path: Path, *, now: datetime = NOW, soft_limit: int = 3):
    return SQLiteProviderRequestBudget(
        path,
        source="baostock",
        soft_limit=soft_limit,
        hard_limit=5,
        now=lambda: now,
    )


def test_budget_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite3"
    SQLiteRepository(path)
    _budget(path).before_request("login")
    _budget(path).before_request("query")
    assert _budget(path).status().request_count == 2


def test_soft_limit_stops_before_request(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite3"
    SQLiteRepository(path)
    budget = _budget(path, soft_limit=2)
    budget.before_request("one")
    budget.before_request("two")
    with pytest.raises(ProviderRequestBudgetExceededError, match="soft limit"):
        budget.before_request("three")
    assert budget.status().request_count == 2


def test_blacklist_circuit_persists_and_increases_freeze(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite3"
    SQLiteRepository(path)
    first = _budget(path)
    first.trip_blacklist("10001011", "blacklisted")
    first_status = first.status()
    assert first_status.blacklist_count == 1
    assert first_status.circuit_open_until == NOW + timedelta(hours=6)
    with pytest.raises(ProviderCircuitOpenError, match="10001011"):
        _budget(path).before_request("query")

    later = NOW + timedelta(hours=7)
    second = _budget(path, now=later)
    second.trip_blacklist("10001011", "blacklisted again")
    second_status = second.status()
    assert second_status.blacklist_count == 2
    assert second_status.circuit_open_until == later + timedelta(hours=12)


def test_budget_rejects_unsafe_limits(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite3"
    SQLiteRepository(path)
    with pytest.raises(ValueError, match="hard_limit"):
        SQLiteProviderRequestBudget(
            path,
            source="baostock",
            soft_limit=50_000,
            hard_limit=50_001,
            now=lambda: NOW,
        )
