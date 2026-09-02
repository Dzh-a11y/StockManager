"""Explicit registry for trusted backend rules."""

from __future__ import annotations

from collections.abc import Iterable

from stock_manager.rules.base import RuleDefinition, ScreeningRule


class RuleRegistry:
    def __init__(self, rules: Iterable[ScreeningRule] = ()) -> None:
        self._rules: dict[str, ScreeningRule] = {}
        for rule in rules:
            self.register(rule)

    def register(self, rule: ScreeningRule) -> None:
        rule_id = rule.definition.rule_id
        if rule_id in self._rules:
            raise ValueError(f"duplicate rule_id: {rule_id}")
        self._rules[rule_id] = rule

    def get(self, rule_id: str) -> ScreeningRule:
        try:
            return self._rules[rule_id]
        except KeyError as error:
            raise KeyError(f"unknown rule_id: {rule_id}") from error

    def definitions(self) -> tuple[RuleDefinition, ...]:
        return tuple(
            self._rules[rule_id].definition for rule_id in sorted(self._rules)
        )
