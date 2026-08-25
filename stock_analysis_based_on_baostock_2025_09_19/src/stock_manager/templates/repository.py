"""Safe JSON-file persistence for system and user screening templates."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from stock_manager.templates.models import (
    TemplateDefinition,
    parse_template,
    template_to_dict,
)

TEMPLATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class TemplateRepositoryProtocol(Protocol):
    def list_ids(self) -> tuple[str, ...]: ...

    def get(self, template_id: str) -> TemplateDefinition | None: ...

    def save(self, template: TemplateDefinition, *, create: bool) -> None: ...

    def delete(self, template_id: str) -> None: ...

    def is_system(self, template_id: str) -> bool: ...


class JsonTemplateRepository:
    def __init__(self, system_root: Path, user_root: Path) -> None:
        self._system_root = system_root
        self._user_root = user_root

    @staticmethod
    def validate_template_id(template_id: str) -> str:
        if not TEMPLATE_ID_PATTERN.fullmatch(template_id):
            raise ValueError("template_id must use lowercase letters, digits, and hyphens")
        return template_id

    def _path(self, root: Path, template_id: str) -> Path:
        safe_id = self.validate_template_id(template_id)
        path = root / f"{safe_id}.json"
        if root.exists() and root.is_symlink():
            raise ValueError("template root must not be a symbolic link")
        return path

    def list_ids(self) -> tuple[str, ...]:
        ids: set[str] = set()
        for root in (self._system_root, self._user_root):
            if not root.exists():
                continue
            if root.is_symlink():
                raise ValueError("template root must not be a symbolic link")
            for path in root.glob("*.json"):
                if path.is_symlink():
                    continue
                if TEMPLATE_ID_PATTERN.fullmatch(path.stem):
                    ids.add(path.stem)
        return tuple(sorted(ids))

    def get(self, template_id: str) -> TemplateDefinition | None:
        for root in (self._user_root, self._system_root):
            path = self._path(root, template_id)
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("template path must be a regular file")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"unable to read template {template_id}") from error
            template = parse_template(raw)
            if template.metadata.template_id != template_id:
                raise ValueError("template_id does not match its file name")
            return template
        return None

    def is_system(self, template_id: str) -> bool:
        return self._path(self._system_root, template_id).is_file()

    def save(self, template: TemplateDefinition, *, create: bool) -> None:
        template_id = template.metadata.template_id
        if self.is_system(template_id):
            raise PermissionError("system templates are read-only")
        path = self._path(self._user_root, template_id)
        if path.exists() is create:
            action = "already exists" if create else "does not exist"
            raise FileExistsError(f"template {template_id} {action}")
        self._user_root.mkdir(parents=True, exist_ok=True)
        if self._user_root.is_symlink():
            raise ValueError("template root must not be a symbolic link")
        payload = json.dumps(
            template_to_dict(template),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{template_id}.", suffix=".tmp", dir=self._user_root
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
        except OSError as error:
            raise ValueError(f"unable to save template {template_id}") from error
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def delete(self, template_id: str) -> None:
        if self.is_system(template_id):
            raise PermissionError("system templates are read-only")
        path = self._path(self._user_root, template_id)
        if not path.exists():
            raise FileNotFoundError(f"template {template_id} does not exist")
        if path.is_symlink() or not path.is_file():
            raise ValueError("template path must be a regular file")
        path.unlink()
