"""Named strategy templates for the backtest system (P5C-23).

Strategy templates store only the *strategy* part (entry/exit policy groups,
take-profit tiers and the four single-policy kinds) in the same JSON shape the
backtest submission API accepts; run-level settings (window, cash, fees, codes,
workers) are never stored here. Storage mirrors the screening-template JSON
convention (revision bump, user files under ``<user_template_root>/strategies``,
read-only built-in default) but lives in a dedicated sub-directory so the
screening template repository never sees these files.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from stock_manager.research.models import (
    PolicyKind,
    PolicySpec,
    TakeProfitTierSpec,
)
from stock_manager.research.policies import (
    InvalidPolicyParametersError,
    UnknownPolicyError,
)

STRATEGY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_STRATEGY_ID = "default-backtest-v1"

_SINGLE_KINDS = ("rebalance", "allocation", "ranking", "execution")
_GROUP_KINDS = ("entry", "exit")
_GROUP_OPERATORS = {"any", "all"}


class StrategyTemplateError(ValueError):
    """Invalid or conflicting strategy template operation."""


class StrategyTemplateRevisionConflictError(StrategyTemplateError):
    pass


def _policy_from_dict(raw: object) -> PolicySpec:
    if not isinstance(raw, Mapping):
        raise StrategyTemplateError("policy must be an object")
    policy_id = raw.get("policy_id")
    version = raw.get("version")
    parameters = raw.get("parameters") or {}
    if not isinstance(policy_id, str) or not isinstance(version, int):
        raise StrategyTemplateError("policy must carry policy_id and version")
    if not isinstance(parameters, Mapping):
        raise StrategyTemplateError("policy parameters must be an object")
    return PolicySpec(policy_id, version, dict(parameters))


def _policy_to_dict(policy: PolicySpec) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "version": policy.version,
        "parameters": dict(policy.parameters),
    }


def validate_and_normalize_policies(
    payload: Mapping[str, object],
    registry: Any,
) -> dict[str, object]:
    """Validate + normalize a strategy payload against the policy registry.

    Returns the canonical payload: entry/exit groups with ``operator``/``items``,
    the four single-policy kinds, and a normalized ``take_profit_tiers`` list
    (ascending ratio, decimal strings). Raises StrategyTemplateError on any
    schema, registry or range violation.
    """
    try:
        normalized: dict[str, object] = {}
        kinds = set(payload)
        if not {"entry", "exit"}.issubset(kinds):
            raise StrategyTemplateError("entry and exit groups are required")
        for kind in _GROUP_KINDS:
            group = payload.get(kind)
            if not isinstance(group, Mapping):
                raise StrategyTemplateError(f"{kind} group must be an object")
            operator_raw = group.get("operator", "any")
            if operator_raw not in _GROUP_OPERATORS:
                raise StrategyTemplateError(
                    f"{kind} operator must be any or all"
                )
            items = group.get("items")
            if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 5:
                raise StrategyTemplateError(
                    f"{kind} group must contain 1..5 policies"
                )
            specs = tuple(_policy_from_dict(item) for item in items)
            registry.validate_group(specs, PolicyKind(kind))
            normalized[kind] = {
                "operator": operator_raw,
                "items": [_policy_to_dict(spec) for spec in specs],
            }
        for kind in _SINGLE_KINDS:
            item = payload.get(kind)
            if item is None:
                raise StrategyTemplateError(f"{kind} policy is required")
            spec = _policy_from_dict(item)
            registry.validate_spec(spec, PolicyKind(kind))
            normalized[kind] = _policy_to_dict(spec)
        tiers_raw = payload.get("take_profit_tiers") or ()
        if not isinstance(tiers_raw, (list, tuple)) or len(tiers_raw) > 5:
            raise StrategyTemplateError(
                "take_profit_tiers must be a list of at most 5 tiers"
            )
        tiers: list[dict[str, str]] = []
        for raw in tiers_raw:
            if not isinstance(raw, Mapping):
                raise StrategyTemplateError("take-profit tier must be an object")
            try:
                tier = TakeProfitTierSpec(
                    Decimal(str(raw.get("take_profit_ratio"))),
                    Decimal(str(raw.get("partial_ratio"))),
                )
            except (TypeError, ValueError) as error:
                raise StrategyTemplateError(
                    f"invalid take-profit tier: {error}"
                ) from error
            tiers.append(
                {
                    "take_profit_ratio": str(tier.take_profit_ratio),
                    "partial_ratio": str(tier.partial_ratio),
                }
            )
        tiers.sort(key=lambda item: Decimal(item["take_profit_ratio"]))
        normalized["take_profit_tiers"] = tiers
        return normalized
    except (UnknownPolicyError, InvalidPolicyParametersError) as error:
        raise StrategyTemplateError(str(error)) from error


@dataclass(frozen=True, slots=True)
class StrategyTemplate:
    """Immutable named strategy template (P5C)."""

    strategy_template_id: str
    revision: int
    name: str
    description: str
    policies: Mapping[str, object]  # canonical payload w/o metadata

    def __post_init__(self) -> None:
        if not STRATEGY_ID_PATTERN.fullmatch(self.strategy_template_id):
            raise StrategyTemplateError(
                "strategy_template_id must use lowercase letters, digits, hyphens"
            )
        if self.revision <= 0:
            raise StrategyTemplateError("revision must be positive")
        if not self.name.strip() or not self.description.strip():
            raise StrategyTemplateError("name and description must not be empty")
        object.__setattr__(self, "policies", dict(self.policies))

    def to_dict(self) -> dict[str, object]:
        return {
            "metadata": {
                "strategy_template_id": self.strategy_template_id,
                "revision": self.revision,
                "name": self.name,
                "description": self.description,
            },
            "policies": dict(self.policies),
        }


def parse_strategy_template(raw: Mapping[str, object]) -> StrategyTemplate:
    metadata = raw.get("metadata")
    if not isinstance(metadata, Mapping):
        raise StrategyTemplateError("metadata is required")
    try:
        template_id = str(metadata["strategy_template_id"])
        revision = int(metadata["revision"])
        name = str(metadata.get("name", ""))
        description = str(metadata.get("description", ""))
    except (KeyError, TypeError, ValueError) as error:
        raise StrategyTemplateError(f"invalid metadata: {error}") from error
    policies = raw.get("policies")
    if not isinstance(policies, Mapping):
        raise StrategyTemplateError("policies is required")
    return StrategyTemplate(
        strategy_template_id=template_id,
        revision=revision,
        name=name,
        description=description,
        policies=dict(policies),
    )


def default_strategy_template() -> StrategyTemplate:
    """Built-in read-only default strategy (D22 default configuration)."""
    return StrategyTemplate(
        strategy_template_id=DEFAULT_STRATEGY_ID,
        revision=1,
        name="默认策略",
        description="资格即买、资格失效即退；等权分配、按换手率排名、A 股执行模型（运行环境参数请在基础数据中填写）",
        policies={
            "entry": {
                "operator": "any",
                "items": [{"policy_id": "eligibility_enter_v1", "version": 1, "parameters": {}}],
            },
            "exit": {
                "operator": "any",
                "items": [{"policy_id": "eligibility_exit_v1", "version": 1, "parameters": {}}],
            },
            "rebalance": {"policy_id": "daily_v1", "version": 1, "parameters": {}},
            "allocation": {
                "policy_id": "equal_weight_v1",
                "version": 1,
                "parameters": {"max_positions": 20, "cash_reserve_ratio": "0"},
            },
            "ranking": {
                "policy_id": "turnover_20d_desc_v1",
                "version": 1,
                "parameters": {"lookback_trading_days": 20},
            },
            "execution": {"policy_id": "ashare_execution_v1", "version": 1, "parameters": {}},
            "take_profit_tiers": [],
        },
    )


class StrategyTemplateRepository:
    """JSON-file persistence for user strategy templates (P5C)."""

    def __init__(self, user_root: Path) -> None:
        self._user_root = user_root / "strategies"

    def _path(self, strategy_template_id: str) -> Path:
        if not STRATEGY_ID_PATTERN.fullmatch(strategy_template_id):
            raise StrategyTemplateError(
                "strategy_template_id must use lowercase letters, digits, hyphens"
            )
        path = self._user_root / f"{strategy_template_id}.json"
        if self._user_root.exists() and self._user_root.is_symlink():
            raise StrategyTemplateError("strategy template root must not be a symlink")
        return path

    def has(self, strategy_template_id: str) -> bool:
        path = self._path(strategy_template_id)
        return path.is_file() and not path.is_symlink()

    def list_ids(self) -> tuple[str, ...]:
        if not self._user_root.exists():
            return ()
        if self._user_root.is_symlink():
            raise StrategyTemplateError(
                "strategy template root must not be a symlink"
            )
        return tuple(
            sorted(
                path.stem
                for path in self._user_root.glob("*.json")
                if not path.is_symlink()
                and STRATEGY_ID_PATTERN.fullmatch(path.stem)
            )
        )

    def get(self, strategy_template_id: str) -> StrategyTemplate | None:
        path = self._path(strategy_template_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise StrategyTemplateError(
                "strategy template path must be a regular file"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StrategyTemplateError(
                f"unable to read strategy template {strategy_template_id}"
            ) from error
        template = parse_strategy_template(raw)
        if template.strategy_template_id != strategy_template_id:
            raise StrategyTemplateError(
                "strategy_template_id does not match its file name"
            )
        return template

    def save(self, template: StrategyTemplate, *, create: bool) -> None:
        path = self._path(template.strategy_template_id)
        if path.exists() is create:
            action = "already exists" if create else "does not exist"
            raise StrategyTemplateError(
                f"strategy template {template.strategy_template_id} {action}"
            )
        self._user_root.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(template.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{template.strategy_template_id}.",
                suffix=".tmp",
                dir=self._user_root,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
        except OSError as error:
            raise StrategyTemplateError(
                f"unable to save strategy template {template.strategy_template_id}"
            ) from error
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def delete(self, strategy_template_id: str) -> None:
        path = self._path(strategy_template_id)
        if not path.exists():
            raise FileNotFoundError(
                f"strategy template {strategy_template_id} does not exist"
            )
        if path.is_symlink() or not path.is_file():
            raise StrategyTemplateError(
                "strategy template path must be a regular file"
            )
        path.unlink()


class StrategyTemplateService:
    """Validated lifecycle operations for user-owned strategy templates."""

    def __init__(
        self,
        repository: StrategyTemplateRepository,
        registry: Any,
    ) -> None:
        self._repository = repository
        self._registry = registry

    def is_system(self, strategy_template_id: str) -> bool:
        return (
            strategy_template_id == DEFAULT_STRATEGY_ID
            and not self._repository.has(strategy_template_id)
        )

    def list_ids(self) -> tuple[str, ...]:
        ids = set(self._repository.list_ids())
        if DEFAULT_STRATEGY_ID not in ids:
            ids.add(DEFAULT_STRATEGY_ID)
        return tuple(sorted(ids))

    def get(self, strategy_template_id: str) -> StrategyTemplate:
        if strategy_template_id == DEFAULT_STRATEGY_ID:
            if self._repository.has(strategy_template_id):
                return self._repository.get(strategy_template_id)  # type: ignore[return-value]
            default = default_strategy_template()
            if default.strategy_template_id == strategy_template_id:
                return default
        template = self._repository.get(strategy_template_id)
        if template is None:
            raise FileNotFoundError(
                f"strategy template {strategy_template_id} does not exist"
            )
        return template

    def create(self, template: StrategyTemplate) -> StrategyTemplate:
        if template.revision != 1:
            raise StrategyTemplateError("a new strategy template must start at revision 1")
        if self.is_system(template.strategy_template_id):
            raise StrategyTemplateError("system strategy templates are read-only")
        normalized = validate_and_normalize_policies(
            template.policies, self._registry
        )
        validated = StrategyTemplate(
            strategy_template_id=template.strategy_template_id,
            revision=template.revision,
            name=template.name,
            description=template.description,
            policies=normalized,
        )
        self._repository.save(validated, create=True)
        return validated

    def update(
        self,
        template: StrategyTemplate,
        *,
        expected_revision: int,
    ) -> StrategyTemplate:
        if self.is_system(template.strategy_template_id):
            raise StrategyTemplateError("system strategy templates are read-only")
        current = self.get(template.strategy_template_id)
        if current.revision != expected_revision:
            raise StrategyTemplateRevisionConflictError(
                f"expected revision {expected_revision}, "
                f"found {current.revision}"
            )
        normalized = validate_and_normalize_policies(
            template.policies, self._registry
        )
        updated = StrategyTemplate(
            strategy_template_id=template.strategy_template_id,
            revision=expected_revision + 1,
            name=template.name,
            description=template.description,
            policies=normalized,
        )
        self._repository.save(updated, create=False)
        return updated

    def delete(self, strategy_template_id: str, *, expected_revision: int) -> None:
        if self.is_system(strategy_template_id):
            raise StrategyTemplateError("system strategy templates are read-only")
        current = self.get(strategy_template_id)
        if current.revision != expected_revision:
            raise StrategyTemplateRevisionConflictError(
                f"expected revision {expected_revision}, "
                f"found {current.revision}"
            )
        self._repository.delete(strategy_template_id)
