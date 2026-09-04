"""Research strategy domain, policy registry and fingerprints (P5A-3)."""

from __future__ import annotations

from stock_manager.research.builtin import (
    build_default_policy_registry,
    builtin_strategy_specs,
)
from stock_manager.research.fingerprint import (
    canonical_json,
    plan_fingerprint,
    policy_fingerprint,
    spec_fingerprint,
)
from stock_manager.research.models import (
    EvaluationSchedule,
    PolicyKind,
    PolicyOperator,
    PolicyParameterSpec,
    PolicyParameterType,
    PolicySpec,
    ResearchStrategySpec,
    TakeProfitTierSpec,
)
from stock_manager.research.policies import (
    InvalidPolicyParametersError,
    PolicyDefinition,
    PolicyRegistry,
    UnknownPolicyError,
)

__all__ = [
    "EvaluationSchedule",
    "InvalidPolicyParametersError",
    "PolicyDefinition",
    "PolicyKind",
    "PolicyOperator",
    "PolicyParameterSpec",
    "PolicyParameterType",
    "PolicyRegistry",
    "PolicySpec",
    "ResearchStrategySpec",
    "TakeProfitTierSpec",
    "UnknownPolicyError",
    "build_default_policy_registry",
    "builtin_strategy_specs",
    "canonical_json",
    "plan_fingerprint",
    "policy_fingerprint",
    "spec_fingerprint",
]
