"""Rule catalog serialization (P3-1): expose registry metadata to the frontend."""

from __future__ import annotations

from collections.abc import Sequence

from stock_manager.rules.base import RuleDefinition
from stock_manager.rules.registry import RuleRegistry
from stock_manager.web.serialization import to_jsonable


def rule_catalog(registry: RuleRegistry) -> dict[str, Sequence[dict[str, object]]]:
    """Serialize every registered RuleDefinition without a second rule list."""
    return {"rules": [rule_to_dict(item) for item in registry.definitions()]}


def rule_to_dict(definition: RuleDefinition) -> dict[str, object]:
    return {
        "rule_id": definition.rule_id,
        "name": definition.name,
        "description": definition.description,
        "parameters": [
            {
                "parameter_id": item.parameter_id,
                "value_type": item.value_type.value,
                "required": item.required,
                "default_value": to_jsonable(item.default_value),
                "minimum": to_jsonable(item.minimum),
                "maximum": to_jsonable(item.maximum),
                "label": item.label,
                "description": item.description,
            }
            for item in definition.parameters
        ],
    }
