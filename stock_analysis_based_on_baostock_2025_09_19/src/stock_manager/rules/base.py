"""Stable contracts for registered, offline screening rules."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Protocol

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    RuleResult,
    StockIdentity,
)


class ParameterType(str, Enum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    TEXT = "text"


class WindowUnit(str, Enum):
    CALENDAR_DAYS = "calendar_days"
    TRADING_SESSIONS = "trading_sessions"


@dataclass(frozen=True, slots=True)
class ParameterDefinition:
    parameter_id: str
    value_type: ParameterType
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
            raise ValueError("parameter label must not be empty")
        if not self.description.strip():
            raise ValueError("parameter description must not be empty")


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    rule_id: str
    name: str
    description: str
    parameters: tuple[ParameterDefinition, ...]

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("rule_id must not be empty")
        if not self.name.strip():
            raise ValueError("rule name must not be empty")
        if not self.description.strip():
            raise ValueError("rule description must not be empty")
        parameter_ids = tuple(item.parameter_id for item in self.parameters)
        if len(set(parameter_ids)) != len(parameter_ids):
            raise ValueError(f"duplicate parameter_id in rule {self.rule_id}")


@dataclass(frozen=True, slots=True)
class RuleDataRequirement:
    needs_stock_identity: bool = True
    needs_fundamental: bool = False
    needs_dividends: bool = False
    dividend_calendar_years: int | None = None
    market_history_unit: WindowUnit | None = None
    history_length: int | None = None

    def __post_init__(self) -> None:
        if (self.market_history_unit is None) != (self.history_length is None):
            raise ValueError("market history unit and history_length must be paired")
        if self.history_length is not None and self.history_length <= 0:
            raise ValueError("history_length must be positive")
        if self.dividend_calendar_years is not None:
            if not self.needs_dividends:
                raise ValueError("dividend years require needs_dividends")
            if self.dividend_calendar_years <= 0:
                raise ValueError("dividend_calendar_years must be positive")


@dataclass(frozen=True, slots=True)
class RuleContext:
    stock: StockIdentity
    trading_day: date
    adjustment: AdjustmentMethod
    metadata: DatasetMetadata
    daily_bars: tuple[DailyBar, ...]
    fundamental: FundamentalSnapshot | None
    dividends: tuple[DividendRecord, ...]

    def __post_init__(self) -> None:
        if self.metadata.trading_day != self.trading_day:
            raise ValueError("context trading_day must match dataset metadata")
        if self.metadata.adjustment is not self.adjustment:
            raise ValueError("context adjustment must match dataset metadata")


class ScreeningRule(Protocol):
    @property
    def definition(self) -> RuleDefinition: ...

    def parse_parameters(self, raw: object) -> object: ...

    def data_requirement(self, parameters: object) -> RuleDataRequirement: ...

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult: ...
