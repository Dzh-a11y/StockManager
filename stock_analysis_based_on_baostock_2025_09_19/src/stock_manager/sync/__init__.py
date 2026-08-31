"""Protected market-data synchronization services."""

from __future__ import annotations

from stock_manager.sync.data_sync_service import (
    CooldownActiveError,
    DataSyncService,
    RetryRequiredError,
    SyncConfig,
    SyncFailedError,
    SyncHistoryConfig,
    latest_completed_trading_day,
)
from stock_manager.sync.config import load_sync_config
from stock_manager.sync.history_plan import (
    CoveragePlan,
    DataTypePlan,
    plan_coverage,
    trading_day_lookback,
)

__all__ = [
    "CooldownActiveError",
    "CoveragePlan",
    "DataSyncService",
    "DataTypePlan",
    "RetryRequiredError",
    "SyncConfig",
    "SyncFailedError",
    "SyncHistoryConfig",
    "latest_completed_trading_day",
    "load_sync_config",
    "plan_coverage",
    "trading_day_lookback",
]
