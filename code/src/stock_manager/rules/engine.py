"""Execute registered rules against prefetched immutable context."""

from __future__ import annotations

from types import MappingProxyType

from stock_manager.domain import RuleExecutionResult, RuleStatus
from stock_manager.rules.base import RuleContext
from stock_manager.rules.composition import CompositionEngine
from stock_manager.rules.registry import RuleRegistry
from stock_manager.templates.models import ScreeningPlan


class RuleEngine:
    def __init__(
        self,
        registry: RuleRegistry,
        composition_engine: CompositionEngine | None = None,
    ) -> None:
        self._registry = registry
        self._composition = composition_engine or CompositionEngine()

    def evaluate(
        self, context: RuleContext, plan: ScreeningPlan
    ) -> tuple[bool, tuple[RuleExecutionResult, ...]]:
        executions: dict[str, RuleExecutionResult] = {}
        statuses: dict[str, RuleStatus] = {}
        for configured in plan.enabled_rules:
            result = self._registry.get(configured.rule_id).evaluate(
                context, configured.parameters
            )
            status = RuleStatus.PASSED if result.passed else RuleStatus.FAILED
            executions[configured.rule_id] = RuleExecutionResult(
                configured.rule_id, status, result
            )
            statuses[configured.rule_id] = status
        for rule_id in plan.disabled_rule_ids:
            executions[rule_id] = RuleExecutionResult(
                rule_id, RuleStatus.SKIPPED, None
            )
            statuses[rule_id] = RuleStatus.SKIPPED
        passed = self._composition.evaluate(
            plan.composition.operator,
            plan.composition.groups,
            MappingProxyType(statuses),
        )
        ordered = tuple(executions[rule_id] for rule_id in plan.source_template.rules)
        return passed, ordered
