"""Strict loader for synchronization policy configuration."""

from __future__ import annotations

import json
from datetime import time, timedelta
from pathlib import Path
from typing import Any

from stock_manager.sync.data_sync_service import SyncConfig


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


def load_sync_config(path: Path) -> SyncConfig:
    """Load a versioned Asia/Shanghai synchronization policy from JSON."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to load sync config from {path}") from error
    root = _mapping(raw, "root")
    metadata = _mapping(root.get("metadata"), "metadata")
    if _integer(metadata, "version") != 1:
        raise ValueError("unsupported sync config version")
    if metadata.get("timezone") != "Asia/Shanghai":
        raise ValueError("sync config timezone must be Asia/Shanghai")
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
    return SyncConfig(
        cutoff_time=cutoff_time,
        retry_cooldown=timedelta(seconds=_integer(policy, "retry_cooldown_seconds")),
        minimum_request_interval_seconds=_number(
            policy, "minimum_request_interval_seconds"
        ),
        calendar_horizon_days=_integer(policy, "calendar_horizon_days"),
        dividend_lookback_years=_integer(policy, "dividend_lookback_years"),
        retention_days=retention,
    )
