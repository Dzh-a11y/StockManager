"""Strict version-2 screening-template models."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from stock_manager.domain import AdjustmentMethod
from stock_manager.rules.base import RuleDataRequirement
from stock_manager.rules.composition import LogicalOperator, RuleGroup


@dataclass(frozen=True, slots=True)
class TemplateMetadata:
    schema_version: int
    template_id: str
    revision: int
    name: str
    description: str
    timezone: str
    technical_adjustment: AdjustmentMethod


@dataclass(frozen=True, slots=True)
class RuleTemplateEntry:
    enabled: bool
    parameters: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CompositionDefinition:
    operator: LogicalOperator
    groups: tuple[RuleGroup, ...]


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    metadata: TemplateMetadata
    rules: Mapping[str, RuleTemplateEntry]
    composition: CompositionDefinition


@dataclass(frozen=True, slots=True)
class ConfiguredRule:
    rule_id: str
    parameters: object
    data_requirement: RuleDataRequirement


@dataclass(frozen=True, slots=True)
class ScreeningPlan:
    template_id: str
    revision: int
    adjustment: AdjustmentMethod
    enabled_rules: tuple[ConfiguredRule, ...]
    disabled_rule_ids: tuple[str, ...]
    composition: CompositionDefinition
    source_template: TemplateDefinition


def _object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _exact_fields(
    value: dict[str, object], expected: set[str], field_name: str
) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{field_name} has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{field_name} is missing field(s): {', '.join(sorted(missing))}")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(value: object, field_name: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _operator(value: object, field_name: str) -> Literal["all", "any"]:
    if value not in ("all", "any"):
        raise ValueError(f"{field_name} must be all or any")
    return value


def parse_template(raw: object) -> TemplateDefinition:
    root = _object(raw, "template")
    _exact_fields(root, {"metadata", "rules", "composition"}, "template")
    metadata_raw = _object(root["metadata"], "metadata")
    _exact_fields(
        metadata_raw,
        {
            "schema_version",
            "template_id",
            "revision",
            "name",
            "description",
            "timezone",
            "technical_adjustment",
        },
        "metadata",
    )
    schema_version = _integer(
        metadata_raw["schema_version"], "schema_version", minimum=1
    )
    if schema_version != 2:
        raise ValueError(f"unsupported template schema: {schema_version}")
    timezone = _text(metadata_raw["timezone"], "timezone")
    if timezone != "Asia/Shanghai":
        raise ValueError("timezone must be Asia/Shanghai")
    try:
        adjustment = AdjustmentMethod(
            _text(metadata_raw["technical_adjustment"], "technical_adjustment")
        )
    except ValueError as error:
        raise ValueError("technical_adjustment is unsupported") from error
    metadata = TemplateMetadata(
        schema_version=schema_version,
        template_id=_text(metadata_raw["template_id"], "template_id"),
        revision=_integer(metadata_raw["revision"], "revision", minimum=1),
        name=_text(metadata_raw["name"], "name"),
        description=_text(metadata_raw["description"], "description"),
        timezone=timezone,
        technical_adjustment=adjustment,
    )

    rules_raw = _object(root["rules"], "rules")
    parsed_rules: dict[str, RuleTemplateEntry] = {}
    for raw_rule_id, raw_entry in rules_raw.items():
        rule_id = _text(raw_rule_id, "rule_id")
        entry = _object(raw_entry, f"rules.{rule_id}")
        _exact_fields(entry, {"enabled", "parameters"}, f"rules.{rule_id}")
        enabled = entry["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(f"rules.{rule_id}.enabled must be a boolean")
        parameters = _object(entry["parameters"], f"rules.{rule_id}.parameters")
        parsed_rules[rule_id] = RuleTemplateEntry(
            enabled, MappingProxyType(dict(parameters))
        )

    composition_raw = _object(root["composition"], "composition")
    _exact_fields(composition_raw, {"operator", "groups"}, "composition")
    groups_raw = composition_raw["groups"]
    if not isinstance(groups_raw, list):
        raise ValueError("composition.groups must be an array")
    groups: list[RuleGroup] = []
    for index, raw_group in enumerate(groups_raw):
        group = _object(raw_group, f"composition.groups[{index}]")
        _exact_fields(
            group,
            {"group_id", "operator", "rules"},
            f"composition.groups[{index}]",
        )
        raw_rule_ids = group["rules"]
        if not isinstance(raw_rule_ids, list):
            raise ValueError(f"composition.groups[{index}].rules must be an array")
        groups.append(
            RuleGroup(
                _text(group["group_id"], "group_id"),
                _operator(group["operator"], "group operator"),
                tuple(_text(item, "composition rule_id") for item in raw_rule_ids),
            )
        )
    if not groups:
        raise ValueError("composition must contain at least one group")
    group_ids = tuple(group.group_id for group in groups)
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("composition group_id values must be unique")
    return TemplateDefinition(
        metadata,
        MappingProxyType(parsed_rules),
        CompositionDefinition(
            _operator(composition_raw["operator"], "composition operator"),
            tuple(groups),
        ),
    )


def template_to_dict(template: TemplateDefinition) -> dict[str, object]:
    return {
        "metadata": {
            "schema_version": template.metadata.schema_version,
            "template_id": template.metadata.template_id,
            "revision": template.metadata.revision,
            "name": template.metadata.name,
            "description": template.metadata.description,
            "timezone": template.metadata.timezone,
            "technical_adjustment": template.metadata.technical_adjustment.value,
        },
        "rules": {
            rule_id: {
                "enabled": entry.enabled,
                "parameters": dict(entry.parameters),
            }
            for rule_id, entry in template.rules.items()
        },
        "composition": {
            "operator": template.composition.operator,
            "groups": [
                {
                    "group_id": group.group_id,
                    "operator": group.operator,
                    "rules": list(group.rule_ids),
                }
                for group in template.composition.groups
            ],
        },
    }
