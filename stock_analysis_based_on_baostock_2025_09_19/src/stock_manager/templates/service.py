"""Validated lifecycle operations for user-owned screening templates."""

from dataclasses import replace

from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import ScreeningPlan, TemplateDefinition
from stock_manager.templates.repository import TemplateRepositoryProtocol


class TemplateRevisionConflictError(ValueError):
    pass


class TemplateService:
    def __init__(
        self,
        repository: TemplateRepositoryProtocol,
        compiler: TemplateCompiler,
    ) -> None:
        self._repository = repository
        self._compiler = compiler

    def list_ids(self) -> tuple[str, ...]:
        return self._repository.list_ids()

    def get(self, template_id: str) -> TemplateDefinition:
        template = self._repository.get(template_id)
        if template is None:
            raise FileNotFoundError(f"template {template_id} does not exist")
        return template

    def compile(self, template_id: str) -> ScreeningPlan:
        return self._compiler.compile(self.get(template_id))

    def create(self, template: TemplateDefinition) -> ScreeningPlan:
        if template.metadata.revision != 1:
            raise ValueError("a new template must start at revision 1")
        plan = self._compiler.compile(template)
        self._repository.save(template, create=True)
        return plan

    def update(
        self,
        template: TemplateDefinition,
        *,
        expected_revision: int,
    ) -> ScreeningPlan:
        current = self.get(template.metadata.template_id)
        if current.metadata.revision != expected_revision:
            raise TemplateRevisionConflictError(
                f"expected revision {expected_revision}, found {current.metadata.revision}"
            )
        updated = replace(
            template,
            metadata=replace(template.metadata, revision=expected_revision + 1),
        )
        plan = self._compiler.compile(updated)
        self._repository.save(updated, create=False)
        return plan

    def delete(self, template_id: str, *, expected_revision: int) -> None:
        current = self.get(template_id)
        if current.metadata.revision != expected_revision:
            raise TemplateRevisionConflictError(
                f"expected revision {expected_revision}, found {current.metadata.revision}"
            )
        self._repository.delete(template_id)
