"""P5A-3 research strategy domain, policy registry and fingerprint tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_manager.domain import AdjustmentMethod
from stock_manager.research import (
    InvalidPolicyParametersError,
    PolicyKind,
    PolicySpec,
    ResearchStrategySpec,
    UnknownPolicyError,
    build_default_policy_registry,
    builtin_strategy_specs,
    canonical_json,
    plan_fingerprint,
    spec_fingerprint,
)
from stock_manager.rules.builtin import build_default_registry
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template


TEMPLATE_RAW = {
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
        "consecutive_up_days": {
            "enabled": True,
            "parameters": {"lookback_trading_sessions": 60, "required_consecutive_days": 5},
        }
    },
    "composition": {
        "operator": "all",
        "groups": [{"group_id": "g", "operator": "all", "rules": ["consecutive_up_days"]}],
    },
}


def _compile_plan() -> object:
    registry = build_default_registry()
    return TemplateCompiler(registry).compile(parse_template(TEMPLATE_RAW))


def test_domain_does_not_import_backtrader() -> None:
    """Research domain must never leak Backtrader types."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import stock_manager.research"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import stock_manager.research; "
            "assert 'backtrader' not in sys.modules, 'backtrader leaked into research'",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_policy_registry_rejects_unknown_policy() -> None:
    registry = build_default_policy_registry()
    with pytest.raises(UnknownPolicyError, match="unknown policy"):
        registry.get("no_such_policy", PolicyKind.ENTRY, 1)
    with pytest.raises(UnknownPolicyError, match="v99"):
        registry.get("eligibility_enter_v1", PolicyKind.ENTRY, 99)
    with pytest.raises(UnknownPolicyError):
        registry.get("eligibility_enter_v1", PolicyKind.EXIT, 1)


def test_policy_registry_rejects_unknown_parameters() -> None:
    registry = build_default_policy_registry()
    spec = PolicySpec("equal_weight_v1", 1, {"max_positions": 10, "import_path": "evil"})
    with pytest.raises(InvalidPolicyParametersError, match="unknown parameter"):
        registry.validate_spec(spec, PolicyKind.ALLOCATION)


def test_policy_registry_rejects_missing_required_and_bad_values() -> None:
    registry = build_default_policy_registry()
    spec = PolicySpec("equal_weight_v1", 1, {})
    with pytest.raises(InvalidPolicyParametersError, match="missing required"):
        registry.validate_spec(spec, PolicyKind.ALLOCATION)
    spec = PolicySpec("equal_weight_v1", 1, {"max_positions": 0})
    with pytest.raises(InvalidPolicyParametersError, match=">= 1"):
        registry.validate_spec(spec, PolicyKind.ALLOCATION)
    spec = PolicySpec("equal_weight_v1", 1, {"max_positions": 10, "cash_reserve_ratio": "2"})
    with pytest.raises(InvalidPolicyParametersError, match="<= 1"):
        registry.validate_spec(spec, PolicyKind.ALLOCATION)


def test_import_paths_in_parameters_are_rejected() -> None:
    """No parameter schema accepts module paths or class names."""
    registry = build_default_policy_registry()
    for definition in registry.definitions():
        for parameter in definition.parameters:
            if parameter.value_type.value == "text":
                with pytest.raises(InvalidPolicyParametersError):
                    registry.validate_spec(
                        PolicySpec(
                            definition.policy_id,
                            definition.version,
                            {parameter.parameter_id: "evil.module.Path"},
                        ),
                        definition.kind,
                    )


def test_first_three_strategy_specs_validate() -> None:
    registry = build_default_policy_registry()
    specs = builtin_strategy_specs(
        template_id="t",
        template_revision=1,
        plan_fingerprint="abc",
        backtest_start=date(2020, 1, 1),
        backtest_end=date(2021, 1, 1),
    )
    assert set(specs) == {
        "selection_rebalance_v1",
        "selection_sma_timing_v1",
        "selection_fixed_holding_v1",
    }
    for spec in specs.values():
        registry.validate_strategy(
            entry=spec.entry_policy,
            exit=spec.exit_policy,
            rebalance=spec.rebalance_policy,
            allocation=spec.allocation_policy,
            ranking=spec.ranking_policy,
            execution=spec.execution_policy,
        )
        assert isinstance(spec.initial_cash, Decimal)
        assert spec.initial_cash > 0


def test_strategy_spec_validation() -> None:
    with pytest.raises(ValueError, match="initial_cash must be positive"):
        ResearchStrategySpec(
            "s",
            "t",
            1,
            "fp",
            AdjustmentMethod.QFQ,
            spec_eval_schedule(),
            PolicySpec("a", 1, {}),
            PolicySpec("b", 1, {}),
            PolicySpec("c", 1, {}),
            PolicySpec("d", 1, {}),
            PolicySpec("e", 1, {}),
            PolicySpec("f", 1, {}),
            Decimal("0"),
            date(2020, 1, 1),
            date(2021, 1, 1),
        )


def spec_eval_schedule():
    from stock_manager.research import EvaluationSchedule

    return EvaluationSchedule.DAILY


def test_fingerprint_stable_and_sensitive() -> None:
    specs = builtin_strategy_specs(
        template_id="t",
        template_revision=1,
        plan_fingerprint="abc",
        backtest_start=date(2020, 1, 1),
        backtest_end=date(2021, 1, 1),
    )
    first = spec_fingerprint(specs["selection_rebalance_v1"])
    second = spec_fingerprint(specs["selection_rebalance_v1"])
    assert first == second
    changed = builtin_strategy_specs(
        template_id="t",
        template_revision=2,
        plan_fingerprint="abc",
        backtest_start=date(2020, 1, 1),
        backtest_end=date(2021, 1, 1),
    )
    assert spec_fingerprint(changed["selection_rebalance_v1"]) != first
    # 策略参数变化(不同退出政策)也改变指纹
    sma = spec_fingerprint(specs["selection_sma_timing_v1"])
    assert sma != first


def test_plan_fingerprint_stable_and_parameter_sensitive() -> None:
    registry = build_default_registry()
    plan = _compile_plan()
    fp1 = plan_fingerprint(plan, registry)
    fp2 = plan_fingerprint(plan, registry)
    assert fp1 == fp2
    raw = dict(TEMPLATE_RAW)
    rules = dict(raw["rules"])
    rules["consecutive_up_days"] = {
        "enabled": True,
        "parameters": {"lookback_trading_sessions": 61, "required_consecutive_days": 5},
    }
    raw["rules"] = rules
    plan2 = TemplateCompiler(registry).compile(parse_template(raw))
    assert plan_fingerprint(plan2, registry) != fp1


def test_canonical_json_normalizes_decimal_and_key_order() -> None:
    assert canonical_json({"b": 1, "a": Decimal("1.50")}) == canonical_json(
        {"a": "1.50", "b": 1}
    )
