"""P5A-2 rule historical capability tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from stock_manager.domain import AdjustmentMethod
from stock_manager.read.historical import PointInTimeRequest
from stock_manager.rules.builtin import build_default_registry
from stock_manager.rules.historical_capability import (
    HistoricalCapabilityValidator,
    UnsupportedRuleForHistoricalRunError,
)
from stock_manager.rules.registry import RuleRegistry
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template

QFQ = AdjustmentMethod.QFQ


class _UnsupportedRule:
    """A rule that must be rejected for historical runs."""

    def __init__(self) -> None:
        from stock_manager.rules.base import RuleDefinition

        self.definition = RuleDefinition(
            "future_rule", "未来规则", "演示 PIT_UNSUPPORTED", ()
        )

    def parse_parameters(self, raw: object) -> object:
        return None

    def data_requirement(self, raw: object) -> object:
        from stock_manager.rules.base import RuleDataRequirement

        return RuleDataRequirement()

    def evaluate(self, context: object, parameters: object) -> object:
        raise NotImplementedError


class _EmptyReader:
    def __init__(self) -> None:
        self.request = PointInTimeRequest("market", (), date(2020, 1, 1), date(2020, 1, 31), QFQ)

    def fundamentals_through(self, day: date) -> tuple:
        return ()

    def universe_as_of(self, day: date) -> tuple:
        return ()


_RULE_PARAMS: dict[str, dict[str, object]] = {
    "consecutive_up_days": {"lookback_trading_sessions": 60, "required_consecutive_days": 5},
    "n_day_close_above": {"lookback_trading_sessions": 5, "minimum_close": "10"},
    "pe_positive": {"minimum_exclusive": "0"},
    "non_st": {},
}


def _plan_for(rule_ids: tuple[str, ...]) -> object:
    raw = {
        "metadata": {
            "schema_version": 2,
            "template_id": "t",
            "revision": 1,
            "name": "T",
            "description": "d",
            "timezone": "Asia/Shanghai",
            "technical_adjustment": "qfq",
        },
        "rules": {
            rule_id: {
                "enabled": True,
                "parameters": _RULE_PARAMS.get(rule_id, {}),
            }
            for rule_id in rule_ids
        },
        "composition": {"operator": "all", "groups": [{"group_id": "g", "operator": "all", "rules": list(rule_ids)}]},
    }
    from stock_manager.templates.models import parse_template

    return TemplateCompiler(build_default_registry()).compile(parse_template(raw))


def _all_rule_ids() -> tuple[str, ...]:
    return tuple(d.rule_id for d in build_default_registry().definitions())


def test_all_builtin_rules_declare_pit_capability() -> None:
    for definition in build_default_registry().definitions():
        assert definition.pit_capability is not None
        assert definition.pit_capability.value.startswith(("PRICE_VOLUME_", "FUNDAMENTAL_", "UNIVERSE_"))


def test_price_volume_rules_are_ready_with_empty_reader() -> None:
    plan = _plan_for(("consecutive_up_days", "n_day_close_above"))
    validator = HistoricalCapabilityValidator(build_default_registry())
    report = validator.validate(plan, _EmptyReader(), dataset_id="market", adjustment=QFQ)
    assert report.ready
    assert report.rejected_rules == ()


def test_fundamental_rule_warns_when_no_fundamentals_data() -> None:
    plan = _plan_for(("pe_positive",))
    validator = HistoricalCapabilityValidator(build_default_registry())
    report = validator.validate(plan, _EmptyReader(), dataset_id="market", adjustment=QFQ)
    assert not report.ready
    assert any("fundamentals" in item for item in report.data_warnings)


def test_universe_rule_warns_when_no_stocks_data() -> None:
    plan = _plan_for(("non_st",))
    validator = HistoricalCapabilityValidator(build_default_registry())
    report = validator.validate(plan, _EmptyReader(), dataset_id="market", adjustment=QFQ)
    assert not report.ready
    assert any("stocks" in item for item in report.data_warnings)


def test_unsupported_rule_rejected_explicitly() -> None:
    registry = RuleRegistry((_UnsupportedRule(),))
    plan = _plan_for(("consecutive_up_days",))
    # 用含 future_rule 的计划
    raw = {
        "metadata": {
            "schema_version": 2,
            "template_id": "t2",
            "revision": 1,
            "name": "T2",
            "description": "d",
            "timezone": "Asia/Shanghai",
            "technical_adjustment": "qfq",
        },
        "rules": {"future_rule": {"enabled": True, "parameters": {}}},
        "composition": {"operator": "all", "groups": [{"group_id": "g", "operator": "all", "rules": ["future_rule"]}]},
    }
    from stock_manager.templates.models import parse_template

    plan2 = TemplateCompiler(registry).compile(parse_template(raw))
    validator = HistoricalCapabilityValidator(registry)
    report = validator.validate(plan2, _EmptyReader(), dataset_id="market", adjustment=QFQ)
    assert not report.ready
    assert report.rejected_rules == ("future_rule",)
    with pytest.raises(UnsupportedRuleForHistoricalRunError, match="future_rule"):
        validator.require_ready(plan2, _EmptyReader(), dataset_id="market", adjustment=QFQ)


def test_require_ready_raises_on_missing_data() -> None:
    plan = _plan_for(("pe_positive",))
    validator = HistoricalCapabilityValidator(build_default_registry())
    with pytest.raises(Exception, match="blocked by missing data"):
        validator.require_ready(plan, _EmptyReader(), dataset_id="market", adjustment=QFQ)
