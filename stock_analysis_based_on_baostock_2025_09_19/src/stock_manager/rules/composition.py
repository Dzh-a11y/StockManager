"""Generic composition of registered rule execution statuses."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from stock_manager.domain import RuleStatus

LogicalOperator = Literal["all", "any"]


@dataclass(frozen=True, slots=True)
class RuleGroup:
    group_id: str
    operator: LogicalOperator
    rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValueError("group_id must not be empty")
        if self.operator not in ("all", "any"):
            raise ValueError("group operator must be all or any")
        if not self.rule_ids:
            raise ValueError("rule group must contain at least one rule_id")
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError(f"duplicate rule_id in group {self.group_id}")


class CompositionEngine:
    def evaluate(
        self,
        operator: LogicalOperator,
        groups: tuple[RuleGroup, ...],
        statuses: Mapping[str, RuleStatus],
    ) -> bool:
        if operator not in ("all", "any"):
            raise ValueError("composition operator must be all or any")
        group_results: list[bool] = []
        enabled_result_count = 0
        for group in groups:
            active_statuses: list[RuleStatus] = []
            for rule_id in group.rule_ids:
                if rule_id not in statuses:
                    raise ValueError(f"missing rule status: {rule_id}")
                status = statuses[rule_id]
                if status is not RuleStatus.SKIPPED:
                    active_statuses.append(status)
            if not active_statuses:
                continue
            enabled_result_count += len(active_statuses)
            passed = tuple(status is RuleStatus.PASSED for status in active_statuses)
            group_results.append(
                all(passed) if group.operator == "all" else any(passed)
            )
        if enabled_result_count == 0:
            raise ValueError("no enabled rule results to compose")
        return all(group_results) if operator == "all" else any(group_results)
