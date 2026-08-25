"""Compile validated templates into immutable screening plans."""

from collections import Counter

from stock_manager.rules.registry import RuleRegistry
from stock_manager.templates.models import ConfiguredRule, ScreeningPlan, TemplateDefinition


class TemplateCompiler:
    def __init__(self, registry: RuleRegistry) -> None:
        self._registry = registry

    def compile(self, template: TemplateDefinition) -> ScreeningPlan:
        enabled: list[ConfiguredRule] = []
        disabled: list[str] = []
        for rule_id, entry in template.rules.items():
            rule = self._registry.get(rule_id)
            parameters = rule.parse_parameters(dict(entry.parameters))
            if entry.enabled:
                enabled.append(
                    ConfiguredRule(
                        rule_id,
                        parameters,
                        rule.data_requirement(parameters),
                    )
                )
            else:
                disabled.append(rule_id)
        if not enabled:
            raise ValueError("at least one rule must be enabled")

        enabled_ids = {item.rule_id for item in enabled}
        references = Counter(
            rule_id
            for group in template.composition.groups
            for rule_id in group.rule_ids
        )
        unknown_references = set(references) - set(template.rules)
        if unknown_references:
            names = ", ".join(sorted(unknown_references))
            raise ValueError(f"composition references unknown template rule(s): {names}")
        if any(references[rule_id] != 1 for rule_id in enabled_ids):
            raise ValueError("enabled rules must appear exactly once in composition")
        disabled_references = set(references) - enabled_ids
        if disabled_references:
            names = ", ".join(sorted(disabled_references))
            raise ValueError(f"disabled rules must not appear in composition: {names}")
        return ScreeningPlan(
            template_id=template.metadata.template_id,
            revision=template.metadata.revision,
            adjustment=template.metadata.technical_adjustment,
            enabled_rules=tuple(enabled),
            disabled_rule_ids=tuple(sorted(disabled)),
            composition=template.composition,
            source_template=template,
        )
