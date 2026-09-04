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


class PolicyOperator(str, Enum):
    """Group-level combination operator for entry/exit policy groups (P5C)."""

    ANY = "any"
    ALL = "all"


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
class TakeProfitTierSpec:
    """One take-profit reduction tier: profit threshold + sell fraction (P5C).

    Fraction semantics mirror ``take_profit_partial_v1.partial_ratio``: the
    fraction of the *current* holding sold when the close reaches the ratio.
    Tiers are held sorted by ascending take_profit_ratio.
    """

    take_profit_ratio: Decimal
    partial_ratio: Decimal

    def __post_init__(self) -> None:
        ratio = Decimal(str(self.take_profit_ratio))
        partial = Decimal(str(self.partial_ratio))
        if ratio <= 0:
            raise ValueError("take_profit_ratio must be positive")
        if not Decimal("0") < partial <= Decimal("1"):
            raise ValueError("partial_ratio must be in (0, 1]")
        object.__setattr__(self, "take_profit_ratio", ratio)
        object.__setattr__(self, "partial_ratio", partial)


@dataclass(frozen=True, slots=True)
class ResearchStrategySpec:
    """Immutable research backtest specification (P5A_PLAN 6.1).

    P5C extension: entry/exit may carry multiple parallel policies with an
    operator; take-profit tiers run independently of the exit group; run-level
    settings (codes, eligibility mode, fees, lot size) live on the spec instead
    of the execution policy parameters. ``entry_policy``/``exit_policy`` remain
    the legacy single-policy views (kept as positional-compatible fields);
    ``entry_policies``/``exit_policies`` default to singletons derived from them
    so legacy construction keeps identical semantics.
    """

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
    entry_policies: tuple[PolicySpec, ...] = ()
    exit_policies: tuple[PolicySpec, ...] = ()
    entry_operator: PolicyOperator = PolicyOperator.ANY
    exit_operator: PolicyOperator = PolicyOperator.ANY
    take_profit_tiers: tuple[TakeProfitTierSpec, ...] = ()
    stock_codes: tuple[str, ...] = ()
    ignore_eligibility: bool = False
    commission_rate: Decimal | None = None
    stamp_duty_rate: Decimal | None = None
    transfer_fee_rate: Decimal | None = None
    min_commission: Decimal | None = None
    lot_size: int | None = None
    strategy_template_id: str | None = None
    strategy_template_revision: int | None = None

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
        entry_policies = tuple(self.entry_policies) or (self.entry_policy,)
        exit_policies = tuple(self.exit_policies) or (self.exit_policy,)
        if not 1 <= len(entry_policies) <= 5:
            raise ValueError("entry policies must contain 1..5 policies")
        if not 1 <= len(exit_policies) <= 5:
            raise ValueError("exit policies must contain 1..5 policies")
        if len(self.take_profit_tiers) > 5:
            raise ValueError("take-profit tiers must contain at most 5 tiers")
        raw_codes = tuple(self.stock_codes)
        if any(not str(code).strip() for code in raw_codes):
            raise ValueError("stock codes must be non-empty")
        codes = tuple(dict.fromkeys(str(code).strip() for code in raw_codes))
        if self.ignore_eligibility and not codes:
            raise ValueError("ignore_eligibility requires at least one stock code")
        tiers = tuple(
            sorted(self.take_profit_tiers, key=lambda item: item.take_profit_ratio)
        )
        object.__setattr__(self, "entry_policies", entry_policies)
        object.__setattr__(self, "exit_policies", exit_policies)
        object.__setattr__(self, "take_profit_tiers", tiers)
        object.__setattr__(self, "stock_codes", codes)
