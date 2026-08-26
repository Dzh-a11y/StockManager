from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from stock_manager.domain import AdjustmentMethod
from stock_manager.rules.base import (
    RuleContext,
    RuleDataRequirement,
    RuleDefinition,
)
from stock_manager.rules.registry import RuleRegistry
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template


@dataclass(frozen=True, slots=True)
class Threshold:
    value: Decimal


class StubRule:
    def __init__(self, rule_id: str) -> None:
        self.definition = RuleDefinition(rule_id, rule_id, "测试规则", ())

    def parse_parameters(self, raw: object) -> Threshold:
        if not isinstance(raw, dict) or set(raw) != {"value"}:
            raise ValueError("parameters must contain only value")
        return Threshold(Decimal(str(raw["value"])))

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        return RuleDataRequirement()

    def evaluate(self, context: RuleContext, parameters: object):
        raise NotImplementedError


def template_payload() -> dict[str, object]:
    return {
        "metadata": {
            "schema_version": 2,
            "template_id": "system-default",
            "revision": 1,
            "name": "系统默认",
            "description": "测试模板",
            "timezone": "Asia/Shanghai",
            "technical_adjustment": "qfq",
        },
        "rules": {
            "alpha": {"enabled": True, "parameters": {"value": "1.5"}},
            "beta": {"enabled": False, "parameters": {"value": "2"}},
        },
        "composition": {
            "operator": "all",
            "groups": [
                {"group_id": "signals", "operator": "any", "rules": ["alpha"]}
            ],
        },
    }


def test_compile_template_produces_typed_immutable_plan() -> None:
    registry = RuleRegistry((StubRule("alpha"), StubRule("beta")))

    plan = TemplateCompiler(registry).compile(parse_template(template_payload()))

    assert plan.template_id == "system-default"
    assert plan.revision == 1
    assert plan.adjustment is AdjustmentMethod.QFQ
    assert plan.enabled_rules[0].rule_id == "alpha"
    assert plan.enabled_rules[0].parameters == Threshold(Decimal("1.5"))
    assert plan.disabled_rule_ids == ("beta",)


def test_template_rejects_unknown_fields() -> None:
    payload = template_payload()
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    metadata["unexpected"] = True

    with pytest.raises(ValueError, match="unknown field"):
        parse_template(payload)


def test_template_rejects_unknown_rule() -> None:
    registry = RuleRegistry((StubRule("alpha"),))

    with pytest.raises(KeyError, match="unknown rule_id: beta"):
        TemplateCompiler(registry).compile(parse_template(template_payload()))


def test_template_rejects_enabled_rule_missing_from_composition() -> None:
    payload = template_payload()
    rules = payload["rules"]
    assert isinstance(rules, dict)
    alpha = rules["alpha"]
    beta = rules["beta"]
    assert isinstance(alpha, dict) and isinstance(beta, dict)
    alpha["enabled"] = True
    beta["enabled"] = True
    registry = RuleRegistry((StubRule("alpha"), StubRule("beta")))

    with pytest.raises(ValueError, match="enabled rules must appear exactly once"):
        TemplateCompiler(registry).compile(parse_template(payload))


def test_template_rejects_all_rules_disabled() -> None:
    payload = template_payload()
    rules = payload["rules"]
    assert isinstance(rules, dict)
    alpha = rules["alpha"]
    assert isinstance(alpha, dict)
    alpha["enabled"] = False
    registry = RuleRegistry((StubRule("alpha"), StubRule("beta")))

    with pytest.raises(ValueError, match="at least one rule must be enabled"):
        TemplateCompiler(registry).compile(parse_template(payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 3, "unsupported template schema"),
        ("timezone", "UTC", "timezone must be Asia/Shanghai"),
        ("technical_adjustment", "bad", "technical_adjustment is unsupported"),
    ],
)
def test_template_rejects_invalid_metadata(
    field: str, value: object, message: str
) -> None:
    payload = template_payload()
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    metadata[field] = value

    with pytest.raises(ValueError, match=message):
        parse_template(payload)
