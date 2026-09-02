from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DatasetMetadata,
    RuleStatus,
    StockIdentity,
)
from stock_manager.rules.base import (
    ParameterDefinition,
    ParameterType,
    RuleContext,
    RuleDataRequirement,
    RuleDefinition,
    WindowUnit,
)
from stock_manager.rules.composition import CompositionEngine, RuleGroup
from stock_manager.rules.registry import RuleRegistry


@dataclass(frozen=True, slots=True)
class ExampleParameters:
    minimum: Decimal


class ExampleRule:
    definition = RuleDefinition(
        rule_id="example",
        name="示例规则",
        description="测试显式注册",
        parameters=(
            ParameterDefinition(
                parameter_id="minimum",
                value_type=ParameterType.DECIMAL,
                required=True,
                default_value="1",
                minimum="0",
                maximum=None,
                label="最小值",
                description="示例阈值",
            ),
        ),
    )

    def parse_parameters(self, raw: object) -> ExampleParameters:
        if not isinstance(raw, dict) or set(raw) != {"minimum"}:
            raise ValueError("example parameters are invalid")
        return ExampleParameters(Decimal(str(raw["minimum"])))

    def data_requirement(
        self, parameters: object
    ) -> RuleDataRequirement:
        if not isinstance(parameters, ExampleParameters):
            raise TypeError("parameters must be ExampleParameters")
        return RuleDataRequirement()

    def evaluate(self, context: RuleContext, parameters: object):
        raise NotImplementedError


def test_registry_rejects_duplicate_rule_ids() -> None:
    registry = RuleRegistry((ExampleRule(),))

    with pytest.raises(ValueError, match="duplicate rule_id"):
        registry.register(ExampleRule())


def test_registry_rejects_unknown_rule_id() -> None:
    registry = RuleRegistry((ExampleRule(),))

    with pytest.raises(KeyError, match="unknown rule_id"):
        registry.get("missing")


def test_rule_context_contains_only_prefetched_domain_data() -> None:
    metadata = DatasetMetadata(
        "market",
        date(2026, 8, 25),
        "fixture",
        datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        AdjustmentMethod.QFQ,
    )
    context = RuleContext(
        stock=StockIdentity("sh.600000", "浦发银行", "sh", False, None, None),
        trading_day=date(2026, 8, 25),
        adjustment=AdjustmentMethod.QFQ,
        metadata=metadata,
        daily_bars=(),
        fundamental=None,
        dividends=(),
    )

    assert context.daily_bars == ()
    assert not hasattr(context, "repository")
    assert not hasattr(context, "provider")


def test_data_requirement_validates_history_window() -> None:
    with pytest.raises(ValueError, match="history_length"):
        RuleDataRequirement(
            market_history_unit=WindowUnit.CALENDAR_DAYS,
            history_length=0,
        )


def test_composition_ignores_skipped_rules_and_supports_groups() -> None:
    groups = (
        RuleGroup("fundamental", "all", ("pe_positive", "non_st")),
        RuleGroup("signal", "any", ("volume_price_5d", "annual_min_volume")),
    )
    statuses = MappingProxyType(
        {
            "pe_positive": RuleStatus.PASSED,
            "non_st": RuleStatus.SKIPPED,
            "volume_price_5d": RuleStatus.FAILED,
            "annual_min_volume": RuleStatus.PASSED,
        }
    )

    result = CompositionEngine().evaluate("all", groups, statuses)

    assert result is True


def test_composition_rejects_when_every_rule_is_skipped() -> None:
    groups = (RuleGroup("only", "all", ("pe_positive",)),)

    with pytest.raises(ValueError, match="no enabled rule results"):
        CompositionEngine().evaluate(
            "all", groups, MappingProxyType({"pe_positive": RuleStatus.SKIPPED})
        )
