"""Strict loader for synchronization policy configuration."""

from __future__ import annotations

import json
import warnings
from datetime import time, timedelta
from pathlib import Path
from typing import Any

from stock_manager.sync.data_sync_service import SyncConfig, SyncHistoryConfig


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _integer(section: dict[str, Any], field_name: str) -> int:
    value = section.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _number(section: dict[str, Any], field_name: str) -> float:
    value = section.get(field_name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    return float(value)


def _parse_policy(root: dict[str, Any]) -> dict[str, Any]:
    policy = _mapping(root.get("policy"), "policy")
    cutoff = policy.get("cutoff_time")
    if not isinstance(cutoff, str):
        raise ValueError("cutoff_time must be an ISO local time string")
    try:
        cutoff_time = time.fromisoformat(cutoff)
    except ValueError as error:
        raise ValueError("cutoff_time must be an ISO local time string") from error
    retention = policy.get("retention_days", 360)
    if not isinstance(retention, int) or isinstance(retention, bool):
        raise ValueError("retention_days must be an integer")
    pipeline_default = policy.get("pipeline_default", False)
    if not isinstance(pipeline_default, bool):
        raise ValueError("pipeline_default must be a boolean")
    return {
        "cutoff_time": cutoff_time,
        "retry_cooldown": timedelta(seconds=_integer(policy, "retry_cooldown_seconds")),
        "minimum_request_interval_seconds": _number(
            policy, "minimum_request_interval_seconds"
        ),
        "calendar_horizon_days": _integer(policy, "calendar_horizon_days"),
        "dividend_lookback_years": _integer(policy, "dividend_lookback_years"),
        "retention_days": retention,
        "pipeline_default": pipeline_default,
        "backfill_request_interval_seconds": (
            _number(policy, "backfill_request_interval_seconds")
            if "backfill_request_interval_seconds" in policy
            else None
        ),
    }


def _parse_history(root: dict[str, Any]) -> SyncHistoryConfig:
    history = _mapping(root.get("history"), "history")
    target_years = history.get("target_years")
    if not isinstance(target_years, int) or isinstance(target_years, bool):
        raise ValueError("history.target_years must be an integer")
    coverage_policy = history.get("coverage_policy", "latest_completed_trading_day")
    if not isinstance(coverage_policy, str):
        raise ValueError("history.coverage_policy must be a string")
    return SyncHistoryConfig(target_years, coverage_policy)


def load_sync_config(path: Path) -> SyncConfig:
    """Load a versioned Asia/Shanghai synchronization policy from JSON.

    Config v1 (no history block) keeps the one-year retention behavior
    unchanged; loading it emits a migration hint. Config v2 adds the optional
    history block whose presence routes startup backfill through the P5A-1
    eight-year coverage path.
    """
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to load sync config from {path}") from error
    root = _mapping(raw, "root")
    metadata = _mapping(root.get("metadata"), "metadata")
    version = _integer(metadata, "version")
    if version not in (1, 2):
        raise ValueError("unsupported sync config version")
    if metadata.get("timezone") != "Asia/Shanghai":
        raise ValueError("sync config timezone must be Asia/Shanghai")
    if version == 1:
        if "history" in root:
            raise ValueError("history requires sync config version 2")
        warnings.warn(
            "sync config v1 已读取；建议升级为 v2 以启用八年历史覆盖（P5A-1）",
            UserWarning,
            stacklevel=2,
        )
        return SyncConfig(**_parse_policy(root))
    history = _parse_history(root)
    return SyncConfig(**_parse_policy(root), history=history)

