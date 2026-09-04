"""Fingerprint computation for screening plans and strategy specs (P5A-3).

The fingerprint is a stable digest over the exact configuration that affects
historical eligibility and backtest semantics: template revision, enabled
rules with their parameters, rule implementation summary, dataset generation,
adjustment, evaluation schedule, and every policy id/version/parameters.
Monetary parameters are canonicalized as decimal strings so binary-float
rounding can never change a fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from stock_manager.research.models import PolicyKind, PolicySpec, ResearchStrategySpec
from stock_manager.rules.registry import RuleRegistry
from stock_manager.templates.models import ScreeningPlan


def canonical_json(value: object) -> str:
    """JSON with sorted keys and Decimal rendered as plain decimal strings."""

    def convert(item: object) -> object:
        if isinstance(item, Decimal):
            return str(item)
        if is_dataclass(item) and not isinstance(item, type):
            return convert(asdict(item))
        if isinstance(item, Mapping):
            return {str(key): convert(item[key]) for key in sorted(item)}
        if isinstance(item, (list, tuple)):
            return [convert(entry) for entry in item]
        if isinstance(item, Enum):
            return item.value
        return item

    return json.dumps(convert(value), sort_keys=True, separators=(",", ":"))


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_fingerprint(
    plan: ScreeningPlan,
    registry: RuleRegistry,
    *,
    rule_implementation_version: str = "builtin-v1",
) -> str:
    """Fingerprint of a compiled plan: revision + enabled rules + parameters.

    rule_implementation_version is the caller-declared version of the rule
    implementation (bump it whenever rule semantics change).
    """
    enabled = tuple(
        (configured.rule_id, canonical_json(configured.parameters))
        for configured in plan.enabled_rules
    )
    payload = canonical_json(
        {
            "template_id": plan.template_id,
            "revision": plan.revision,
            "adjustment": plan.adjustment.value,
            "rule_implementation_version": rule_implementation_version,
            "enabled_rules": enabled,
            "composition": canonical_json(
                {
                    "operator": plan.composition.operator,
                    "groups": tuple(
                        (group.group_id, group.operator, tuple(group.rule_ids))
                        for group in plan.composition.groups
                    ),
                }
            ),
        }
    )
    return _digest(payload)


def policy_fingerprint(policy: PolicySpec) -> str:
    return _digest(
        canonical_json(
            {
                "policy_id": policy.policy_id,
                "version": policy.version,
                "parameters": dict(policy.parameters),
            }
        )
    )


def spec_fingerprint(spec: ResearchStrategySpec) -> str:
    """Fingerprint of the whole strategy spec (stable for identical input).

    P5C: entry/exit are fingerprinted as groups (operator + member policies);
    the four single-policy kinds stay as before; take-profit tiers and the
    group structure participate so any strategy change alters the digest.
    """
    policies: dict[str, object] = {
        kind.value: policy_fingerprint(getattr(spec, f"{kind.value}_policy"))
        for kind in PolicyKind
        if kind not in (PolicyKind.ENTRY, PolicyKind.EXIT)
    }
    policies["entry"] = {
        "operator": spec.entry_operator.value,
        "items": tuple(policy_fingerprint(item) for item in spec.entry_policies),
    }
    policies["exit"] = {
        "operator": spec.exit_operator.value,
        "items": tuple(policy_fingerprint(item) for item in spec.exit_policies),
    }
    payload = canonical_json(
        {
            "strategy_spec_id": spec.strategy_spec_id,
            "screening_template_id": spec.screening_template_id,
            "screening_template_revision": spec.screening_template_revision,
            "screening_plan_fingerprint": spec.screening_plan_fingerprint,
            "adjustment": spec.adjustment.value,
            "evaluation_schedule": spec.evaluation_schedule.value,
            "initial_cash": spec.initial_cash,
            "backtest_start": spec.backtest_start.isoformat(),
            "backtest_end": spec.backtest_end.isoformat(),
            "policies": policies,
            "take_profit_tiers": tuple(
                (str(tier.take_profit_ratio), str(tier.partial_ratio))
                for tier in spec.take_profit_tiers
            ),
        }
    )
    return _digest(payload)
