import json
from dataclasses import replace
from pathlib import Path

import pytest

from stock_manager.rules.builtin import build_default_registry
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template, template_to_dict
from stock_manager.templates.repository import JsonTemplateRepository
from stock_manager.templates.service import (
    TemplateRevisionConflictError,
    TemplateService,
)


def default_template():
    path = Path(__file__).parents[1] / "config/rule_templates/system-default.json"
    return parse_template(json.loads(path.read_text(encoding="utf-8")))


def service(tmp_path: Path) -> TemplateService:
    repository = JsonTemplateRepository(tmp_path / "system", tmp_path / "user")
    return TemplateService(
        repository,
        TemplateCompiler(build_default_registry()),
    )


def user_template(template_id: str = "my-strategy"):
    template = default_template()
    return replace(
        template,
        metadata=replace(
            template.metadata,
            template_id=template_id,
            name="我的策略",
            revision=1,
        ),
    )


def test_user_template_create_update_delete_lifecycle(tmp_path: Path) -> None:
    templates = service(tmp_path)

    created = templates.create(user_template())
    assert created.revision == 1
    assert templates.list_ids() == ("my-strategy",)

    changed = replace(
        user_template(),
        metadata=replace(user_template().metadata, description="更新说明"),
    )
    updated = templates.update(changed, expected_revision=1)
    assert updated.revision == 2
    assert templates.get("my-strategy").metadata.description == "更新说明"

    with pytest.raises(TemplateRevisionConflictError):
        templates.update(changed, expected_revision=1)

    templates.delete("my-strategy", expected_revision=2)
    assert templates.list_ids() == ()


@pytest.mark.parametrize("template_id", ("../escape", "UPPER", "has space", ""))
def test_repository_rejects_unsafe_template_ids(
    tmp_path: Path, template_id: str
) -> None:
    with pytest.raises(ValueError, match="template_id"):
        service(tmp_path).create(user_template(template_id))


def test_system_template_is_read_only(tmp_path: Path) -> None:
    system_root = tmp_path / "system"
    system_root.mkdir()
    template = default_template()
    (system_root / "system-default.json").write_text(
        json.dumps(template_to_dict(template), ensure_ascii=False),
        encoding="utf-8",
    )
    templates = TemplateService(
        JsonTemplateRepository(system_root, tmp_path / "user"),
        TemplateCompiler(build_default_registry()),
    )

    with pytest.raises(PermissionError, match="read-only"):
        templates.delete("system-default", expected_revision=1)


def test_create_validates_before_writing(tmp_path: Path) -> None:
    template = user_template()
    invalid = replace(
        template,
        composition=replace(template.composition, groups=()),
    )

    with pytest.raises(ValueError):
        service(tmp_path).create(invalid)

    assert not (tmp_path / "user" / "my-strategy.json").exists()
