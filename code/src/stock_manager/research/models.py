"""Research strategy domain models (P5A-3).

Pure data objects: no Backtrader, no database, no provider, no API types.
All amounts are Decimal; all policy parameters are JSON-serializable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from stock_manager.domain import AdjustmentMethod


class PolicyKind(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    REBALANCE = "rebalance"
    ALLOCATION = "allocation"
    RANKING = "ranking"
    EXECUTION = "execution"


class EvaluationSchedule(str, Enum):
    DAILY = "daily"


class PolicyParameterType(str, Enum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class PolicyParameterSpec:
    """Declared boundary for one policy parameter (whitelist validation)."""

    parameter_id: str
    value_type: PolicyParameterType
    required: bool
    default_value: object
    minimum: object | None
    maximum: object | None
    label: str
    description: str

    def __post_init__(self) -> None:
        if not self.parameter_id.strip():
            raise ValueError("parameter_id must not be empty")
        if not self.label.strip():
            raise ValueError("label must not be empty")
        if not self.description.strip():
            raise ValueError("description must not be empty")


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """Versioned reference to one registered policy with JSON parameters."""

    policy_id: str
    version: int
    parameters: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("policy_id must not be empty")
        if self.version <= 0:
            raise ValueError("policy version must be positive")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ResearchStrategySpec:
    """Immutable research backtest specification (P5A_PLAN 6.1)."""

    strategy_spec_id: str
    screening_template_id: str
    screening_template_revision: int
    screening_plan_fingerprint: str
    adjustment: AdjustmentMethod
    evaluation_schedule: EvaluationSchedule
    entry_policy: PolicySpec
    exit_policy: PolicySpec
    rebalance_policy: PolicySpec
    allocation_policy: PolicySpec
    ranking_policy: PolicySpec
    execution_policy: PolicySpec
    initial_cash: Decimal
    backtest_start: date
    backtest_end: date

    def __post_init__(self) -> None:
        if not self.strategy_spec_id.strip():
            raise ValueError("strategy_spec_id must not be empty")
        if not self.screening_template_id.strip():
            raise ValueError("screening_template_id must not be empty")
        if self.screening_template_revision <= 0:
            raise ValueError("screening_template_revision must be positive")
        if not self.screening_plan_fingerprint.strip():
            raise ValueError("screening_plan_fingerprint must not be empty")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.backtest_start > self.backtest_end:
            raise ValueError("backtest_start must not be after backtest_end")
