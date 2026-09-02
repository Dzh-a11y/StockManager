"""Template API serialization helpers (P3-3)."""

from __future__ import annotations

from collections.abc import Callable

from stock_manager.templates.models import (
    TemplateDefinition,
    parse_template,
    template_to_dict,
)
from stock_manager.templates.service import TemplateService


def _wrapper(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def parse_template_wrapper(raw: object) -> TemplateDefinition:
    """Parse `{"template": {...}}` into a validated TemplateDefinition."""
    root = _wrapper(raw, "body")
    unknown = set(root) - {"template"}
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if "template" not in root:
        raise ValueError("missing template")
    return parse_template(root["template"])


def summary_entry(template: TemplateDefinition, is_system: bool) -> dict[str, object]:
    return {
        "template_id": template.metadata.template_id,
        "revision": template.metadata.revision,
        "name": template.metadata.name,
        "description": template.metadata.description,
        "is_system": is_system,
    }


def template_list(
    service: TemplateService,
    is_system: Callable[[str], bool],
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for template_id in service.list_ids():
        template = service.get(template_id)
        entries.append(summary_entry(template, is_system(template_id)))
    return {"templates": entries}


def full_template(
    template: TemplateDefinition,
    is_system: bool,
) -> dict[str, object]:
    return {
        "template": template_to_dict(template),
        "is_system": is_system,
    }


def save_confirmation(template_id: str, revision: int) -> dict[str, object]:
    return {"template_id": template_id, "revision": revision}
