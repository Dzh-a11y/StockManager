"""Strict loader for JSON rule thresholds."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from stock_manager.domain import AdjustmentMethod


@dataclass(frozen=True, slots=True)
class PePositiveConfig:
    minimum_exclusive: Decimal


@dataclass(frozen=True, slots=True)
class VolumePriceConfig:
    lookback_trading_sessions: int
    minimum_volume_ratio: Decimal
    minimum_close_rise_percent: Decimal


@dataclass(frozen=True, slots=True)
class LimitUpBreakoutConfig:
    signal_lookback_trading_sessions: int
    highest_lookback_trading_sessions: int
    limit_ratio_lower_exclusive: Decimal
    limit_ratio_upper_exclusive: Decimal
    close_below_high_amount: Decimal


@dataclass(frozen=True, slots=True)
class LimitUpConfig:
    lookback_trading_sessions: int
    minimum_events: int
    maximum_events: int
    limit_ratio_lower_exclusive: Decimal
    limit_ratio_upper_exclusive: Decimal


@dataclass(frozen=True, slots=True)
class VolatilityConfig:
    lookback_trading_sessions: int
    maximum_multiple: Decimal
    minimum_required_sessions: int


@dataclass(frozen=True, slots=True)
class RulesConfig:
    version: int
    timezone: str
    technical_adjustment: AdjustmentMethod
    pe_positive: PePositiveConfig
    volume_price_5d: VolumePriceConfig
    limit_up_breakout: LimitUpBreakoutConfig
    limit_up_3m: LimitUpConfig
    volatility_multiple: VolatilityConfig


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _integer(section: dict[str, Any], field_name: str) -> int:
    value = section.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _text(section: dict[str, Any], field_name: str) -> str:
    value = section.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _decimal(section: dict[str, Any], field_name: str) -> Decimal:
    value = _text(section, field_name)
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a decimal string") from error


def load_rules_config(path: Path) -> RulesConfig:
    """Load and type-check the versioned rule configuration at a caller-owned path."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to load rules config from {path}") from error
    root = _mapping(raw, "root")
    metadata = _mapping(root.get("metadata"), "metadata")
    version = _integer(metadata, "version")
    if version != 1:
        raise ValueError(f"unsupported rules config version: {version}")
    timezone = _text(metadata, "timezone")
    if timezone != "Asia/Shanghai":
        raise ValueError("rules config timezone must be Asia/Shanghai")
    try:
        adjustment = AdjustmentMethod(_text(metadata, "technical_adjustment"))
    except ValueError as error:
        raise ValueError("technical_adjustment is unsupported") from error

    pe = _mapping(root.get("pe_positive"), "pe_positive")
    volume = _mapping(root.get("volume_price_5d"), "volume_price_5d")
    breakout = _mapping(root.get("limit_up_breakout"), "limit_up_breakout")
    limit_up = _mapping(root.get("limit_up_3m"), "limit_up_3m")
    volatility = _mapping(root.get("volatility_multiple"), "volatility_multiple")
    return RulesConfig(
        version=version,
        timezone=timezone,
        technical_adjustment=adjustment,
        pe_positive=PePositiveConfig(_decimal(pe, "minimum_exclusive")),
        volume_price_5d=VolumePriceConfig(
            _integer(volume, "lookback_trading_sessions"),
            _decimal(volume, "minimum_volume_ratio"),
            _decimal(volume, "minimum_close_rise_percent"),
        ),
        limit_up_breakout=LimitUpBreakoutConfig(
            _integer(breakout, "signal_lookback_trading_sessions"),
            _integer(breakout, "highest_lookback_trading_sessions"),
            _decimal(breakout, "limit_ratio_lower_exclusive"),
            _decimal(breakout, "limit_ratio_upper_exclusive"),
            _decimal(breakout, "close_below_high_amount"),
        ),
        limit_up_3m=LimitUpConfig(
            _integer(limit_up, "lookback_trading_sessions"),
            _integer(limit_up, "minimum_events"),
            _integer(limit_up, "maximum_events"),
            _decimal(limit_up, "limit_ratio_lower_exclusive"),
            _decimal(limit_up, "limit_ratio_upper_exclusive"),
        ),
        volatility_multiple=VolatilityConfig(
            _integer(volatility, "lookback_trading_sessions"),
            _decimal(volatility, "maximum_multiple"),
            _integer(volatility, "minimum_required_sessions"),
        ),
    )
