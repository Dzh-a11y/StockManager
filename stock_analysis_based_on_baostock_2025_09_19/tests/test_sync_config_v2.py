"""P5A-1 config v2 loader tests: v1 compatibility and history parsing."""

from __future__ import annotations

import json
from datetime import time, timedelta
from pathlib import Path
import warnings

import pytest

from stock_manager.sync import SyncConfig, SyncHistoryConfig, load_sync_config


V1_CONFIG = {
    "metadata": {"version": 1, "timezone": "Asia/Shanghai"},
    "policy": {
        "cutoff_time": "17:30:00",
        "retry_cooldown_seconds": 300,
        "minimum_request_interval_seconds": 0.2,
        "calendar_horizon_days": 45,
        "dividend_lookback_years": 3,
        "retention_days": 360,
    },
}

V2_CONFIG = {
    "metadata": {"version": 2, "timezone": "Asia/Shanghai"},
    "policy": {
        "cutoff_time": "17:30:00",
        "retry_cooldown_seconds": 300,
        "minimum_request_interval_seconds": 0.2,
        "calendar_horizon_days": 45,
        "dividend_lookback_years": 3,
    },
    "history": {
        "target_years": 8,
        "coverage_policy": "latest_completed_trading_day",
    },
}


def _write(tmp_path: Path, config: dict[str, object]) -> Path:
    path = tmp_path / "sync.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_v1_config_loads_with_history_none_and_migration_hint(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, V1_CONFIG)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = load_sync_config(path)
    assert config.history is None
    assert config.retention_days == 360
    assert any("v2" in str(item.message) for item in caught)


def test_v2_config_loads_history(tmp_path: Path) -> None:
    path = _write(tmp_path, V2_CONFIG)
    config = load_sync_config(path)
    assert isinstance(config.history, SyncHistoryConfig)
    assert config.history is not None
    assert config.history.target_years == 8
    assert config.history.coverage_policy == "latest_completed_trading_day"
    assert config.retention_days == 360  # default when omitted


def test_v1_config_with_history_block_is_rejected(tmp_path: Path) -> None:
    config = dict(V1_CONFIG)
    config["history"] = {"target_years": 8}
    path = _write(tmp_path, config)
    with pytest.raises(ValueError, match="requires sync config version 2"):
        load_sync_config(path)


def test_unsupported_version_rejected(tmp_path: Path) -> None:
    config = dict(V1_CONFIG)
    config["metadata"] = {"version": 3, "timezone": "Asia/Shanghai"}
    path = _write(tmp_path, config)
    with pytest.raises(ValueError, match="unsupported sync config version"):
        load_sync_config(path)


def test_v2_bad_target_years_rejected(tmp_path: Path) -> None:
    config = dict(V2_CONFIG)
    config["history"] = {"target_years": "8"}
    path = _write(tmp_path, config)
    with pytest.raises(ValueError, match="target_years must be an integer"):
        load_sync_config(path)


def test_v2_nonpositive_target_years_rejected(tmp_path: Path) -> None:
    config = dict(V2_CONFIG)
    config["history"] = {"target_years": 0}
    path = _write(tmp_path, config)
    with pytest.raises(ValueError, match="target_years must be positive"):
        load_sync_config(path)


def test_v2_unknown_coverage_policy_rejected(tmp_path: Path) -> None:
    config = dict(V2_CONFIG)
    config["history"] = {"target_years": 8, "coverage_policy": "everything"}
    path = _write(tmp_path, config)
    with pytest.raises(ValueError, match="unsupported coverage policy"):
        load_sync_config(path)


def test_v2_wrong_timezone_rejected(tmp_path: Path) -> None:
    config = dict(V2_CONFIG)
    config["metadata"] = {"version": 2, "timezone": "UTC"}
    path = _write(tmp_path, config)
    with pytest.raises(ValueError, match="timezone must be Asia/Shanghai"):
        load_sync_config(path)


def test_history_config_validates_directly() -> None:
    SyncHistoryConfig(target_years=8)
    with pytest.raises(ValueError, match="target_years must be positive"):
        SyncHistoryConfig(target_years=-1)
    with pytest.raises(ValueError, match="unsupported coverage policy"):
        SyncHistoryConfig(target_years=8, coverage_policy="unknown")


def test_sync_config_accepts_history_and_keeps_v1_fields() -> None:
    config = SyncConfig(
        time(17, 30),
        timedelta(minutes=5),
        0.2,
        45,
        3,
        360,
        SyncHistoryConfig(target_years=8),
    )
    assert config.history is not None
    assert config.history.target_years == 8
    assert config.cutoff_time == time(17, 30)
