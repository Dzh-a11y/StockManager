"""P5C contract tests: multi-policy entry/exit groups, take-profit tiers,
run-level settings and group validation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from stock_manager.domain import AdjustmentMethod
from stock_manager.research import (
    EvaluationSchedule,
    PolicyKind,
    PolicyOperator,
    PolicySpec,
    ResearchStrategySpec,
    TakeProfitTierSpec,
    build_default_policy_registry,
    canonical_json,
    spec_fingerprint,
    UnknownPolicyError,
)

QFQ = AdjustmentMethod.QFQ


def _spec(
    *,
    entry: tuple[PolicySpec, ...] | None = None,
    exit: tuple[PolicySpec, ...] | None = None,
    entry_operator: PolicyOperator = PolicyOperator.ANY,
    exit_operator: PolicyOperator = PolicyOperator.ANY,
    tiers: tuple[TakeProfitTierSpec, ...] = (),
    **overrides: object,
) -> ResearchStrategySpec:
    legacy_entry = PolicySpec("eligibility_enter_v1", 1, {})
    legacy_exit = PolicySpec("eligibility_exit_v1", 1, {})
    entry_policies = legacy_entry if entry is None else entry
    exit_policies = legacy_exit if exit is None else exit
    if isinstance(entry_policies, PolicySpec):
        entry_policies = (entry_policies,)
    if isinstance(exit_policies, PolicySpec):
        exit_policies = (exit_policies,)
    kwargs = dict(
        strategy_spec_id="s",
        screening_template_id="t",
        screening_template_revision=1,
        screening_plan_fingerprint="fp",
        adjustment=QFQ,
        evaluation_schedule=EvaluationSchedule.DAILY,
        entry_policy=entry_policies[0],
        exit_policy=exit_policies[0],
        rebalance_policy=PolicySpec("daily_v1", 1, {}),
        allocation_policy=PolicySpec("equal_weight_v1", 1, {"max_positions": 20}),
        ranking_policy=PolicySpec("turnover_20d_desc_v1", 1, {}),
        execution_policy=PolicySpec("ashare_execution_v1", 1, {}),
        initial_cash=Decimal("1000000"),
        backtest_start=date(2020, 1, 1),
        backtest_end=date(2021, 1, 1),
        entry_policies=tuple(entry_policies),
        exit_policies=tuple(exit_policies),
        entry_operator=entry_operator,
        exit_operator=exit_operator,
        take_profit_tiers=tiers,
    )
    kwargs.update(overrides)
    return ResearchStrategySpec(**kwargs)  # type: ignore[arg-type]


def test_legacy_single_policy_construction_derives_singleton_groups() -> None:
    spec = ResearchStrategySpec(
        "s",
        "t",
        1,
        "fp",
        QFQ,
        EvaluationSchedule.DAILY,
        PolicySpec("eligibility_enter_v1", 1, {}),
        PolicySpec("eligibility_exit_v1", 1, {}),
        PolicySpec("daily_v1", 1, {}),
        PolicySpec("equal_weight_v1", 1, {"max_positions": 20}),
        PolicySpec("turnover_20d_desc_v1", 1, {}),
        PolicySpec("ashare_execution_v1", 1, {}),
        Decimal("1000000"),
        date(2020, 1, 1),
        date(2021, 1, 1),
    )
    assert spec.entry_policies == (spec.entry_policy,)
    assert spec.exit_policies == (spec.exit_policy,)
    assert spec.entry_operator is PolicyOperator.ANY
    assert spec.exit_operator is PolicyOperator.ANY
    assert spec.take_profit_tiers == ()


def test_multi_policy_groups_and_operators_are_preserved() -> None:
    entry = (
        PolicySpec("sma_below_v1", 1, {"sma_period": 20}),
        PolicySpec("pullback_entry_v1", 1, {"lookback_trading_days": 10, "drawdown_ratio": "0.05"}),
    )
    spec = _spec(
        entry=entry,
        exit=(PolicySpec("sma_above_v1", 1, {"sma_period": 20}),),
        entry_operator=PolicyOperator.ALL,
    )
    assert spec.entry_policies == entry
    assert spec.entry_operator is PolicyOperator.ALL


def test_duplicate_same_policy_instances_allowed() -> None:
    spec = _spec(
        exit=(
            PolicySpec("sma_above_v1", 1, {"sma_period": 20}),
            PolicySpec("sma_above_v1", 1, {"sma_period": 60}),
        )
    )
    assert len(spec.exit_policies) == 2


def test_take_profit_tiers_sorted_and_capped() -> None:
    tiers = (
        TakeProfitTierSpec(Decimal("0.20"), Decimal("0.5")),
        TakeProfitTierSpec(Decimal("0.10"), Decimal("0.5")),
    )
    spec = _spec(tiers=tiers)
    assert [t.take_profit_ratio for t in spec.take_profit_tiers] == [
        Decimal("0.10"),
        Decimal("0.20"),
    ]
    with pytest.raises(ValueError, match="at most 5"):
        _spec(tiers=tuple(TakeProfitTierSpec(Decimal(i), Decimal("0.5")) for i in range(1, 7)))
    with pytest.raises(ValueError, match="take_profit_ratio must be positive"):
        TakeProfitTierSpec(Decimal("0"), Decimal("0.5"))
    with pytest.raises(ValueError, match="partial_ratio"):
        TakeProfitTierSpec(Decimal("0.1"), Decimal("0"))


def test_stock_codes_dedup_and_ignore_mode_requires_codes() -> None:
    spec = _spec(stock_codes=("sh.600000", "sz.000001", "sh.600000"))
    assert spec.stock_codes == ("sh.600000", "sz.000001")
    with pytest.raises(ValueError, match="requires at least one stock code"):
        _spec(ignore_eligibility=True)
    with pytest.raises(ValueError, match="non-empty"):
        _spec(stock_codes=("sh.600000", ""))
    spec = _spec(stock_codes=("sh.600000",), ignore_eligibility=True)
    assert spec.ignore_eligibility


def test_group_length_bounds() -> None:
    many = tuple(PolicySpec("sma_below_v1", 1, {"sma_period": 20}) for _ in range(6))
    with pytest.raises(ValueError, match="1..5"):
        _spec(entry=many)
    registry = build_default_policy_registry()
    with pytest.raises(ValueError, match="1..5"):
        registry.validate_group((), PolicyKind.ENTRY)


def test_registry_validate_group() -> None:
    registry = build_default_policy_registry()
    registry.validate_group(
        (PolicySpec("sma_below_v1", 1, {"sma_period": 20}),), PolicyKind.ENTRY
    )
    with pytest.raises(UnknownPolicyError):
        registry.validate_group((PolicySpec("nope_v1", 1, {}),), PolicyKind.ENTRY)
    with pytest.raises(ValueError, match="entry or exit"):
        registry.validate_group(
            (PolicySpec("equal_weight_v1", 1, {"max_positions": 5}),),
            PolicyKind.ALLOCATION,
        )


def test_spec_fingerprint_group_sensitive() -> None:
    single = _spec()
    same = _spec()
    assert spec_fingerprint(single) == spec_fingerprint(same)
    any_exit = _spec(
        exit=(
            PolicySpec("sma_timing_v1", 1, {"sma_period": 20}),
            PolicySpec("sma_above_v1", 1, {"sma_period": 20}),
        )
    )
    all_exit = _spec(
        exit=(
            PolicySpec("sma_timing_v1", 1, {"sma_period": 20}),
            PolicySpec("sma_above_v1", 1, {"sma_period": 20}),
        ),
        exit_operator=PolicyOperator.ALL,
    )
    assert spec_fingerprint(any_exit) != spec_fingerprint(single)
    assert spec_fingerprint(any_exit) != spec_fingerprint(all_exit)
    tiered = _spec(tiers=(TakeProfitTierSpec(Decimal("0.1"), Decimal("0.5")),))
    assert spec_fingerprint(tiered) != spec_fingerprint(single)


def test_canonical_json_handles_new_structures() -> None:
    payload = {
        "operator": PolicyOperator.ALL.value,
        "tier": TakeProfitTierSpec(Decimal("0.10"), Decimal("0.5")),
    }
    first = canonical_json(payload)
    second = canonical_json(
        {"tier": {"partial_ratio": "0.5", "take_profit_ratio": "0.10"}, "operator": "all"}
    )
    assert first == second
