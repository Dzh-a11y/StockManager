"""Picklable view of a ScreeningPlan for process-pool workers (P4-4).

ScreeningPlan itself is not picklable because TemplateDefinition wraps rule
entries in mappingproxy. Workers therefore receive this flat, immutable view
and rebuild a ScreeningPlan inside the child process.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from stock_manager.domain import AdjustmentMethod
from stock_manager.templates.models import CompositionDefinition
from stock_manager.templates.models import (
    ConfiguredRule,
    RuleTemplateEntry,
    ScreeningPlan,
    TemplateDefinition,
    TemplateMetadata,
)


@dataclass(frozen=True, slots=True)
class PicklableScreeningPlan:
    template_id: str
    revision: int
    adjustment: AdjustmentMethod
    enabled_rules: tuple[ConfiguredRule, ...]
    disabled_rule_ids: tuple[str, ...]
    composition: CompositionDefinition
    rule_order: tuple[str, ...]
    template_schema_version: int
    rule_parameters: tuple[tuple[str, dict[str, object]], ...]

    @classmethod
    def from_plan(cls, plan: ScreeningPlan) -> "PicklableScreeningPlan":
        return cls(
            plan.template_id,
            plan.revision,
            plan.adjustment,
            plan.enabled_rules,
            plan.disabled_rule_ids,
            plan.composition,
            tuple(plan.source_template.rules),
            plan.source_template.metadata.schema_version,
            tuple(
                (
                    rule_id,
                    dict(plan.source_template.rules[rule_id].parameters),
                )
                for rule_id in plan.source_template.rules
            ),
        )

    def to_plan(self) -> ScreeningPlan:
        parameters = dict(self.rule_parameters)
        template = TemplateDefinition(
            TemplateMetadata(
                schema_version=self.template_schema_version,
                template_id=self.template_id,
                revision=self.revision,
                name="",
                description="",
                timezone="Asia/Shanghai",
                technical_adjustment=self.adjustment,
            ),
            MappingProxyType(
                {
                    rule_id: RuleTemplateEntry(
                        enabled=rule_id not in self.disabled_rule_ids,
                        parameters=MappingProxyType(
                            dict(parameters.get(rule_id, {})),
                        ),
                    )
                    for rule_id in self.rule_order
                }
            ),
            self.composition,
        )
        return ScreeningPlan(
            template_id=self.template_id,
            revision=self.revision,
            adjustment=self.adjustment,
            enabled_rules=self.enabled_rules,
            disabled_rule_ids=self.disabled_rule_ids,
            composition=self.composition,
            source_template=template,
        )


